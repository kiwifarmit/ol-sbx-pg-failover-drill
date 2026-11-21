from pgdrill.verify import analyze

A, B = "10.0.0.1", "10.0.0.2"
NODES = {A: "pg1", B: "pg2"}


def stream(w, srv, t0, t1, step=0.01, seq0=1):
    """Synthetic acks for one writer from t0 to t1."""
    out, seq, t = [], seq0, t0
    while t < t1:
        out.append({"ev": "ack", "t": t, "w": w, "seq": seq, "srv": srv})
        seq += 1
        t += step
    return out, seq


def keys(evs):
    return [(e["w"], e["seq"]) for e in evs if e["ev"] == "ack"]


def test_clean_failover_no_loss():
    pre, nxt = stream(1, A, 0.0, 10.0)
    post, _ = stream(1, B, 14.0, 20.0, seq0=nxt + 1)
    err = {"ev": "err", "t": 10.0, "w": 1, "seq": nxt, "phase": "commit"}
    events = pre + [err] + post + [{"ev": "stop", "t": 20.0, "hung_writers": 0}]
    r = analyze(events, keys(events), fault_t=10.0, ip_to_node=NODES)

    assert r["rpo"]["lost_acked"] == 0
    assert r["in_doubt"] == {"total": 1, "committed": 0, "not_committed": 1}
    assert r["rto"]["recovered"]
    assert 3.9 < r["rto"]["max_gap_s"] < 4.1
    assert r["rto"]["outages"] == 1
    assert not r["split_brain"]["detected"]
    assert [s["node"] for s in r["split_brain"]["servers"]] == ["pg1", "pg2"]
    assert r["unexplained_rows"] == 0


def test_async_loss_counts_tail():
    pre, nxt = stream(1, A, 0.0, 10.0)
    post, _ = stream(1, B, 15.0, 20.0, seq0=nxt)
    events = pre + post
    # surviving primary is missing the last 2 seconds before the fault
    ledger = [k for e, k in zip(pre, keys(pre)) if e["t"] < 8.0] + keys(post)
    r = analyze(events, ledger, fault_t=10.0)

    assert r["rpo"]["lost_acked"] == sum(1 for e in pre if e["t"] >= 8.0)
    assert -2.02 < r["rpo"]["lost_first_s"] < -1.98
    assert r["rpo"]["lost_last_s"] <= 0


def test_in_doubt_committed_is_not_loss():
    pre, nxt = stream(1, A, 0.0, 5.0)
    err = {"ev": "err", "t": 5.0, "w": 1, "seq": nxt, "phase": "commit"}
    post, _ = stream(1, B, 7.0, 9.0, seq0=nxt + 1)
    events = pre + [err] + post
    ledger = keys(pre) + [(1, nxt)] + keys(post)
    r = analyze(events, ledger, fault_t=5.0)
    assert r["in_doubt"]["committed"] == 1
    assert r["rpo"]["lost_acked"] == 0
    assert r["unexplained_rows"] == 0


def test_read_only_rejection_is_not_in_doubt():
    pre, nxt = stream(1, A, 0.0, 5.0)
    errs = [
        {"ev": "err", "t": 5.0, "w": 1, "seq": nxt, "phase": "commit", "sqlstate": "25006"},
        {"ev": "err", "t": 5.1, "w": 1, "seq": nxt + 1, "phase": "commit", "sqlstate": "57P01"},
        {"ev": "err", "t": 5.2, "w": 1, "seq": nxt + 2, "phase": "commit", "sqlstate": None},
    ]
    post, _ = stream(1, B, 7.0, 9.0, seq0=nxt + 3)
    events = pre + errs + post
    r = analyze(events, keys(pre) + keys(post), fault_t=5.0)
    assert r["in_doubt"]["total"] == 2
    assert r["client"]["rejected"] == 1
    assert r["client"]["rejected_sqlstates"] == ["25006"]


def test_split_brain_overlap_detected():
    a, _ = stream(1, A, 0.0, 12.0)          # old primary keeps acking until 12s
    b, _ = stream(2, B, 9.0, 20.0)          # new primary starts at 9s
    events = a + b
    ledger = [k for e, k in zip(a, keys(a)) if e["t"] < 9.0] + keys(b)
    r = analyze(events, ledger, fault_t=8.0, ip_to_node=NODES)
    sb = r["split_brain"]
    assert sb["detected"]
    assert 290 < sb["overlap_acks"] < 310
    assert sb["overlap_lost"] == sb["overlap_acks"]


def test_no_recovery_is_open_outage():
    pre, _ = stream(1, A, 0.0, 10.0)
    events = pre + [{"ev": "stop", "t": 40.0, "hung_writers": 1}]
    r = analyze(events, keys(pre), fault_t=10.0)
    assert not r["rto"]["recovered"]
    assert 29.9 < r["rto"]["max_gap_s"] < 30.1
    assert r["client"]["hung_writers_at_stop"] == 1


def test_no_outage_when_writes_continue():
    pre, _ = stream(1, A, 0.0, 20.0)
    r = analyze(pre, keys(pre), fault_t=10.0)
    assert r["rto"]["max_gap_s"] < 0.02
    assert r["rto"]["downtime_s"] == 0
    assert r["rto"]["outages"] == 0
    assert r["rto"]["writes_resumed_s"] == 0.0
    assert 95 < r["client"]["commit_rate"] < 105  # one ack every 10 ms


def test_acks_just_after_fault_then_silence_is_not_recovered():
    # the fault time is taken a moment before the fault lands: a few acks after it
    # must not hide an outage that never ends
    pre, _ = stream(1, A, 0.0, 10.05)
    events = pre + [{"ev": "stopping", "t": 40.0}, {"ev": "stop", "t": 41.0, "hung_writers": 0}]
    r = analyze(events, keys(pre), fault_t=10.0)
    assert not r["rto"]["recovered"]
    assert 29.9 < r["rto"]["max_gap_s"] < 30.0
    assert r["rto"]["writes_resumed_s"] is None


def test_unread_ledger_reports_unknown_rpo():
    pre, _ = stream(1, A, 0.0, 10.0)
    r = analyze(pre, None, fault_t=5.0)
    assert r["rpo"]["lost_acked"] is None
    assert r["in_doubt"]["committed"] is None
    assert r["unexplained_rows"] is None


def test_inflight_at_stop_is_in_doubt():
    pre, nxt = stream(1, A, 0.0, 5.0)
    events = pre + [{"ev": "stopping", "t": 5.0}, {"ev": "inflight", "t": 15.0, "w": 1, "seq": nxt},
                    {"ev": "stop", "t": 15.0, "hung_writers": 1}]
    # the blocked COMMIT completed after all: its row is in the ledger, explained by "inflight"
    r = analyze(events, keys(pre) + [(1, nxt)], fault_t=4.0)
    assert r["in_doubt"] == {"total": 1, "committed": 1, "not_committed": 0}
    assert r["unexplained_rows"] == 0
