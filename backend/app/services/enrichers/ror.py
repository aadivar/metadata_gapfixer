"""ROR (Research Organization Registry) — resolve affiliation strings to ROR IDs."""
from __future__ import annotations
from typing import Any
from ._base import HttpEnricher


class RORClient(HttpEnricher):
    base_url = "https://api.ror.org/v2"

    def search(self, name: str, max_results: int = 5) -> list[dict[str, Any]]:
        data = self._get("organizations", params={"affiliation": name})
        if not data:
            return []
        items = data.get("items") or []
        out = []
        for it in items[:max_results]:
            org = it.get("organization") or it
            out.append({
                "ror_id": org.get("id"),
                "name": (org.get("names") or [{}])[0].get("value") if isinstance(org.get("names"), list) else org.get("name"),
                "country": (org.get("locations") or [{}])[0].get("geonames_details", {}).get("country_name"),
                "score": it.get("score"),
                "matching_type": it.get("matching_type"),
            })
        return out

    def by_id(self, ror_id: str) -> dict | None:
        rid = ror_id.replace("https://ror.org/", "")
        return self._get(f"organizations/{rid}")
