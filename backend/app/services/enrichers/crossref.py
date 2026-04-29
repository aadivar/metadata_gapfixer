"""Crossref REST — DOI lookup, journal lookup, reference matching."""
from __future__ import annotations
from typing import Any
from ._base import HttpEnricher


class CrossrefClient(HttpEnricher):
    base_url = "https://api.crossref.org"

    def by_doi(self, doi: str) -> dict | None:
        data = self._get(f"works/{doi}")
        return (data or {}).get("message")

    def search_work(self, query: str, author: str | None = None,
                    container_title: str | None = None, max_results: int = 5) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"rows": max_results, "query.bibliographic": query}
        if author:
            params["query.author"] = author
        if container_title:
            params["query.container-title"] = container_title
        data = self._get("works", params=params)
        items = ((data or {}).get("message") or {}).get("items") or []
        return [_compact(w) for w in items]

    def search_journal(self, title: str | None = None, issn: str | None = None,
                       max_results: int = 5) -> list[dict[str, Any]]:
        if issn:
            data = self._get(f"journals/{issn}")
            msg = (data or {}).get("message")
            return [_compact_journal(msg)] if msg else []
        if not title:
            return []
        data = self._get("journals", params={"query": title, "rows": max_results})
        items = ((data or {}).get("message") or {}).get("items") or []
        return [_compact_journal(j) for j in items]


def _compact(w: dict) -> dict:
    return {
        "doi": w.get("DOI"),
        "title": (w.get("title") or [None])[0],
        "container_title": (w.get("container-title") or [None])[0],
        "type": w.get("type"),
        "issued": ((w.get("issued") or {}).get("date-parts") or [[None]])[0],
        "volume": w.get("volume"),
        "issue": w.get("issue"),
        "page": w.get("page"),
        "publisher": w.get("publisher"),
        "authors": [
            {"family": a.get("family"), "given": a.get("given"), "orcid": a.get("ORCID")}
            for a in (w.get("author") or [])
        ],
        "score": w.get("score"),
    }


def _compact_journal(j: dict | None) -> dict:
    if not j:
        return {}
    return {
        "title": j.get("title"),
        "publisher": j.get("publisher"),
        "issn": j.get("ISSN"),
        "issn_type": j.get("issn-type"),
    }
