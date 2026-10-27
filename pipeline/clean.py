"""
Clean the raw TxDOT CRIS export and collapse it to one row per crash.

The export is person-level: everyone involved in a crash gets their own row,
and all the crash-level columns are repeated on each of those rows. Every
question we care about (severity, contributing factors, trends) is about
crashes, not people, so the main job here is getting back to one row per
Crash ID without losing anything useful along the way.

The output is a small gzipped JSON file that the API loads at startup. Keeping
pandas on this side means the deployed Lambda only needs FastAPI + Mangum.

Run from the project root:
    python -m pipeline.clean                          # default input path
    python -m pipeline.clean path/to/export.csv       # any other CRIS export
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "raw" / "district_crashes.csv"
DEFAULT_OUTPUT = ROOT / "src" / "crash_api" / "data" / "crashes.json.gz"

# CRIS has no single way of saying "missing". These are all the spellings I
# found during EDA, compared after upper-casing and trimming.
MISSING_TOKENS = {"", "NO DATA", "N/A", "NA", "NOT REPORTED", "UNKNOWN", "-1"}

# Coded fields use "99 - UNKNOWN" style values for missing data.
UNKNOWN_CODE = re.compile(r"^9[79] - (UNKNOWN|NOT APPLICABLE)$")

SEVERITY_CODES = {"K", "A", "B", "C"}

# Rough bounding box for Texas. Anything outside it is a bad coordinate.
TX_LAT = (25.8, 36.6)
TX_LON = (-106.7, -93.5)

# CRIS road names come through as e.g. SL0289 or IH0027. These prefixes turn
# them into what people actually call the road (Loop 289, I-27).
ROAD_PREFIXES = {
    "IH": "I-",
    "US": "US ",
    "SH": "SH ",
    "SL": "Loop ",
    "LP": "Loop ",
    "FM": "FM ",
    "RM": "RM ",
    "SS": "Spur ",
    "SP": "Spur ",
    "BU": "Business US ",
    "BI": "Business I-",
    "BS": "Business SH ",
    "BF": "Business FM ",
    "CR": "County Road ",
}
ROAD_CODE = re.compile(r"^([A-Z]{2})0*(\d+)([A-Z]?)$")

# Columns that describe the crash itself. These must be identical on every
# person row of the same crash, and we check that before deduplicating.
CRASH_COLUMNS = [
    "Crash ID",
    "Crash Date",
    "Crash Year",
    "Crash Time",
    "County",
    "Crash Severity",
    "Contributing Factors",
    "Latitude",
    "Longitude",
    "Street Name",
    "Road Class",
    "Speed Limit",
    "Weather Condition",
    "First Harmful Event",
    "Crash Death Count",
    "Crash Suspected Serious Injury Count",
    "Crash Total Injury Count",
    "Rural Flag",
    "Intersection Related",
]


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def find_header_line(path: Path) -> int:
    """Return the 0-based line number of the real header row.

    The export starts with a disclaimer, query counts and the filters used.
    Rather than hard-coding "skip 11 lines" (which breaks the moment TxDOT
    changes the preamble), look for the line that starts with "Crash ID".
    """
    with open(path, encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            if line.lstrip('"').startswith("Crash ID"):
                return i
            if i > 100:
                break
    raise ValueError(f"Couldn't find a 'Crash ID' header row in {path}")


def read_preamble(path: Path) -> dict:
    """Pull the 'as of' date and TxDOT's own record counts out of the preamble.

    These are useful for two things: telling API users how fresh the data is,
    and checking that our crash count matches what TxDOT says it exported.
    """
    header_line = find_header_line(path)
    with open(path, encoding="utf-8-sig") as f:
        preamble = " ".join(next(f) for _ in range(header_line))

    info: dict = {}
    as_of = re.search(r"as of (\d{1,2}/\d{1,2}/\d{4})", preamble)
    if as_of:
        info["data_as_of"] = datetime.strptime(as_of.group(1), "%m/%d/%Y").date().isoformat()

    counts = re.search(
        r"([\d,]+) Crashes containing ([\d,]+) Units and ([\d,]+) Persons", preamble
    )
    if counts:
        crashes, units, persons = (int(x.replace(",", "")) for x in counts.groups())
        info["reported_counts"] = {"crashes": crashes, "units": units, "persons": persons}

    district = re.search(r"TxDOT District Is Equal To ([A-Z ]+)", preamble)
    if district:
        info["district"] = district.group(1).strip().title()
    return info


def read_raw(path: Path) -> pd.DataFrame:
    """Read the export with every column as text.

    keep_default_na=False stops pandas from guessing what counts as missing.
    CRIS uses "No Data", "N/A", -1 and others, and I'd rather handle all of
    those explicitly in one place than have some of them silently become NaN.
    """
    return pd.read_csv(
        path,
        skiprows=find_header_line(path),
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )


# ---------------------------------------------------------------------------
# Small cleaning helpers (kept as plain functions so they're easy to test)
# ---------------------------------------------------------------------------

def clean_text(value) -> str | None:
    """Trim, collapse inner whitespace, upper-case, and map missing tokens to None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip().upper()
    if text in MISSING_TOKENS or UNKNOWN_CODE.match(text):
        return None
    return text


def strip_code(value: str | None) -> str | None:
    """'1 - CLEAR' -> 'CLEAR'. Leaves values without a numeric code alone."""
    if not isinstance(value, str):
        return None
    return re.sub(r"^\d+\s*-\s*", "", value)


def parse_factors(value) -> list[str]:
    """Split the combined 'Contributing Factors' field into a clean list.

    About half the multi-factor crashes use ';' and the other half use '; ',
    so every piece gets trimmed. A factor listed twice for the same crash
    (two drivers both failing to yield, say) is only kept once, because the
    metric we report is "how many crashes cited this factor".
    'NONE' means the officer didn't record a factor, so it's dropped.
    """
    text = clean_text(value)
    if not text:
        return []
    factors = {part.strip() for part in text.split(";")}
    factors.discard("")
    factors.discard("NONE")
    return sorted(factors)


def to_int(value) -> int | None:
    text = clean_text(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def to_coordinate(value, bounds: tuple[float, float]) -> float | None:
    text = clean_text(value)
    if text is None:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    low, high = bounds
    return round(number, 6) if low <= number <= high else None


def parse_hour(value) -> int | None:
    """Crash Time is stored as 'HHMM' text (e.g. '0140'). Return the hour."""
    text = clean_text(value)
    if text is None or not text.isdigit():
        return None
    hour = int(text.zfill(4)[:2])
    return hour if 0 <= hour <= 23 else None


def road_label(value) -> str | None:
    """Turn CRIS road codes into readable names. City streets pass through as-is."""
    text = clean_text(value)
    if text is None:
        return None
    match = ROAD_CODE.match(text.replace(" ", ""))
    if match and match.group(1) in ROAD_PREFIXES:
        prefix, number, suffix = match.groups()
        return f"{ROAD_PREFIXES[prefix]}{number}{suffix}"
    return text


def title_case(value) -> str | None:
    """Title-case for display, but keep road-system abbreviations upper-case."""
    text = clean_text(value)
    if text is None:
        return None
    words = []
    for i, word in enumerate(text.split(" ")):
        if word in {"US", "FM", "RM", "SH", "IH"}:
            words.append(word)
        elif i > 0 and word in {"TO", "OF", "AND"}:
            words.append(word.lower())
        else:
            words.append(word.capitalize())
    return " ".join(words)


def yes_no(value) -> bool | None:
    text = clean_text(value)
    if text in {"YES", "Y", "TRUE", "1"}:
        return True
    if text in {"NO", "N", "FALSE", "0"}:
        return False
    return None


# ---------------------------------------------------------------------------
# The main transformation
# ---------------------------------------------------------------------------

def check_crash_columns_are_consistent(raw: pd.DataFrame) -> None:
    """Make sure crash-level columns really are constant within a crash.

    If they weren't, keeping "the first row" per crash would be arbitrary,
    so this is worth failing loudly on rather than assuming.
    """
    per_crash = raw[CRASH_COLUMNS].groupby("Crash ID").nunique()
    inconsistent = per_crash.columns[(per_crash > 1).any()]
    if len(inconsistent):
        raise ValueError(f"Crash-level columns vary within a crash: {list(inconsistent)}")


def vulnerable_user_flags(raw: pd.DataFrame) -> pd.DataFrame:
    """Flag crashes that involved a pedestrian, cyclist or motorcyclist.

    This is the one thing I take from the person-level rows. It's a common
    cut for safety work and you can't get it reliably from the crash columns.
    """
    person_type = raw["Person Type"].str.upper()
    flags = pd.DataFrame(
        {
            "Crash ID": raw["Crash ID"],
            "pedestrian": person_type.str.contains("PEDESTRIAN", na=False),
            "cyclist": person_type.str.contains("PEDALCYCLIST", na=False),
            "motorcyclist": person_type.str.contains("MOTORCYCLE", na=False),
        }
    )
    return flags.groupby("Crash ID").any().reset_index()


def build_crashes(raw: pd.DataFrame) -> pd.DataFrame:
    """Collapse person rows to crash rows and clean the columns we keep."""
    raw = raw.copy()
    raw["Crash ID"] = raw["Crash ID"].str.strip()
    raw = raw[raw["Crash ID"] != ""]

    check_crash_columns_are_consistent(raw)
    crashes = raw[CRASH_COLUMNS].drop_duplicates("Crash ID").copy()
    crashes = crashes.merge(vulnerable_user_flags(raw), on="Crash ID", how="left")

    severity = crashes["Crash Severity"].map(clean_text).str[0]
    bad_severity = ~severity.isin(SEVERITY_CODES)
    if bad_severity.any():
        # Shouldn't happen with this export (it's filtered to K/A/B/C), but if
        # it does I'd rather drop the row and say so than invent a severity.
        print(f"warning: dropping {bad_severity.sum()} crashes with no K/A/B/C severity")
        crashes, severity = crashes[~bad_severity], severity[~bad_severity]

    dates = pd.to_datetime(crashes["Crash Date"].str.strip(), errors="coerce")
    if dates.isna().any():
        print(f"warning: dropping {dates.isna().sum()} crashes with an unreadable date")
        keep = dates.notna()
        crashes, severity, dates = crashes[keep], severity[keep], dates[keep]

    out = pd.DataFrame(
        {
            "crash_id": crashes["Crash ID"],
            "date": dates.dt.strftime("%Y-%m-%d"),
            "year": dates.dt.year,
            "month": dates.dt.month,
            "hour": crashes["Crash Time"].map(parse_hour),
            "weekday": dates.dt.day_name(),
            "county": crashes["County"].map(title_case),
            "severity": severity,
            "factors": crashes["Contributing Factors"].map(parse_factors),
            "lat": crashes["Latitude"].map(lambda v: to_coordinate(v, TX_LAT)),
            "lon": crashes["Longitude"].map(lambda v: to_coordinate(v, TX_LON)),
            "road": crashes["Street Name"].map(road_label),
            "road_class": crashes["Road Class"].map(title_case),
            "speed_limit": crashes["Speed Limit"].map(to_int),
            "weather": crashes["Weather Condition"].map(lambda v: strip_code(clean_text(v))),
            "first_harmful_event": crashes["First Harmful Event"].map(clean_text),
            "deaths": crashes["Crash Death Count"].map(to_int).fillna(0).astype(int),
            "serious_injuries": crashes["Crash Suspected Serious Injury Count"].map(to_int).fillna(0).astype(int),
            "total_injuries": crashes["Crash Total Injury Count"].map(to_int).fillna(0).astype(int),
            "rural": crashes["Rural Flag"].map(yes_no),
            "intersection": crashes["Intersection Related"].map(clean_text),
            "pedestrian": crashes["pedestrian"].astype(bool),
            "cyclist": crashes["cyclist"].astype(bool),
            "motorcyclist": crashes["motorcyclist"].astype(bool),
        }
    )

    # A speed limit of 0 or 5 is almost certainly a parking lot or a typo.
    # I keep 5+ (school zones and private roads really do post low limits)
    # but treat 0 as missing.
    out.loc[out["speed_limit"] == 0, "speed_limit"] = None

    # Lat and lon should be missing together. If only one survived the bounds
    # check, the point is useless for mapping, so drop both.
    half_missing = out["lat"].isna() ^ out["lon"].isna()
    out.loc[half_missing, ["lat", "lon"]] = None

    return out.sort_values(["date", "crash_id"]).reset_index(drop=True)


def to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list of plain dicts with real None instead of NaN."""
    records = df.astype(object).where(df.notna(), None).to_dict(orient="records")
    for r in records:
        for key in ("year", "month", "hour", "speed_limit", "deaths", "serious_injuries", "total_injuries"):
            if r[key] is not None:
                r[key] = int(r[key])
    return records


def write_output(records: list[dict], meta: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "crashes": records}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))


def run(input_path: Path, output_path: Path) -> dict:
    meta = read_preamble(input_path)
    raw = read_raw(input_path)
    crashes = build_crashes(raw)

    meta.update(
        {
            "source": "TxDOT Crash Records Information System (CRIS)",
            "source_file": input_path.name,
            "person_rows_read": int(len(raw)),
            "crash_count": int(len(crashes)),
            "first_crash_date": crashes["date"].min(),
            "latest_crash_date": crashes["date"].max(),
            "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    )

    # Reconcile with TxDOT's own counts. Units can't be checked because the
    # export has no unit/vehicle ID column.
    reported = meta.get("reported_counts", {})
    if reported:
        for name, ours in (("crashes", len(crashes)), ("persons", len(raw))):
            status = "ok" if ours == reported[name] else "MISMATCH"
            print(f"{name:>8}: ours={ours:,}  txdot={reported[name]:,}  {status}")

    write_output(to_records(crashes), meta, output_path)
    shown = output_path.relative_to(ROOT) if output_path.is_relative_to(ROOT) else output_path
    print(f"wrote {len(crashes):,} crashes to {shown}")
    return meta


def print_headline_metrics(output_path: Path) -> None:
    """Print the three required metrics using the same code the API uses."""
    sys.path.insert(0, str(ROOT / "src"))
    from crash_api import metrics
    from crash_api.data import CrashStore

    store = CrashStore.from_file(output_path)
    crashes = store.crashes

    print("\nCrashes by severity")
    for row in metrics.severity_counts(crashes):
        print(f"  {row['code']}  {row['label']:<26} {row['crashes']:>6,}  {row['share']:.1%}")

    print("\nTop 5 contributing factors (crashes citing each)")
    for row in metrics.top_factors(crashes, n=5)["factors"]:
        print(f"  {row['crashes']:>6,}  {row['factor']}")

    print("\nCrashes by year")
    for row in metrics.trend(crashes, by="year", latest_date=store.latest_date):
        flag = "" if row.get("complete", True) else "  (partial year)"
        print(f"  {row['period']}  {row['total']:>6,}   KA: {row['KA']:>4}{flag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(f"Input file not found: {args.input}\nPut the CRIS export at data/raw/district_crashes.csv")

    run(args.input, args.output)
    print_headline_metrics(args.output)


if __name__ == "__main__":
    main()
