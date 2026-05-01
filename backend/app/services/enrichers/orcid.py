"""ORCID public API — disambiguate authors by name + affiliation.

Per the ORCID search tutorial:
https://info.orcid.org/documentation/api-tutorials/api-tutorial-searching-the-orcid-registry/

Endpoint: https://pub.orcid.org/v3.0/search/?q=<solr-query>

The public API does NOT require authentication for the search endpoint, but
ORCID's `affiliation-org-name` field is essentially exact-match on the
institution token. Long PDF-extracted strings like
"Dr. Reddy ' s Institute of Life Sciences, University of Hyderabad,
 Hyderabad 500046, India" return zero results because the tokenizer can't
match them against the registered employment record's clean form.

This client normalises the affiliation, splits it into candidate institution
tokens, and builds a Solr query with OR'd alternatives. If that still yields
nothing, it falls back to a name-only search (which the cross-source
verifier can then disambiguate using paper topic context)."""

from __future__ import annotations

import re
from typing import Any

from ._base import HttpEnricher


# Country names + ISO-2 codes we can strip off the tail of an affiliation
_COUNTRY_TAIL = re.compile(
    r"(?i),?\s*(india|usa|u\.s\.a\.|united states( of america)?|uk|united kingdom|"
    r"china|japan|germany|france|italy|spain|canada|australia|brazil|mexico|"
    r"singapore|switzerland|netherlands|sweden|norway|denmark|finland|poland|"
    r"russia|south korea|korea|argentina|chile|colombia|israel|turkey|"
    r"south africa|new zealand|ireland|austria|belgium|portugal|greece|"
    r"hungary|czech republic|romania|ukraine)\s*$"
)

# Postal codes — Indian PIN, US ZIP, UK postcode, etc.
_POSTAL_TAIL = re.compile(
    r",?\s*(?:\b\d{4,6}\b|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b|\b\d{5}-\d{4}\b)\s*$"
)

# PDF artifacts: stray apostrophe-with-space ("Reddy ' s" → "Reddy's"),
# soft-hyphen / non-breaking-space, runs of whitespace
_ARTIFACT_APOSTROPHE = re.compile(r"\s+'\s*s\b")
_WHITESPACE = re.compile(r"\s+")

_GENERIC_TOKENS = {
    "india", "usa", "uk", "china", "japan", "and", "the",
    "department", "school", "institute", "laboratory", "lab", "center", "centre",
    "faculty", "division", "section", "unit", "group", "office", "campus",
}


def _clean_one(s: str) -> str:
    """Lightly fix common PDF-extraction artifacts."""
    s = s.replace("­", "").replace(" ", " ")
    s = _ARTIFACT_APOSTROPHE.sub("'s", s)   # "Reddy ' s" → "Reddy's"
    s = _WHITESPACE.sub(" ", s).strip()
    return s


def _affiliation_candidates(raw: str | None, max_alts: int = 4) -> list[str]:
    """Turn a noisy affiliation string into a small list of institution
    tokens that ORCID's `affiliation-org-name` field is likely to match.

    Strategy: drop country, drop postal code, split on commas, keep the
    pieces that look like institution names (longest first), drop generic
    department/school/lab tokens that aren't useful by themselves."""
    if not raw or not raw.strip():
        return []
    s = _clean_one(raw)
    # Strip country, then postal code (run twice in case of order)
    s = _COUNTRY_TAIL.sub("", s)
    s = _POSTAL_TAIL.sub("", s)
    s = _COUNTRY_TAIL.sub("", s)
    parts = [_clean_one(p) for p in s.split(",")]
    parts = [p for p in parts if p]

    # Drop pieces shorter than 8 chars OR that are purely a generic header
    def is_useful(p: str) -> bool:
        if len(p) < 4:
            return False
        low = p.lower()
        if low in _GENERIC_TOKENS:
            return False
        # "Department of X" → keep the X portion as an alt below; for now keep both
        return True

    parts = [p for p in parts if is_useful(p)]
    # Also include "X" extracted from "Department of X" / "School of X"
    extras = []
    for p in parts:
        m = re.match(r"(?i)^(?:department|school|faculty|division|institute|laboratory|lab|center|centre)\s+of\s+(.+)$", p)
        if m and len(m.group(1)) >= 4:
            extras.append(m.group(1).strip())
    candidates = parts + extras
    # De-duplicate, preserve insertion order, prefer the longer (more specific) tokens first
    seen = set()
    uniq: list[str] = []
    for c in sorted(candidates, key=lambda x: -len(x)):
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    return uniq[:max_alts]


def _solr_quote(s: str) -> str:
    """Escape backslashes and double-quotes for a Solr quoted phrase."""
    return s.replace("\\", "\\\\").replace('"', r"\"")


class ORCIDClient(HttpEnricher):
    base_url = "https://pub.orcid.org/v3.0"

    def _headers(self) -> dict[str, str]:
        h = super()._headers()
        h["Accept"] = "application/json"
        return h

    def _search_raw(self, q: str, rows: int) -> list[dict[str, Any]]:
        data = self._get("search", params={"q": q, "rows": rows})
        if not data:
            return []
        return [
            {"orcid": r.get("orcid-identifier", {}).get("path")}
            for r in data.get("result") or []
            if r.get("orcid-identifier")
        ]

    def search(self, given_name: str | None = None, family_name: str | None = None,
               affiliation: str | None = None, max_results: int = 5) -> list[dict[str, Any]]:
        """Solr search.

        Tries, in order, until one returns hits:
          1. cleaned name (period stripped, given→first word only) AND any
             affiliation alternative
          2. cleaned name only
          3. SWAPPED name (given ↔ family) only — common for Indian /
             Telugu / Tamil / Tibetan / Vietnamese / Korean profiles where
             the registry order doesn't match Western "given family" form

        Returns the hit list from the first variant that produces results."""
        if not (given_name or family_name):
            return []

        # Variants to try: (given, family)
        # Strip trailing period(s) from initials and reduce given to its
        # first whole word so that "Varma D." → "Varma" and "Tapan K." → "Tapan"
        def _strip_initials(g: str | None) -> str | None:
            if not g:
                return g
            head = g.split()[0].rstrip(".") if g.split() else None
            return head or None

        g_clean = _strip_initials(given_name)
        f_clean = (family_name or "").strip().rstrip(".") or None

        variants: list[tuple[str | None, str | None, str]] = []
        # Pass 1: cleaned-as-given vs cleaned-as-family
        variants.append((g_clean, f_clean, "name+affil"))
        variants.append((g_clean, f_clean, "name_only"))
        # Pass 2: swap given ↔ family (handles registries that store the
        # family/given order opposite to how it appears on the paper)
        if g_clean and f_clean and g_clean.lower() != f_clean.lower():
            variants.append((f_clean, g_clean, "name_only_swapped"))

        affil_alts = _affiliation_candidates(affiliation)

        for g, f, mode in variants:
            clauses: list[str] = []
            if g:
                clauses.append(f'given-names:"{_solr_quote(g)}"')
            if f:
                clauses.append(f'family-name:"{_solr_quote(f)}"')
            if not clauses:
                continue
            name_part = " AND ".join(clauses)

            if mode == "name+affil" and affil_alts:
                ors = " OR ".join(
                    f'affiliation-org-name:"{_solr_quote(a)}"' for a in affil_alts
                )
                hits = self._search_raw(f"{name_part} AND ({ors})", max_results)
                if hits:
                    return hits
            elif mode in ("name_only", "name_only_swapped"):
                hits = self._search_raw(name_part, max_results)
                if hits:
                    return hits

        return []

    def record(self, orcid: str) -> dict | None:
        return self._get(f"{orcid}/record")
