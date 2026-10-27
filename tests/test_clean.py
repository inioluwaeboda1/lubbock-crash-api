"""Tests for the cleaning pipeline (pipeline/clean.py)."""

import pytest

from pipeline import clean


def test_finds_header_after_the_preamble(sample_csv):
    # The header is on line 9 (0-based) of the sample, after the preamble.
    assert clean.find_header_line(sample_csv) == 9


def test_reads_txdot_counts_and_as_of_date(sample_csv):
    info = clean.read_preamble(sample_csv)
    assert info["data_as_of"] == "2026-10-22"
    assert info["reported_counts"] == {"crashes": 2, "units": 3, "persons": 5}
    assert info["district"] == "Lubbock"


def test_collapses_person_rows_to_one_row_per_crash(sample_csv):
    crashes = clean.build_crashes(clean.read_raw(sample_csv))
    assert len(crashes) == 2
    assert sorted(crashes["crash_id"]) == ["100", "200"]


def cleaned_records(path):
    """What actually gets written to disk, keyed by crash ID."""
    records = clean.to_records(clean.build_crashes(clean.read_raw(path)))
    return {r["crash_id"]: r for r in records}


def test_crash_level_fields_are_cleaned(sample_csv):
    crashes = cleaned_records(sample_csv)

    fatal = crashes["100"]
    assert fatal["severity"] == "K"
    assert fatal["hour"] == 1
    assert fatal["road"] == "Loop 289"
    assert fatal["road_class"] == "US & State Highways"
    assert fatal["speed_limit"] is None      # -1 means unknown
    assert fatal["pedestrian"]  # picked up from the person rows

    minor = crashes["200"]
    assert minor["severity"] == "C"          # lower-case source value
    assert minor["county"] == "Hale"         # lower-case source value
    assert minor["weather"] is None          # "99 - UNKNOWN"
    assert minor["lat"] is None and minor["lon"] is None   # "No Data"
    assert not minor["pedestrian"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("FAILED TO CONTROL SPEED;UNSAFE SPEED", ["FAILED TO CONTROL SPEED", "UNSAFE SPEED"]),
        ("FAILED TO CONTROL SPEED; UNSAFE SPEED", ["FAILED TO CONTROL SPEED", "UNSAFE SPEED"]),
        ("  driver inattention ;DRIVER INATTENTION", ["DRIVER INATTENTION"]),  # duplicate + casing
        ("NONE", []),
        ("No Data", []),
        ("", []),
        (None, []),
    ],
)
def test_parse_factors(raw, expected):
    assert clean.parse_factors(raw) == expected


def test_factor_listed_twice_counts_once_per_crash(sample_csv):
    assert cleaned_records(sample_csv)["200"]["factors"] == ["DRIVER INATTENTION"]


@pytest.mark.parametrize(
    "raw, expected",
    [("SL0289", "Loop 289"), ("IH0027", "I-27"), ("US0062", "US 62"), ("FM1585", "FM 1585"),
     ("SLIDE RD", "SLIDE RD"), ("No Data", None)],
)
def test_road_label(raw, expected):
    assert clean.road_label(raw) == expected


@pytest.mark.parametrize("raw", ["", "No Data", "NO DATA", "N/A", "NOT REPORTED", "-1", "99 - UNKNOWN", None])
def test_missing_tokens_become_none(raw):
    assert clean.clean_text(raw) is None


def test_out_of_texas_coordinates_are_dropped():
    assert clean.to_coordinate("33.5", clean.TX_LAT) == 33.5
    assert clean.to_coordinate("0", clean.TX_LAT) is None
    assert clean.to_coordinate("-101.8", clean.TX_LON) == -101.8
    assert clean.to_coordinate("101.8", clean.TX_LON) is None  # sign flipped


def test_hour_parsing():
    assert clean.parse_hour("0140") == 1
    assert clean.parse_hour("140") == 1
    assert clean.parse_hour("2359") == 23
    assert clean.parse_hour("2500") is None
    assert clean.parse_hour("No Data") is None


def test_inconsistent_crash_rows_fail_loudly(sample_csv):
    raw = clean.read_raw(sample_csv)
    raw.loc[0, "Crash Severity"] = "A - SUSPECTED SERIOUS INJURY"  # now crash 100 disagrees with itself
    with pytest.raises(ValueError, match="Crash Severity"):
        clean.build_crashes(raw)


def test_full_run_writes_a_file_the_api_can_load(sample_csv, tmp_path):
    from crash_api.data import CrashStore

    out = tmp_path / "crashes.json.gz"
    meta = clean.run(sample_csv, out)
    store = CrashStore.from_file(out)
    assert meta["crash_count"] == 2
    assert len(store.crashes) == 2
    assert store.meta["latest_crash_date"] == "2026-01-15"
