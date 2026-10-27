"""Shared test fixtures.

Most tests run against a tiny hand-made CRIS-style file so the expected
answers are obvious. A couple of tests also check the real processed data
against TxDOT's own published counts.
"""

import textwrap

import pytest

# Two crashes, five person rows. Crash 100 has three people and two
# contributing factors written with the "; " spacing; crash 200 has two
# people, "No Data" coordinates and a factor listed twice.
SAMPLE_CSV = textwrap.dedent(
    '''\
    "All crash data available using this tool represents reportable data ... as of 10/22/2026. ..."

    "Query Result Counts:"
    "Your query returned a total of 2 Crashes containing 3 Units and 5 Persons"

    "Filters Applied to current Query:"
    "TxDOT District Is Equal To LUBBOCK"


    "Crash ID","Crash Date","Crash Year","Crash Time","County","Crash Severity","Contributing Factors","Latitude","Longitude","Street Name","Road Class","Speed Limit","Weather Condition","First Harmful Event","Crash Death Count","Crash Suspected Serious Injury Count","Crash Total Injury Count","Rural Flag","Intersection Related","Person Type","Person Age"
    "100","2025-03-04","2025","0140","LUBBOCK","K - FATAL INJURY","FAILED TO CONTROL SPEED; UNSAFE SPEED","33.5","-101.8","SL0289","US & STATE HIGHWAYS","-1","1 - CLEAR","MOTOR VEHICLE IN TRANSPORT","1","0","1","No","INTERSECTION","1 - DRIVER","34"
    "100","2025-03-04","2025","0140","LUBBOCK","K - FATAL INJURY","FAILED TO CONTROL SPEED; UNSAFE SPEED","33.5","-101.8","SL0289","US & STATE HIGHWAYS","-1","1 - CLEAR","MOTOR VEHICLE IN TRANSPORT","1","0","1","No","INTERSECTION","2 - PASSENGER/OCCUPANT","12"
    "100","2025-03-04","2025","0140","LUBBOCK","K - FATAL INJURY","FAILED TO CONTROL SPEED; UNSAFE SPEED","33.5","-101.8","SL0289","US & STATE HIGHWAYS","-1","1 - CLEAR","MOTOR VEHICLE IN TRANSPORT","1","0","1","No","INTERSECTION","4 - PEDESTRIAN","50"
    "200","2026-01-15","2026","1715","hale","c - possible injury","DRIVER INATTENTION;DRIVER INATTENTION;NONE","No Data","No Data","main st","CITY STREET","30","99 - UNKNOWN","PARKED CAR","0","0","1","Yes","NON INTERSECTION","1 - DRIVER","No Data"
    "200","2026-01-15","2026","1715","hale","c - possible injury","DRIVER INATTENTION;DRIVER INATTENTION;NONE","No Data","No Data","main st","CITY STREET","30","99 - UNKNOWN","PARKED CAR","0","0","1","Yes","NON INTERSECTION","1 - DRIVER","41"
    '''
)


@pytest.fixture
def sample_csv(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("﻿" + SAMPLE_CSV, encoding="utf-8")  # real exports start with a BOM
    return path


def make_crash(**overrides):
    """A cleaned crash record with sensible defaults, for metric tests."""
    crash = {
        "crash_id": "1", "date": "2024-06-01", "year": 2024, "month": 6, "hour": 12, "weekday": "Saturday",
        "county": "Lubbock", "severity": "C", "factors": [], "lat": 33.5, "lon": -101.8,
        "road": "Loop 289", "road_class": "US & State Highways", "speed_limit": 45, "weather": "CLEAR",
        "first_harmful_event": "MOTOR VEHICLE IN TRANSPORT", "deaths": 0, "serious_injuries": 0,
        "total_injuries": 1, "rural": False, "intersection": "INTERSECTION",
        "pedestrian": False, "cyclist": False, "motorcyclist": False,
    }
    crash.update(overrides)
    return crash
