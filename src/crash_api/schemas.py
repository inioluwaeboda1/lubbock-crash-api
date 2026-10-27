"""
Response shapes for the API.

These exist mainly so /docs and /openapi.json describe what each endpoint
returns, which makes the API easier to build a dashboard (or generate a
client) against. The field names match what metrics.py already produces.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# -- shared --------------------------------------------------------------------

class AppliedFilters(BaseModel):
    """The filters actually applied, after normalising (e.g. 'lubbock' -> 'Lubbock')."""
    county: list[str] | None
    severity: list[str] | None
    year_from: int | None
    year_to: int | None
    road_class: list[str] | None


class Envelope(BaseModel):
    filters: AppliedFilters
    crash_count: int = Field(description="Number of crashes behind this answer.")


# -- /api/severity ---------------------------------------------------------------

class SeverityRow(BaseModel):
    code: str = Field(examples=["K"])
    label: str = Field(examples=["Fatal"])
    crashes: int
    share: float = Field(description="Share of crash_count, 0 to 1.")


class SeverityResponse(Envelope):
    data: list[SeverityRow]


# -- /api/factors ----------------------------------------------------------------

class FactorRow(BaseModel):
    rank: int
    factor: str = Field(examples=["FAILED TO CONTROL SPEED"])
    crashes: int = Field(description="Crashes that cited this factor (each crash counts once).")
    share_of_crashes: float = Field(description="Out of all crashes in the filter, including ones with no factor.")


class FactorsResponse(Envelope):
    crashes_considered: int
    crashes_with_a_factor: int
    unspecified_excluded: dict[str, int] = Field(
        description="Factors left out of the ranking (e.g. 'OTHER (EXPLAIN IN NARRATIVE)') and how many crashes cited them."
    )
    data: list[FactorRow]


# -- /api/trends -----------------------------------------------------------------

class TrendRow(BaseModel):
    period: int | str = Field(description="Year (2024), month ('2024-06'), hour (0-23) or weekday ('Monday').")
    K: int
    A: int
    B: int
    C: int
    KA: int = Field(description="Fatal + serious injury crashes.")
    total: int
    complete: bool | None = Field(
        default=None,
        description="Only for by=year or by=month without same_period. False for the period the data ends in.",
    )


class TrendsResponse(Envelope):
    by: str
    same_period: bool
    notes: list[str]
    data: list[TrendRow]


# -- /api/summary ----------------------------------------------------------------

class DateRange(BaseModel):
    first: str
    last: str


class RoadCount(BaseModel):
    road: str = Field(examples=["US 84"])
    crashes: int


class Summary(BaseModel):
    crashes: int
    fatal_crashes: int
    serious_injury_crashes: int
    ka_crashes: int
    ka_share: float
    deaths: int = Field(description="People killed. Can exceed fatal_crashes, since one crash can kill several people.")
    serious_injuries: int = Field(description="People seriously injured.")
    pedestrian_crashes: int
    cyclist_crashes: int
    motorcycle_crashes: int
    crashes_with_location: int
    date_range: DateRange | None
    top_roads_for_ka_crashes: list[RoadCount]


class SummaryResponse(Envelope):
    data: Summary


# -- /api/crashes ----------------------------------------------------------------

class Crash(BaseModel):
    """One crash. Crash-level fields only; no personal details."""
    crash_id: str
    date: str
    hour: int | None
    county: str | None
    severity: str
    road: str | None
    road_class: str | None
    speed_limit: int | None
    factors: list[str]
    lat: float | None
    lon: float | None
    deaths: int
    serious_injuries: int
    pedestrian: bool
    cyclist: bool
    motorcyclist: bool


class Pagination(BaseModel):
    limit: int
    offset: int
    returned: int
    next_offset: int | None = Field(description="Pass this as offset for the next page. Null on the last page.")


class CrashesResponse(Envelope):
    has_location: bool | None
    pagination: Pagination
    data: list[Crash]


# -- /api/meta -------------------------------------------------------------------

class CountyOption(BaseModel):
    name: str
    crashes: int


class SeverityOption(BaseModel):
    code: str
    label: str


class ValidFilters(BaseModel):
    county: list[CountyOption]
    severity: list[SeverityOption]
    year: list[int]
    road_class: list[str]


class MetaResponse(BaseModel):
    source: str | None
    district: str | None
    data_as_of: str | None
    first_crash_date: str | None
    latest_crash_date: str | None
    crash_count: int
    valid_filters: ValidFilters
    notes: list[str]
