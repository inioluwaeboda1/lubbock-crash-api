"""Checks on the real processed dataset against what TxDOT says it exported.

If someone re-runs the pipeline on a new export and these numbers change,
that's expected. If they change without a new export, something broke.
"""

from crash_api.data import get_store
from crash_api import metrics


def test_crash_count_matches_txdot_header():
    store = get_store()
    assert len(store.crashes) == store.meta["reported_counts"]["crashes"] == 24_261


def test_every_person_row_was_read():
    store = get_store()
    assert store.meta["person_rows_read"] == store.meta["reported_counts"]["persons"] == 67_906


def test_crash_ids_are_unique():
    ids = [c["crash_id"] for c in get_store().crashes]
    assert len(ids) == len(set(ids))


def test_severity_counts():
    counts = {r["code"]: r["crashes"] for r in metrics.severity_counts(get_store().crashes)}
    assert counts == {"K": 624, "A": 1546, "B": 7224, "C": 14867}


def test_fatal_crashes_all_have_a_death():
    assert all(c["deaths"] > 0 for c in get_store().crashes if c["severity"] == "K")


def test_top_factor():
    top = metrics.top_factors(get_store().crashes, n=1)["factors"][0]
    assert top["factor"] == "FAILED TO CONTROL SPEED"
    assert top["crashes"] == 5559
