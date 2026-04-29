"""ORCID public API — disambiguate authors by name + affiliation."""
from __future__ import annotations
from typing import Any
from ._base import HttpEnricher


class ORCIDClient(HttpEnricher):
    base_url = "https://pub.orcid.org/v3.0"

    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        h["Accept"] = "application/json"
        return h

    def search(self, given_name: str | None = None, family_name: str | None = None,
               affiliation: str | None = None, max_results: int = 5) -> list[dict[str, Any]]:
        clauses = []
        if given_name:
            clauses.append(f'given-names:"{given_name}"')
        if family_name:
            clauses.append(f'family-name:"{family_name}"')
        if affiliation:
            clauses.append(f'affiliation-org-name:"{affiliation}"')
        if not clauses:
            return []
        q = " AND ".join(clauses)
        data = self._get("search", params={"q": q, "rows": max_results})
        if not data:
            return []
        return [
            {"orcid": r.get("orcid-identifier", {}).get("path")}
            for r in data.get("result", []) or []
            if r.get("orcid-identifier")
        ]

    def record(self, orcid: str) -> dict | None:
        return self._get(f"{orcid}/record")
