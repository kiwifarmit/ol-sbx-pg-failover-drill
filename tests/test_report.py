from pgdrill.report import render_markdown, verdict


def result(mode="async", lost=0, error=None, hung=0, recovered=True, rejoined=True, split=False):
    return {
        "scenario": "kill-primary", "mode": mode, "via": "haproxy", "error": error,
        "leader_before": "pg1", "leader_after": "pg2", "rejoined": rejoined, "marks": [],
        "analysis": {
            "rpo": {"lost_acked": lost, "lost_first_s": None, "lost_last_s": None},
            "rto": {"recovered": recovered, "max_gap_s": 20.0, "downtime_s": 20.0},
            "in_doubt": {"total": 4, "committed": 0},
            "split_brain": {"detected": split, "overlap_acks": 0},
            "client": {"hung_writers_at_stop": hung, "rejected": 0},
            "unexplained_rows": 0,
        },
    }


def test_clean_run_passes():
    assert verdict(result(), None, None) == (True, [])


def test_async_loss_only_fails_with_threshold():
    assert verdict(result(lost=10), None, None)[0]
    assert not verdict(result(lost=10), None, 0)[0]


def test_sync_loss_always_fails():
    for mode in ("sync", "strict"):
        ok, reasons = verdict(result(mode=mode, lost=1), None, None)
        assert not ok
        assert "synchronous mode" in reasons[0]


def test_scenario_error_fails_even_with_analysis():
    ok, reasons = verdict(result(error="scenario: timeout"), None, None)
    assert not ok and reasons[0].startswith("run error")


def test_hung_writers_and_unknown_rpo_fail():
    assert not verdict(result(hung=2), None, None)[0]
    assert not verdict(result(lost=None), None, None)[0]


def test_rto_threshold():
    assert not verdict(result(), 10, None)[0]
    assert verdict(result(), 30, None)[0]


def test_markdown_renders_unknown_rpo():
    md = render_markdown([result(lost=None)], {"timers": {}}, None, None)
    assert "20.0s / ?" in md
