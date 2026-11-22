import pytest

from pgdrill.faults import SCENARIOS, lsn_to_int


def test_lsn_to_int():
    assert lsn_to_int("0/0") == 0
    assert lsn_to_int("0/3000060") == 0x3000060
    assert lsn_to_int("16/B374D848\n") == (0x16 << 32) | 0xB374D848
    # the difference of two LSNs is the WAL distance in bytes
    assert lsn_to_int("1/0") - lsn_to_int("0/FFFFFC00") == 1024


@pytest.mark.parametrize("bad", ["", "not-an-lsn", "0/xyz"])
def test_lsn_to_int_rejects_garbage(bad):
    with pytest.raises(ValueError):
        lsn_to_int(bad)


def test_every_scenario_names_an_oracle_analogue():
    for s in SCENARIOS.values():
        assert s.summary and s.oracle
