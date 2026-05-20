"""Score the metadata Crossref currently has deposited for a DOI, using the
exact same rubric as the PDF-derived score.

Rationale: the publisher's editorial value-add isn't measured by an absolute
score — it's the **delta** between (a) what Crossref currently shows for the
article and (b) what the PDF could deposit if its metadata were fully
extracted. Showing both side by side surfaces the gap-fixing opportunity
directly.

Implementation: we don't write a parallel scoring engine. Instead we map
Crossref's `message` shape into the same synthetic `(Factsheet, metadata)`
pair the rubric consumes, then call `scoring.score(...)` on it. Anything
the rubric checks against the PDF (boilerplate, factsheet regex hits, etc.)
gets a blank Factsheet — the deposited record has no PDF behind it. All
deposited content lives on the metadata side.

Mapping is best-effort and conservative: we only count a field as "present"
when Crossref's deposited message actually contains it. Crossref doesn't
deposit prose statements (CoI, data-availability) as structured fields, so
those score 0 on the deposited side even when the article PDF includes them
— which is exactly the kind of gap a publisher should see.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from .enrichers.crossref import CrossrefClient
from .factsheet import Boilerplate, Facts, Factsheet
from .scoring import DimensionScore, Scorecard, score

log = logging.getLogger("crossref_score")

_DOI_RX = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


@dataclass
class DepositedResult:
    """A successful deposited-record score, ready to attach to the main Scorecard."""
    doi: str
    fetched_at: str                       # ISO timestamp
    dimensions: list[DimensionScore]
    research_nexus_score: int
    mandatory_ready: bool
    mandatory_present: int
    mandatory_total: int
    raw_summary: dict[str, Any]           # small summary of what Crossref returned (for the GUI tooltip)
    deposited_meta: dict[str, Any]        # the full mapped metadata dict — used by the route handler
                                          # to attach per-field previews and to back the accept-deposited flow


def _clean_doi(s: str) -> Optional[str]:
    if not s:
        return None
    m = _DOI_RX.search(s.strip())
    return m.group(0).rstrip(".,;)]>") if m else None


def _format_pub_date(parts: list[int] | None) -> Optional[str]:
    """`[[2024, 8, 13]]` -> `"2024-08-13"` (or `"2024-08"` / `"2024"`)."""
    if not parts:
        return None
    y = parts[0] if len(parts) >= 1 else None
    m = parts[1] if len(parts) >= 2 else None
    d = parts[2] if len(parts) >= 3 else None
    if y is None:
        return None
    if d is not None:
        return f"{y:04d}-{m:02d}-{d:02d}"
    if m is not None:
        return f"{y:04d}-{m:02d}"
    return f"{y:04d}"


def _split_pages(page: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Crossref stores pages as a single string like `"123-130"`."""
    if not page:
        return (None, None)
    if "-" in page:
        first, last = page.split("-", 1)
        return (first.strip() or None, last.strip() or None)
    return (page.strip() or None, None)


def _is_oa_license_url(url: str | None) -> bool:
    return bool(url) and "creativecommons.org/licenses" in (url or "").lower()


def _map_authors(authors: list[dict]) -> list[dict]:
    """Crossref author shape:
        {
          "given": "Asha", "family": "Lakshmi",
          "ORCID": "http://orcid.org/0000-0001-...",
          "affiliation": [{"name": "...", "id": [{"id-type": "ROR", "id": "..."}]}]
        }
    """
    out: list[dict] = []
    for i, a in enumerate(authors or []):
        given = a.get("given")
        family = a.get("family")
        orcid_url = a.get("ORCID") or ""
        # Normalise ORCID to bare ####-####-####-### form
        orcid = None
        m = re.search(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid_url)
        if m:
            orcid = m.group(0)

        affs: list[str] = []
        ror_ids: list[Optional[str]] = []
        for af in (a.get("affiliation") or []):
            name = (af.get("name") or "").strip()
            if not name:
                continue
            affs.append(name)
            ror = None
            for ident in (af.get("id") or []):
                if (ident.get("id-type") or "").upper() == "ROR":
                    raw = ident.get("id") or ""
                    ror = raw if raw.startswith("http") else f"https://ror.org/{raw}"
                    break
            ror_ids.append(ror)
        is_corr = bool(a.get("sequence") == "first") and i == 0
        out.append({
            "given_name": given,
            "surname": family,
            "full_name": " ".join(x for x in [given, family] if x),
            "orcid": orcid,
            "is_corresponding": is_corr,
            "affiliations": affs,
            "ror_ids": ror_ids,
        })
    return out


def _map_funders(funders: list[dict]) -> list[dict]:
    """Crossref funder shape:
        {"name": "NIH", "DOI": "10.13039/100000002", "award": ["R01-..."]}
    """
    out: list[dict] = []
    for f in funders or []:
        out.append({
            "name": f.get("name"),
            "doi": f.get("DOI"),
            "award_numbers": list(f.get("award") or []),
        })
    return out


def _map_references(refs: list[dict]) -> list[dict]:
    """Crossref reference shape: {"DOI": "...", "article-title": "...", "year": "2023", "unstructured": "..."}"""
    out: list[dict] = []
    for r in refs or []:
        out.append({
            "raw": r.get("unstructured") or r.get("article-title") or r.get("key") or "",
            "doi": (r.get("DOI") or "").lower() or None,
            "title": r.get("article-title"),
            "year": int(r["year"]) if str(r.get("year") or "").isdigit() else None,
        })
    return out


def _map_message_to_metadata(message: dict) -> dict:
    """Translate a Crossref `works/{doi}` message into the metadata shape
    used by `scoring._present` so the deposited record can be scored against
    the same rubric."""
    if not message:
        return {}
    issns_typed = message.get("issn-type") or []
    issn_e = next((x.get("value") for x in issns_typed if x.get("type") == "electronic"), None)
    issn_p = next((x.get("value") for x in issns_typed if x.get("type") == "print"), None)
    if not issn_e and not issn_p and message.get("ISSN"):
        # Fallback: ISSN list with no type info — assume electronic
        issn_e = message["ISSN"][0]

    issued = ((message.get("issued") or {}).get("date-parts") or [[None]])[0]
    pub_date = _format_pub_date(issued)
    first_page, last_page = _split_pages(message.get("page"))

    license_url = None
    for lic in (message.get("license") or []):
        url = lic.get("URL")
        if url:
            license_url = url
            break

    abstract = message.get("abstract") or None

    # Preprint linkage — Crossref deposits this via `relation.has-preprint`
    # or the reverse `relation.is-preprint-of`, or via `update-to`.
    preprint_doi = None
    rel = message.get("relation") or {}
    for key in ("has-preprint", "is-version-of", "is-derived-from"):
        items = rel.get(key) or []
        if items and isinstance(items, list):
            preprint_doi = items[0].get("id") or items[0].get("DOI")
            if preprint_doi:
                break

    # update-policy is the Crossmark policy URL pointer
    crossmark_policy_url = message.get("update-policy") or None

    meta: dict[str, Any] = {
        "doi": _clean_doi(message.get("DOI") or ""),
        "title": (message.get("title") or [None])[0],
        "journal_title": (message.get("container-title") or [None])[0],
        "issn_electronic": issn_e,
        "issn_print": issn_p,
        "publication_date": pub_date,
        "volume": message.get("volume"),
        "issue": message.get("issue"),
        "first_page": first_page,
        "last_page": last_page,
        "abstract": abstract,
        "license_url": license_url,
        "is_open_access": _is_oa_license_url(license_url),
        "preprint_doi": preprint_doi,
        "crossmark_policy_url": crossmark_policy_url,
        # Crossref doesn't deposit these as structured fields, so they stay None
        "conflict_of_interest": None,
        "data_availability": None,
        "copyright_holder": (message.get("copyright-holder") or None),
        "plain_language_summary": None,
        "credit_contributions": [],
        "authors": _map_authors(message.get("author") or []),
        "funders": _map_funders(message.get("funder") or []),
        "references": _map_references(message.get("reference") or []),
        "provenance": {},
    }
    return meta


def _empty_factsheet() -> Factsheet:
    """A blank factsheet — the deposited record has no PDF behind it.
    The rubric's PDF-derived fallbacks (e.g. `fs.license_url`, boilerplate)
    will all be empty, so only the deposited side counts."""
    return Factsheet(
        facts=Facts(),
        authors=[],
        affiliations={},
        boilerplate=Boilerplate(),
        coverage={},
    )


def fetch_deposited_score(doi: str) -> Optional[DepositedResult]:
    """Look up the DOI in Crossref and return a deposited-side scorecard.
    Returns None when Crossref has no record for the DOI."""
    cleaned = _clean_doi(doi)
    if not cleaned:
        return None
    client = CrossrefClient()
    try:
        message = client.by_doi(cleaned)
    except Exception as exc:
        log.warning("crossref by_doi failed for %s: %s", cleaned, exc)
        return None
    if not message:
        return None

    deposited_meta = _map_message_to_metadata(message)
    sc: Scorecard = score(_empty_factsheet(), deposited_meta)

    # The mandatory bucket of our rubric is *stricter* than Crossref's actual
    # deposit minimum — it includes `copyright_holder` as a Mandatory field,
    # but Crossref does not require it to mint a DOI. So if Crossref returned
    # a record for this DOI, the article is by definition past the deposit
    # gate, regardless of which sub-fields our rubric flags as missing. Force
    # `mandatory_ready=True` on the deposited side so the GUI doesn't show
    # the article as "not depositable" when Crossref has clearly deposited it.
    # The per-field counts (mandatory_present/mandatory_total) stay as-is so
    # the publisher can still see which integrity-grade Mandatory fields are
    # absent from the deposit.
    mandatory_ready = True

    # Build a compact summary the GUI can show in a hover
    summary = {
        "authors": len(deposited_meta.get("authors") or []),
        "authors_with_orcid": sum(1 for a in deposited_meta["authors"] if a.get("orcid")),
        "funders": len(deposited_meta.get("funders") or []),
        "funders_with_doi": sum(1 for f in deposited_meta["funders"] if f.get("doi")),
        "references": len(deposited_meta.get("references") or []),
        "references_with_doi": sum(1 for r in deposited_meta["references"] if r.get("doi")),
        "license_url": deposited_meta.get("license_url"),
        "has_abstract": bool(deposited_meta.get("abstract")),
        "publisher": message.get("publisher"),
    }

    return DepositedResult(
        doi=cleaned,
        fetched_at=datetime.utcnow().isoformat(timespec="seconds"),
        dimensions=sc.dimensions,
        research_nexus_score=sc.research_nexus_score,
        mandatory_ready=mandatory_ready,
        mandatory_present=sc.mandatory_present,
        mandatory_total=sc.mandatory_total,
        raw_summary=summary,
        deposited_meta=deposited_meta,
    )
