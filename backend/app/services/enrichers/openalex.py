"""OpenAlex — disambiguate authors, works, institutions, funders."""
from __future__ import annotations
from typing import Any
from ._base import HttpEnricher


class OpenAlexClient(HttpEnricher):
    base_url = "https://api.openalex.org"

    def search_work(self, title: str | None = None, doi: str | None = None,
                    author: str | None = None, max_results: int = 5) -> list[dict[str, Any]]:
        if doi:
            payload = self._get(f"works/doi:{doi}")
            return [_compact_work(payload)] if payload else []
        if not title:
            return []
        params = {"search": title, "per-page": max_results}
        if author:
            params["filter"] = f"author.display_name.search:{author}"
        data = self._get("works", params=params)
        return [_compact_work(w) for w in (data or {}).get("results", [])]

    def search_author(self, name: str, affiliation: str | None = None,
                      max_results: int = 5) -> list[dict[str, Any]]:
        params = {"search": name, "per-page": max_results}
        if affiliation:
            params["filter"] = f"last_known_institutions.display_name.search:{affiliation}"
        data = self._get("authors", params=params)
        return [_compact_author(a) for a in (data or {}).get("results", [])]

    def search_funder(self, name: str, max_results: int = 5) -> list[dict[str, Any]]:
        data = self._get("funders", params={"search": name, "per-page": max_results})
        return [
            {"openalex_id": f.get("id"), "name": f.get("display_name"),
             "doi": f.get("ids", {}).get("doi"), "country": f.get("country_code")}
            for f in (data or {}).get("results", [])
        ]


def _compact_work(w: dict | None) -> dict:
    if not w:
        return {}
    return {
        "openalex_id": w.get("id"),
        "doi": w.get("doi"),
        "title": w.get("title") or w.get("display_name"),
        "publication_year": w.get("publication_year"),
        "publication_date": w.get("publication_date"),
        "type": w.get("type"),
        "authors": [
            {"name": a.get("author", {}).get("display_name"),
             "orcid": a.get("author", {}).get("orcid"),
             "institutions": [i.get("display_name") for i in (a.get("institutions") or [])]}
            for a in (w.get("authorships") or [])
        ],
        "host_venue": (w.get("primary_location") or {}).get("source", {}).get("display_name"),
        "issn_l": (w.get("primary_location") or {}).get("source", {}).get("issn_l"),
    }


def _compact_author(a: dict) -> dict:
    inst = (a.get("last_known_institutions") or [{}])[0] if a.get("last_known_institutions") else {}
    return {
        "openalex_id": a.get("id"),
        "name": a.get("display_name"),
        "orcid": a.get("orcid"),
        "works_count": a.get("works_count"),
        "institution": inst.get("display_name"),
        "ror": inst.get("ror"),
    }
