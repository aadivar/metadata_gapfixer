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

    def work_concepts(self, doi: str | None = None, openalex_id: str | None = None,
                      top_n: int = 8) -> list[dict[str, Any]]:
        """Return the paper's top OpenAlex concepts: [{display_name, score, level}, ...].

        Free, cached. Use either a DOI or an OpenAlex work ID."""
        if doi:
            payload = self._get(f"works/doi:{doi}")
        elif openalex_id:
            wid = openalex_id.replace("https://openalex.org/", "")
            payload = self._get(f"works/{wid}")
        else:
            return []
        return _compact_concepts((payload or {}).get("concepts"), top_n)

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
             "doi": f.get("ids", {}).get("doi"),
             "ror": f.get("ids", {}).get("ror"),
             "country": f.get("country_code")}
            for f in (data or {}).get("results", [])
        ]


def _compact_concepts(concepts: list[dict] | None, top_n: int) -> list[dict]:
    if not concepts:
        return []
    out = []
    for c in concepts[:top_n]:
        out.append({
            "display_name": c.get("display_name"),
            "score": round(float(c.get("score") or 0.0), 3),
            "level": c.get("level"),
        })
    return out


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
        "concepts": _compact_concepts(w.get("concepts"), 8),
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
        "top_concepts": _compact_concepts(a.get("x_concepts"), 5),
    }
