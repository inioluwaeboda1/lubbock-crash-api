"""
REST API over the cleaned Lubbock district crash data.

Every /api endpoint accepts the same filters (county, severity, year range,
road class) and returns the same envelope:

    {
      "filters": {...what was applied...},
      "crash_count": 1234,          # crashes matching the filters
      "data": ...                   # the actual answer
    }

so a dashboard can wire up one set of controls and reuse it everywhere.

Locally:   uvicorn crash_api.main:app --app-dir src --reload
On AWS:    Lambda calls `handler` (Mangum translates the function URL event).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from mangum import Mangum
from pydantic import BaseModel

from . import metrics, schemas
from .data import SEVERITY_CODES, get_store

STATIC_DIR = Path(__file__).parent / "static"

NOTES = [
    "Injury crashes only (K, A, B, C). Property-damage-only crashes were filtered out of the source export.",
    "Counts are crashes, not people or vehicles. The source file has one row per person; it's collapsed to one row per crash.",
]

def _query_param_names(dependant) -> set[str]:
    """All query parameter names a route accepts, including ones from Depends()."""
    names = {param.alias for param in dependant.query_params}
    for sub in dependant.dependencies:
        names |= _query_param_names(sub)
    return names


def reject_unknown_params(request: Request) -> None:
    """Return a 400 for query parameters an endpoint doesn't accept.

    Without this, a typo like ?countys=Hale is silently ignored and the
    caller gets district-wide numbers that look like Hale's.
    """
    route = request.scope.get("route")
    if not request.url.path.startswith("/api") or not hasattr(route, "dependant"):
        return
    allowed = _query_param_names(route.dependant)
    unknown = sorted(set(request.query_params) - allowed)
    if unknown:
        message = f"Unknown query parameter: {', '.join(unknown)}"
        if not allowed:
            message += ". This endpoint doesn't take any query parameters."
        raise HTTPException(status_code=400, detail={"message": message, "valid_values": sorted(allowed)})


app = FastAPI(
    dependencies=[Depends(reject_unknown_params)],
    title="Lubbock District Crash API",
    version="1.0.0",
    description=(
        "Injury crash statistics for the TxDOT Lubbock district, 2020 to Oct 2026, "
        "built from a TxDOT CRIS export. All /api endpoints share the same filters."
    ),
)

# CORS is handled here rather than on the Lambda function URL. Turning it on
# in both places sends duplicate Access-Control-Allow-Origin headers, which
# browsers reject.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)


class ErrorDetail(BaseModel):
    message: str
    valid_values: list | None = None
    errors: list[dict] | None = None


class ErrorResponse(BaseModel):
    detail: ErrorDetail


# Documented on every /api endpoint so the OpenAPI spec shows both error cases.
ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Unknown filter value or parameter. `valid_values` lists the options."},
    422: {"model": ErrorResponse, "description": "A parameter has the wrong type or is out of range."},
}


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    """Give FastAPI's own validation errors the same shape as our 400s."""
    errors = [
        {"parameter": str(err["loc"][-1]), "problem": err["msg"]}
        for err in exc.errors()
    ]
    message = "; ".join(f"{e['parameter']}: {e['problem']}" for e in errors)
    return JSONResponse(status_code=422, content={"detail": {"message": message, "errors": errors}})


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    # The data only changes when the pipeline is re-run and redeployed, so
    # it's safe to let browsers cache responses for a while.
    response = await call_next(request)
    if request.url.path.startswith("/api") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# ---------------------------------------------------------------------------
# Shared filters
# ---------------------------------------------------------------------------

@dataclass
class Filters:
    county: list[str] | None
    severity: list[str] | None
    year_from: int | None
    year_to: int | None
    road_class: list[str] | None


def _split(values: list[str] | None) -> list[str] | None:
    """Accept both ?county=Lubbock&county=Hale and ?county=Lubbock,Hale."""
    if not values:
        return None
    parts = [p.strip() for v in values for p in v.split(",") if p.strip()]
    return parts or None


def _check_allowed(name: str, values: list[str] | None, allowed: list[str]) -> list[str] | None:
    """Reject unknown values with a list of valid ones, and normalise casing."""
    if not values:
        return None
    lookup = {a.lower(): a for a in allowed}
    unknown = [v for v in values if v.lower() not in lookup]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unknown {name}: {', '.join(unknown)}", "valid_values": allowed},
        )
    return [lookup[v.lower()] for v in values]


def parse_severity(raw: str | None) -> list[str] | None:
    """'KA', 'K,A', 'k' and 'A, B' all work."""
    if not raw:
        return None
    codes = [ch for ch in raw.upper() if ch not in ", "]
    bad = [c for c in codes if c not in SEVERITY_CODES]
    if bad or not codes:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Severity must be made of K, A, B, C (got {raw!r})", "valid_values": list(SEVERITY_CODES)},
        )
    return sorted(set(codes), key=SEVERITY_CODES.index)


def crash_filters(
    county: list[str] | None = Query(None, description="County name. Repeat or comma-separate for several.", examples=["Lubbock"]),
    severity: str | None = Query(None, description="Any of K, A, B, C. 'KA' = fatal + serious injury.", examples=["KA"]),
    year_from: int | None = Query(None, ge=2000, le=2100, description="First year to include."),
    year_to: int | None = Query(None, ge=2000, le=2100, description="Last year to include."),
    road_class: list[str] | None = Query(None, description="e.g. 'City Street', 'Interstate'. Repeat or comma-separate."),
) -> Filters:
    store = get_store()
    if year_from and year_to and year_from > year_to:
        raise HTTPException(
            status_code=400,
            detail={"message": f"year_from ({year_from}) is after year_to ({year_to})", "valid_values": store.years},
        )
    first, last = store.years[0], store.years[-1]
    if (year_from and year_from > last) or (year_to and year_to < first):
        # A range that only partly overlaps (e.g. year_from=2019) is fine.
        # One with no overlap at all is almost certainly a mistake, and an
        # empty 200 would read as "zero crashes".
        raise HTTPException(
            status_code=400,
            detail={"message": f"No data for that year range. The data covers {first} to {last}.", "valid_values": store.years},
        )
    return Filters(
        county=_check_allowed("county", _split(county), store.counties),
        severity=parse_severity(severity),
        year_from=year_from,
        year_to=year_to,
        road_class=_check_allowed("road_class", _split(road_class), store.road_classes),
    )


def filtered(f: Filters) -> list[dict]:
    return get_store().filter(
        counties=f.county,
        severities=f.severity,
        year_from=f.year_from,
        year_to=f.year_to,
        road_classes=f.road_class,
    )


def envelope(f: Filters, crashes: list[dict], data, **extra) -> dict:
    return {"filters": asdict(f), "crash_count": len(crashes), **extra, "data": data}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/meta", response_model=schemas.MetaResponse, response_model_exclude_unset=True, tags=["reference"], responses={400: ERROR_RESPONSES[400]}, summary="What's in the dataset and which filter values are valid")
def meta():
    store = get_store()
    counts = {}
    for crash in store.crashes:
        counts[crash["county"]] = counts.get(crash["county"], 0) + 1
    return {
        "source": store.meta.get("source"),
        "district": store.meta.get("district"),
        "data_as_of": store.meta.get("data_as_of"),
        "first_crash_date": store.meta.get("first_crash_date"),
        "latest_crash_date": store.meta.get("latest_crash_date"),
        "crash_count": len(store.crashes),
        "valid_filters": {
            "county": [{"name": c, "crashes": counts[c]} for c in sorted(counts, key=lambda c: -counts[c])],
            "severity": [{"code": c, "label": metrics.SEVERITY_LABELS[c]} for c in SEVERITY_CODES],
            "year": store.years,
            "road_class": store.road_classes,
        },
        "notes": NOTES,
    }


@app.get("/api/summary", response_model=schemas.SummaryResponse, response_model_exclude_unset=True, tags=["metrics"], responses=ERROR_RESPONSES, summary="Headline numbers for the current filters")
def summary(f: Filters = Depends(crash_filters)):
    crashes = filtered(f)
    return envelope(f, crashes, metrics.summary(crashes))


@app.get("/api/severity", response_model=schemas.SeverityResponse, response_model_exclude_unset=True, tags=["metrics"], responses=ERROR_RESPONSES, summary="Crash counts by severity (K, A, B, C)")
def severity(f: Filters = Depends(crash_filters)):
    crashes = filtered(f)
    return envelope(f, crashes, metrics.severity_counts(crashes))


@app.get("/api/factors", response_model=schemas.FactorsResponse, response_model_exclude_unset=True, tags=["metrics"], responses=ERROR_RESPONSES, summary="Most common contributing factors")
def factors(
    f: Filters = Depends(crash_filters),
    n: int = Query(5, ge=1, le=50, description="How many factors to return."),
    include_unspecified: bool = Query(False, description="Include 'OTHER (EXPLAIN IN NARRATIVE)' in the ranking."),
):
    crashes = filtered(f)
    result = metrics.top_factors(crashes, n=n, include_unspecified=include_unspecified)
    data = result.pop("factors")
    return envelope(f, crashes, data, **result)


@app.get("/api/trends", response_model=schemas.TrendsResponse, response_model_exclude_unset=True, tags=["metrics"], responses=ERROR_RESPONSES, summary="Crash counts over time, split by severity")
def trends(
    f: Filters = Depends(crash_filters),
    by: Literal["year", "month", "hour", "weekday"] = Query("year", description="Time axis."),
    same_period: bool = Query(
        False,
        description="Only with by=year. Counts Jan 1 up to the latest date in the data, in every year, for a fair year-over-year comparison.",
    ),
):
    store = get_store()
    if same_period and by != "year":
        raise HTTPException(
            status_code=400,
            detail={
                "message": "same_period only works with by=year. It compares the same Jan 1 to latest-date window across years.",
                "valid_values": ["year"],
            },
        )
    crashes = filtered(f)
    notes = []
    if same_period and store.latest_date:
        crashes = metrics.same_period_only(crashes, store.latest_date)
        notes.append(f"Each year counts Jan 1 to {store.latest_date:%b %d} only.")
    elif by in ("year", "month"):
        notes.append(
            f"Data ends {store.latest_date:%Y-%m-%d}, so the last period is incomplete (complete=false). "
            "Recent crashes can also still be arriving from local agencies."
        )
    rows = metrics.trend(crashes, by=by, latest_date=store.latest_date)
    if same_period:
        for row in rows:
            row.pop("complete", None)
    return envelope(f, crashes, rows, by=by, same_period=same_period, notes=notes)


CRASH_FIELDS = (
    "crash_id", "date", "hour", "county", "severity", "road", "road_class", "speed_limit",
    "factors", "lat", "lon", "deaths", "serious_injuries", "pedestrian", "cyclist", "motorcyclist",
)


@app.get("/api/crashes", response_model=schemas.CrashesResponse, response_model_exclude_unset=True, tags=["records"], responses=ERROR_RESPONSES, summary="Individual crashes (paginated, newest first)")
def list_crashes(
    f: Filters = Depends(crash_filters),
    has_location: bool | None = Query(
        None,
        description="true = only crashes with usable coordinates (for maps), false = only crashes without. Leave out for all.",
    ),
    limit: int = Query(100, ge=1, le=5000, description="Crashes per page (1 to 5000)."),
    offset: int = Query(0, ge=0, description="How many crashes to skip. Use next_offset from the previous page."),
):
    # Only crash-level fields go out. The source has person-level details
    # (age, gender, ethnicity, driver ZIP) that have no business on a public API.
    rows = filtered(f)
    if has_location is not None:
        rows = [c for c in rows if (c["lat"] is not None) == has_location]
    rows = sorted(rows, key=lambda c: (c["date"], c["crash_id"]), reverse=True)
    page = [{k: c[k] for k in CRASH_FIELDS} for c in rows[offset : offset + limit]]
    next_offset = offset + limit if offset + limit < len(rows) else None
    return envelope(
        f,
        rows,
        page,
        has_location=has_location,
        pagination={"limit": limit, "offset": offset, "returned": len(page), "next_offset": next_offset},
    )


# Lambda entry point. lifespan="off" because there's nothing to start up:
# the data loads lazily on the first request and stays cached in the container.
handler = Mangum(app, lifespan="off")
