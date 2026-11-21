"""End-to-end: bring up the lab and run one switchover drill. Needs podman.

    uv run pytest -m podman
"""

from pathlib import Path

import pytest

from pgdrill.drill import run_one
from pgdrill.lab import MODES, Lab
from pgdrill.report import verdict


@pytest.mark.podman
def test_switchover_sync_loses_nothing():
    workdir = Path(".pgdrill-test").resolve()  # must be under a podman-shared path on macOS
    lab = Lab(workdir)
    try:
        r = run_one(lab, "switchover", MODES["sync"], "haproxy", workdir / "runs")
        a = r["analysis"]
        assert r["leader_after"] != r["leader_before"]
        assert a["rpo"]["lost_acked"] == 0
        assert a["rto"]["recovered"]
        assert not a["split_brain"]["detected"]
        assert a["unexplained_rows"] == 0
        ok, reasons = verdict(r, max_rto=30, max_rpo=0)
        assert ok, reasons
    finally:
        lab.down()
