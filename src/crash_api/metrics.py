"""
The actual numbers. Every function takes a list of cleaned crash dicts (one
per crash, already filtered) and returns plain JSON-friendly data.

Both the API and pipeline/clean.py call these, so there's exactly one
definition of each metric.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

SEVERITY_ORDER = ("K", "A", "B", "C")
SEVERITY_LABELS = {
    "K": "Fatal",
    "A": "Suspected serious injury",
    "B": "Suspected minor injury",
    "C": "Possible injury",
}

# "OTHER (EXPLAIN IN NARRATIVE)" means the real reason is in the officer's
# free-text narrative, which isn't in this export. It's a real entry on the
# form, but ranking it alongside actual behaviours isn't very useful to an
# analyst, so it's excluded from the top-N by default (and reported
# separately so nothing is hidden).
UNSPECIFIED_FACTORS = {"OTHER (EXPLAIN IN NARRATIVE)"}

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _share(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _severity_breakdown(crashes: list[dict]) -> dict:
    counts = Counter(c["severity"] for c in crashes)
    row = {code: counts.get(code, 0) for code in SEVERITY_ORDER}
    row["KA"] = row["K"] + row["A"]
    row["total"] = len(crashes)
    return row


# ---------------------------------------------------------------------------
# 1. Severity
# ---------------------------------------------------------------------------

def severity_counts(crashes: list[dict]) -> list[dict]:
    """Crash counts for K, A, B and C, always in that order (zeros included)."""
    counts = Counter(c["severity"] for c in crashes)
    total = len(crashes)
    return [
        {
            "code": code,
            "label": SEVERITY_LABELS[code],
            "crashes": counts.get(code, 0),
            "share": _share(counts.get(code, 0), total),
        }
        for code in SEVERITY_ORDER
    ]


# ---------------------------------------------------------------------------
# 2. Contributing factors
# ---------------------------------------------------------------------------

def top_factors(crashes: list[dict], n: int = 5, include_unspecified: bool = False) -> dict:
    """Rank contributing factors by the number of crashes that cite them.

    A crash with three factors counts once for each of them, but a factor
    can't count twice for the same crash (that's handled in cleaning). Shares
    are out of all crashes in the filter, including ones with no factor
    recorded, so they read as "x% of crashes involved ...".
    """
    counts: Counter = Counter()
    with_any_factor = 0
    for crash in crashes:
        factors = crash["factors"]
        if factors:
            with_any_factor += 1
        counts.update(factors)

    unspecified = {f: counts.pop(f) for f in list(counts) if f in UNSPECIFIED_FACTORS}
    if include_unspecified:
        counts.update(unspecified)

    # Sort by count, then name, so ties come out in a stable order.
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:n]
    total = len(crashes)
    return {
        "crashes_considered": total,
        "crashes_with_a_factor": with_any_factor,
        "unspecified_excluded": {} if include_unspecified else unspecified,
        "factors": [
            {"rank": i, "factor": factor, "crashes": count, "share_of_crashes": _share(count, total)}
            for i, (factor, count) in enumerate(ranked, start=1)
        ],
    }


# ---------------------------------------------------------------------------
# 3. Time patterns
# ---------------------------------------------------------------------------

def same_period_only(crashes: list[dict], latest_date: date) -> list[dict]:
    """Keep only crashes from Jan 1 up to the latest month/day in the data.

    The export stops partway through the current year, so a straight
    year-over-year chart makes the last year look like a big drop. Comparing
    the same window in every year is the fair comparison.
    """
    cutoff = latest_date.strftime("%m-%d")
    return [c for c in crashes if c["date"][5:] <= cutoff]


def trend(crashes: list[dict], by: str = "year", latest_date: date | None = None) -> list[dict]:
    """Crash counts over time, split by severity.

    by = "year" | "month" | "hour" | "weekday"
    Each row has total, K, A, B, C and KA. For years and months, the period
    containing latest_date is marked complete=False.
    """
    if by == "year":
        groups = _group(crashes, lambda c: c["year"])
        periods = range(min(groups), max(groups) + 1) if groups else []
        rows = [{"period": year, **_severity_breakdown(groups.get(year, []))} for year in periods]
        if latest_date:
            for row in rows:
                row["complete"] = not (row["period"] == latest_date.year and latest_date.strftime("%m-%d") != "12-31")
        return rows

    if by == "month":
        groups = _group(crashes, lambda c: c["date"][:7])
        rows = [{"period": month, **_severity_breakdown(groups.get(month, []))} for month in _month_range(groups)]
        if latest_date:
            current = latest_date.strftime("%Y-%m")
            for row in rows:
                row["complete"] = row["period"] != current
        return rows

    if by == "hour":
        groups = _group(crashes, lambda c: c["hour"])
        return [{"period": hour, **_severity_breakdown(groups.get(hour, []))} for hour in range(24)]

    if by == "weekday":
        groups = _group(crashes, lambda c: c["weekday"])
        return [{"period": day, **_severity_breakdown(groups.get(day, []))} for day in WEEKDAYS]

    raise ValueError(f"Unknown trend axis: {by!r}")


def _group(crashes: list[dict], key) -> dict:
    groups: dict = {}
    for crash in crashes:
        k = key(crash)
        if k is not None:
            groups.setdefault(k, []).append(crash)
    return groups


def _month_range(groups: dict) -> list[str]:
    """Every month between the first and last one present, so gaps show as zeros."""
    if not groups:
        return []
    first, last = min(groups), max(groups)
    year, month = int(first[:4]), int(first[5:])
    months = []
    while f"{year:04d}-{month:02d}" <= last:
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


# ---------------------------------------------------------------------------
# Summary (the "headline numbers" a dashboard would show first)
# ---------------------------------------------------------------------------

def top_roads(crashes: list[dict], n: int = 5) -> list[dict]:
    counts = Counter(c["road"] for c in crashes if c["road"])
    return [{"road": road, "crashes": count} for road, count in counts.most_common(n)]


def summary(crashes: list[dict]) -> dict:
    breakdown = _severity_breakdown(crashes)
    ka = [c for c in crashes if c["severity"] in ("K", "A")]
    dates = [c["date"] for c in crashes]
    return {
        "crashes": breakdown["total"],
        "fatal_crashes": breakdown["K"],
        "serious_injury_crashes": breakdown["A"],
        "ka_crashes": breakdown["KA"],
        "ka_share": _share(breakdown["KA"], breakdown["total"]),
        "deaths": sum(c["deaths"] for c in crashes),
        "serious_injuries": sum(c["serious_injuries"] for c in crashes),
        "pedestrian_crashes": sum(1 for c in crashes if c["pedestrian"]),
        "cyclist_crashes": sum(1 for c in crashes if c["cyclist"]),
        "motorcycle_crashes": sum(1 for c in crashes if c["motorcyclist"]),
        "crashes_with_location": sum(1 for c in crashes if c["lat"] is not None),
        "date_range": {"first": min(dates), "last": max(dates)} if dates else None,
        "top_roads_for_ka_crashes": top_roads(ka, n=5),
    }
