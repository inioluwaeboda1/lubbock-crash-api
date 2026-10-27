"""
Loading and filtering the cleaned crash file.

The whole dataset is ~24k crashes and about 1 MB gzipped, so it's loaded into
memory once per Lambda container and filtered with plain Python. At this size
that's faster than any database round trip and costs nothing to run.
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

DEFAULT_DATA_PATH = Path(__file__).parent / "data" / "crashes.json.gz"

SEVERITY_CODES = ("K", "A", "B", "C")


@dataclass
class CrashStore:
    crashes: list[dict]
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> "CrashStore":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            payload = json.load(f)
        return cls(crashes=payload["crashes"], meta=payload.get("meta", {}))

    # -- things the API needs to describe the data ---------------------------

    @property
    def latest_date(self) -> date | None:
        if not self.crashes:
            return None
        return date.fromisoformat(max(c["date"] for c in self.crashes))

    @property
    def counties(self) -> list[str]:
        return sorted({c["county"] for c in self.crashes if c["county"]})

    @property
    def years(self) -> list[int]:
        return sorted({c["year"] for c in self.crashes})

    @property
    def road_classes(self) -> list[str]:
        return sorted({c["road_class"] for c in self.crashes if c["road_class"]})

    # -- filtering ------------------------------------------------------------

    def filter(
        self,
        counties: list[str] | None = None,
        severities: list[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        road_classes: list[str] | None = None,
    ) -> list[dict]:
        """Return crashes matching every filter that was given.

        Text filters are case-insensitive so ?county=lubbock and
        ?county=LUBBOCK both work. Validation (unknown county names etc.)
        happens in the API layer so it can return a helpful error.
        """
        county_set = {c.lower() for c in counties} if counties else None
        severity_set = set(severities) if severities else None
        road_set = {r.lower() for r in road_classes} if road_classes else None

        result = []
        for crash in self.crashes:
            if county_set and (crash["county"] or "").lower() not in county_set:
                continue
            if severity_set and crash["severity"] not in severity_set:
                continue
            if year_from and crash["year"] < year_from:
                continue
            if year_to and crash["year"] > year_to:
                continue
            if road_set and (crash["road_class"] or "").lower() not in road_set:
                continue
            result.append(crash)
        return result


@lru_cache(maxsize=1)
def get_store() -> CrashStore:
    """Load the data once and reuse it for every request in this container."""
    path = os.environ.get("CRASH_DATA_PATH", DEFAULT_DATA_PATH)
    return CrashStore.from_file(path)
