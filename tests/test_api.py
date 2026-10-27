"""API tests, run against the real processed dataset bundled with the app."""

import json

import pytest
from fastapi.testclient import TestClient

from crash_api.main import app, handler

client = TestClient(app)

ENDPOINTS = ["/api/summary", "/api/severity", "/api/factors", "/api/trends", "/api/crashes"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_endpoint_uses_the_same_envelope(path):
    body = client.get(path).json()
    assert set(body) >= {"filters", "crash_count", "data"}
    assert set(body["filters"]) == {"county", "severity", "year_from", "year_to", "road_class"}


@pytest.mark.parametrize("path", ENDPOINTS)
def test_filters_narrow_the_results(path):
    everything = client.get(path).json()["crash_count"]
    narrowed = client.get(path, params={"county": "Hale", "severity": "KA", "year_from": 2024}).json()
    assert 0 < narrowed["crash_count"] < everything


def test_filter_values_are_case_insensitive_and_accept_lists():
    a = client.get("/api/severity", params={"county": "lubbock,HALE"}).json()
    b = client.get("/api/severity", params=[("county", "Lubbock"), ("county", "Hale")]).json()
    assert a["crash_count"] == b["crash_count"]
    assert a["filters"]["county"] == ["Lubbock", "Hale"]


def test_severity_counts_add_up():
    body = client.get("/api/severity").json()
    assert sum(r["crashes"] for r in body["data"]) == body["crash_count"]


def test_unknown_county_gives_a_helpful_400():
    r = client.get("/api/severity", params={"county": "Narnia"})
    assert r.status_code == 400
    assert "Lubbock" in r.json()["detail"]["valid_values"]


@pytest.mark.parametrize("bad", ["X", "KZ", ","])
def test_bad_severity_is_rejected(bad):
    assert client.get("/api/severity", params={"severity": bad}).status_code == 400


def test_reversed_year_range_is_rejected():
    assert client.get("/api/severity", params={"year_from": 2025, "year_to": 2021}).status_code == 400


def test_bad_trend_axis_is_rejected():
    assert client.get("/api/trends", params={"by": "fortnight"}).status_code == 422


def test_same_period_trend_is_marked_and_smaller():
    full = client.get("/api/trends").json()
    same = client.get("/api/trends", params={"same_period": True}).json()
    assert same["same_period"] is True
    assert same["crash_count"] < full["crash_count"]
    assert full["data"][-1]["complete"] is False   # the current year is partial


def test_crash_list_pagination():
    first = client.get("/api/crashes", params={"limit": 10}).json()
    second = client.get("/api/crashes", params={"limit": 10, "offset": 10}).json()
    assert len(first["data"]) == 10
    assert first["pagination"]["next_offset"] == 10
    assert {c["crash_id"] for c in first["data"]}.isdisjoint(c["crash_id"] for c in second["data"])
    assert first["data"][0]["date"] >= first["data"][-1]["date"]   # newest first


def test_crash_list_has_no_personal_fields():
    crash = client.get("/api/crashes", params={"limit": 1}).json()["data"][0]
    for field in ("age", "gender", "ethnicity", "zip", "driver_zip"):
        assert not any(field in key for key in crash)


def test_has_location_filter():
    body = client.get("/api/crashes", params={"has_location": True, "limit": 5000}).json()
    assert body["has_location"] is True
    assert all(c["lat"] is not None and c["lon"] is not None for c in body["data"])


def test_has_location_false_returns_only_unmapped_crashes():
    body = client.get("/api/crashes", params={"has_location": False, "limit": 5000}).json()
    assert body["has_location"] is False
    assert body["crash_count"] == 849
    assert all(c["lat"] is None for c in body["data"])


def test_has_location_left_out_returns_everything():
    body = client.get("/api/crashes", params={"limit": 1}).json()
    assert body["has_location"] is None
    assert body["crash_count"] == 24_261


def test_meta_lists_valid_filter_values():
    body = client.get("/api/meta").json()
    assert body["valid_filters"]["county"][0]["name"] == "Lubbock"   # sorted by crash count
    assert [s["code"] for s in body["valid_filters"]["severity"]] == ["K", "A", "B", "C"]


def test_dashboard_and_docs_are_served():
    assert "Lubbock District Injury Crashes" in client.get("/").text
    assert client.get("/docs").status_code == 200


def test_lambda_handler_with_a_function_url_event():
    # Shape of the event a Lambda function URL sends (payload format 2.0).
    event = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": "/api/severity",
        "rawQueryString": "county=Lubbock&severity=K",
        "headers": {"host": "example.lambda-url.us-east-1.on.aws", "accept": "application/json"},
        "queryStringParameters": {"county": "Lubbock", "severity": "K"},
        "requestContext": {
            "accountId": "anonymous",
            "domainName": "example.lambda-url.us-east-1.on.aws",
            "http": {"method": "GET", "path": "/api/severity", "protocol": "HTTP/1.1",
                     "sourceIp": "127.0.0.1", "userAgent": "pytest"},
            "requestId": "test", "routeKey": "$default", "stage": "$default",
            "time": "24/Oct/2026:12:00:00 +0000", "timeEpoch": 0,
        },
        "isBase64Encoded": False,
    }
    response = handler(event, None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["filters"]["severity"] == ["K"]
    assert body["data"][0]["crashes"] == body["crash_count"]


def test_unknown_query_parameter_is_rejected():
    # A typo shouldn't silently return district-wide numbers.
    r = client.get("/api/severity", params={"countys": "Hale"})
    assert r.status_code == 400
    assert "county" in r.json()["detail"]["valid_values"]


def test_dashboard_ignores_extra_query_parameters():
    # Only /api is strict. Links with tracking parameters should still open the page.
    assert client.get("/", params={"utm_source": "email"}).status_code == 200


@pytest.mark.parametrize("params", [{"year_from": 2030}, {"year_to": 2019}])
def test_year_range_with_no_data_is_rejected(params):
    r = client.get("/api/severity", params=params)
    assert r.status_code == 400
    assert r.json()["detail"]["valid_values"] == list(range(2020, 2027))


def test_year_range_that_partly_overlaps_is_fine():
    body = client.get("/api/severity", params={"year_from": 2019}).json()
    assert body["crash_count"] == 24_261


@pytest.mark.parametrize("axis", ["month", "hour", "weekday"])
def test_same_period_only_works_by_year(axis):
    assert client.get("/api/trends", params={"by": axis, "same_period": True}).status_code == 400


def test_validation_errors_have_the_same_shape_as_400s():
    detail = client.get("/api/crashes", params={"limit": 0}).json()["detail"]
    assert "message" in detail and "limit" in detail["message"]
    assert detail["errors"][0]["parameter"] == "limit"


def test_openapi_documents_error_responses():
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/api/severity"]["get"]["responses"]
    assert {"200", "400", "422"} <= set(responses)


@pytest.mark.parametrize(
    "path, params",
    [
        ("/api/severity", {"year_from": 2025, "year_to": 2021}),
        ("/api/trends", {"by": "month", "same_period": True}),
        ("/api/severity", {"county": "Narnia"}),
        ("/api/severity", {"bogus": 1}),
    ],
)
def test_every_400_says_what_would_be_valid(path, params):
    detail = client.get(path, params=params).json()["detail"]
    assert detail["message"] and "valid_values" in detail


def test_meta_explains_it_takes_no_parameters():
    detail = client.get("/api/meta", params={"bogus": 1}).json()["detail"]
    assert "doesn't take any query parameters" in detail["message"]


def test_openapi_describes_success_responses():
    spec = client.get("/openapi.json").json()
    for path in ["/api/meta", "/api/summary", "/api/severity", "/api/factors", "/api/trends", "/api/crashes"]:
        schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema.get("$ref", "").startswith("#/components/schemas/")


def test_complete_flag_only_where_it_means_something():
    assert "complete" in client.get("/api/trends", params={"by": "year"}).json()["data"][0]
    assert "complete" not in client.get("/api/trends", params={"by": "hour"}).json()["data"][0]
    assert "complete" not in client.get("/api/trends", params={"same_period": True}).json()["data"][0]
