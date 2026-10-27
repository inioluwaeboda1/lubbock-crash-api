"""Tests for the metric functions (src/crash_api/metrics.py)."""

from datetime import date

from conftest import make_crash
from crash_api import metrics


def test_severity_counts_include_every_level_in_order():
    crashes = [make_crash(severity="K"), make_crash(severity="C"), make_crash(severity="C")]
    rows = metrics.severity_counts(crashes)
    assert [r["code"] for r in rows] == ["K", "A", "B", "C"]
    assert [r["crashes"] for r in rows] == [1, 0, 0, 2]
    assert rows[3]["share"] == round(2 / 3, 4)


def test_severity_counts_on_empty_input():
    assert all(r["crashes"] == 0 and r["share"] == 0 for r in metrics.severity_counts([]))


def test_top_factors_counts_crashes_not_mentions():
    crashes = [
        make_crash(factors=["SPEED", "INATTENTION"]),
        make_crash(factors=["SPEED"]),
        make_crash(factors=[]),
    ]
    result = metrics.top_factors(crashes, n=5)
    assert result["crashes_considered"] == 3
    assert result["crashes_with_a_factor"] == 2
    assert [(f["factor"], f["crashes"]) for f in result["factors"]] == [("SPEED", 2), ("INATTENTION", 1)]
    assert result["factors"][0]["share_of_crashes"] == round(2 / 3, 4)


def test_other_narrative_is_excluded_by_default_but_reported():
    other = "OTHER (EXPLAIN IN NARRATIVE)"
    crashes = [make_crash(factors=[other]), make_crash(factors=[other]), make_crash(factors=["SPEED"])]

    default = metrics.top_factors(crashes)
    assert [f["factor"] for f in default["factors"]] == ["SPEED"]
    assert default["unspecified_excluded"] == {other: 2}

    included = metrics.top_factors(crashes, include_unspecified=True)
    assert included["factors"][0]["factor"] == other


def test_top_factors_breaks_ties_alphabetically():
    crashes = [make_crash(factors=["B"]), make_crash(factors=["A"])]
    assert [f["factor"] for f in metrics.top_factors(crashes)["factors"]] == ["A", "B"]


def test_yearly_trend_flags_partial_year_and_fills_gaps():
    crashes = [
        make_crash(year=2022, date="2022-05-01", severity="K"),
        make_crash(year=2024, date="2024-05-01", severity="A"),
        make_crash(year=2024, date="2024-07-01", severity="C"),
    ]
    rows = metrics.trend(crashes, by="year", latest_date=date(2024, 7, 1))
    assert [r["period"] for r in rows] == [2022, 2023, 2024]
    assert rows[1]["total"] == 0                   # gap year shows as zero
    assert rows[2]["KA"] == 1 and rows[2]["total"] == 2
    assert [r["complete"] for r in rows] == [True, True, False]


def test_monthly_trend_fills_missing_months():
    crashes = [make_crash(date="2024-01-10"), make_crash(date="2024-03-02")]
    rows = metrics.trend(crashes, by="month")
    assert [r["period"] for r in rows] == ["2024-01", "2024-02", "2024-03"]
    assert rows[1]["total"] == 0


def test_hour_trend_always_has_24_rows():
    rows = metrics.trend([make_crash(hour=17)], by="hour")
    assert len(rows) == 24 and rows[17]["total"] == 1


def test_same_period_keeps_jan_1_to_latest_day_each_year():
    crashes = [make_crash(date="2024-10-20"), make_crash(date="2024-10-21"), make_crash(date="2025-02-01")]
    kept = metrics.same_period_only(crashes, latest_date=date(2026, 10, 20))
    assert [c["date"] for c in kept] == ["2024-10-20", "2025-02-01"]


def test_summary_headline_numbers():
    crashes = [
        make_crash(severity="K", deaths=2, road="US 84", pedestrian=True),
        make_crash(severity="A", serious_injuries=1, road="US 84"),
        make_crash(severity="C", road="Loop 289", lat=None, lon=None),
    ]
    s = metrics.summary(crashes)
    assert s["crashes"] == 3 and s["ka_crashes"] == 2 and s["deaths"] == 2
    assert s["pedestrian_crashes"] == 1
    assert s["crashes_with_location"] == 2
    assert s["top_roads_for_ka_crashes"] == [{"road": "US 84", "crashes": 2}]
