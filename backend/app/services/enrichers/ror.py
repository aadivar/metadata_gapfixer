"""ROR (Research Organization Registry) — resolve affiliation strings to ROR IDs.

ROR's `affiliation=` parameter is a full-text matcher backed by Solr; it's
robust to short/clean institution names ("IIT Delhi" → exact hit) but is
easily confused by long PDF-extracted strings that prefix the institution
with a school / department name and suffix it with city, postal code, and
country (e.g. "Kusuma School of Biological Sciences, Indian Institute of
Technology, New Delhi 110016, India" returns *Indiana* Institute of
Technology as the top FUZZY match — wrong).

To get reliable hits we try a small set of candidate institution strings
derived from the raw affiliation, collect the hits across all of them, and
return the highest-scoring de-duplicated set."""

from __future__ import annotations

import re
from typing import Any

from ._base import HttpEnricher


# Reuse the same artifact / postal / country regexes as ORCID
from .orcid import (
    _COUNTRY_TAIL, _POSTAL_TAIL, _clean_one,
)

# Institutions whose ROR-canonical form is "<Stem> <City>" but which papers
# typically write as "<Stem>, <City>". E.g. "Indian Institute of Technology,
# New Delhi" but ROR has it as "Indian Institute of Technology Delhi".
_COMMA_DELETE_STEMS = re.compile(
    r"(?i)\b("
    r"Indian Institute of Technology|"
    r"Indian Institute of Science|"
    r"National Institute of Technology|"
    r"All India Institute of Medical Sciences|"
    r"Indian Statistical Institute|"
    r"University of California|"
    r"University of Texas|"
    r"University of Michigan"
    r")\b\s*,\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,2})"
)

# What a piece looks like if it's an institution name (not a department / city)
_INSTITUTION_HINT = re.compile(
    r"(?i)\b(univers|institute|college|school|hospital|laborator|academy|"
    r"academ[ie]|polytechnic|cent(re|er)|consortium|foundation|society)\b"
)

# City-only / postal-only fragments that are never institution names by themselves
_CITY_ONLY = re.compile(r"^[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?$")


def _affiliation_candidates(raw: str | None, max_alts: int = 5) -> list[str]:
    """Build a small ranked list of query strings to try against ROR.

    Order matters: more specific / canonical first, so we can short-circuit
    on the first EXACT match."""
    if not raw or not raw.strip():
        return []
    cleaned = _clean_one(raw)

    out: list[str] = []

    # 1. Comma-delete canonical-stem patterns ("IIT, Delhi" → "IIT Delhi").
    #    These are the highest-precision rewrites — try them first. Also
    #    emit a variant with directional / cardinal city qualifiers stripped
    #    ("New Delhi" → "Delhi") since some ROR canonical names drop them.
    _CITY_QUALIFIER = re.compile(r"^(?:New|Old|Greater|East|West|North|South|Saint|St\.?|São|Port)\s+", re.IGNORECASE)
    for m in _COMMA_DELETE_STEMS.finditer(cleaned):
        stem = m.group(1).strip()
        city = m.group(2).strip()
        joined = f"{stem} {city}"
        if joined not in out:
            out.append(joined)
        # Also try the variant without the leading qualifier
        bare_city = _CITY_QUALIFIER.sub("", city).strip()
        if bare_city and bare_city != city:
            alt = f"{stem} {bare_city}"
            if alt not in out:
                out.append(alt)

    # 2. Strip country / postal tail
    stripped = _COUNTRY_TAIL.sub("", cleaned)
    stripped = _POSTAL_TAIL.sub("", stripped)
    stripped = _COUNTRY_TAIL.sub("", stripped).strip(" ,")

    # 3. Each comma-separated piece that looks like an institution name
    parts = [_clean_one(p) for p in stripped.split(",")]
    for p in parts:
        if not p or len(p) < 4:
            continue
        if _CITY_ONLY.match(p) and not _INSTITUTION_HINT.search(p):
            continue   # bare city — skip
        if _INSTITUTION_HINT.search(p) and p not in out:
            out.append(p)

    # 4. The whole stripped string — fall back to ROR's own fuzzy logic
    if stripped and stripped not in out:
        out.append(stripped)

    # 5. Last resort: the raw form
    if cleaned not in out:
        out.append(cleaned)

    return out[:max_alts]


class RORClient(HttpEnricher):
    base_url = "https://api.ror.org/v2"

    def _search_raw(self, name: str, max_results: int) -> list[dict[str, Any]]:
        data = self._get("organizations", params={"affiliation": name})
        if not data:
            return []
        items = data.get("items") or []
        out: list[dict[str, Any]] = []
        for it in items[:max_results]:
            org = it.get("organization") or it
            names = org.get("names") if isinstance(org.get("names"), list) else None
            display = (names[0].get("value") if names else None) or org.get("name")
            country = (org.get("locations") or [{}])[0].get("geonames_details", {}).get("country_name")
            out.append({
                "ror_id": org.get("id"),
                "name": display,
                "country": country,
                "score": it.get("score"),
                "matching_type": it.get("matching_type"),
                "matched_query": name,
            })
        return out

    def search(self, name: str, max_results: int = 5) -> list[dict[str, Any]]:
        """Try a ranked list of candidate institution strings.

        Strategy: each candidate is searched in turn. The first candidate that
        yields a *clean winner* (EXACT 1.0, OR top score ≥ 0.95 with the
        runner-up at most score-0.10) wins and we return its hits as-is.

        If no candidate produces a clean winner, return the hit list from
        the candidate whose top score is highest — the autofix's needs_pick
        path will then surface those candidates to the editor verbatim."""
        candidates = _affiliation_candidates(name)
        if not candidates:
            return []

        best_attempt: list[dict[str, Any]] = []
        best_top_score: float = -1.0
        for q in candidates:
            hits = self._search_raw(q, max_results)
            if not hits:
                continue
            top = hits[0]
            top_score = float(top.get("score") or 0)
            next_score = float((hits[1].get("score") if len(hits) > 1 else 0) or 0)
            # Clean winner — short-circuit
            is_exact_one = (top.get("matching_type") == "EXACT" and top_score >= 0.99)
            is_clear_winner = top_score >= 0.95 and (top_score - next_score) >= 0.10
            if is_exact_one or is_clear_winner:
                return hits[:max_results]
            # Track the strongest fallback by top-score
            if top_score > best_top_score:
                best_top_score = top_score
                best_attempt = hits[:max_results]

        return best_attempt

    def by_id(self, ror_id: str) -> dict | None:
        rid = ror_id.replace("https://ror.org/", "")
        return self._get(f"organizations/{rid}")
