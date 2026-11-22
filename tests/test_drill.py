import json

from pgdrill.drill import last_ack_t, wait_writes_resumed


def _write(path, events):
    path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events))


def test_last_ack_t_skips_errors_and_missing_file(tmp_path):
    f = tmp_path / "events.jsonl"
    assert last_ack_t(f) is None
    _write(f, [
        {"ev": "ack", "t": 10.0, "w": 1, "seq": 1},
        {"ev": "ack", "t": 11.0, "w": 2, "seq": 1},
        {"ev": "err", "t": 12.0, "w": 1, "seq": 2, "phase": "commit"},
        {"ev": "err", "t": 12.2, "w": 1, "phase": "connect"},
    ])
    assert last_ack_t(f) == 11.0


def test_last_ack_t_survives_a_cut_first_line(tmp_path):
    f = tmp_path / "events.jsonl"
    _write(f, [{"ev": "ack", "t": float(i), "w": 1, "seq": i} for i in range(5000)])
    assert last_ack_t(f, tail_bytes=100) == 4999.0


def test_wait_writes_resumed(tmp_path):
    f = tmp_path / "events.jsonl"
    _write(f, [{"ev": "ack", "t": 5.0, "w": 1, "seq": 1}, {"ev": "err", "t": 9.0, "w": 1, "phase": "connect"}])
    assert not wait_writes_resumed(f, after_t=8.0, timeout=0.6)
    assert wait_writes_resumed(f, after_t=4.0, timeout=0.6)
