"""Per-field deterministic auto-fixers.

Each fixer takes the current metadata + factsheet and returns a patched
metadata dict (and a short report describing what changed). No LLM calls —
only the enricher APIs and the factsheet. The agent/LLM is only invoked
for ambiguous disambiguation, not for these mechanical fixes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .enrichers import CrossrefClient, OpenAlexClient, ORCIDClient, RORClient
from .factsheet import Factsheet

log = logging.getLogger("autofix")


def _ensure_authors(meta: dict, fs: Factsheet) -> list[dict]:
    if meta.get("authors"):
        return meta["authors"]
    # Bootstrap authors from the factsheet
    out = []
    for a in fs.authors:
        affs = []
        for marker in a.markers:
            if marker in fs.affiliations:
                affs.append(fs.affiliations[marker])
        out.append({
            "given_name": a.given,
            "surname": a.surname,
            "full_name": a.name,
            "orcid": a.orcid,
            "is_corresponding": a.is_corresponding,
            "email": a.email,
            "affiliations": affs,
            "ror_ids": [],
        })
    meta["authors"] = out
    return out


def _ensure_funders(meta: dict, fs: Factsheet) -> list[dict]:
    if meta.get("funders"):
        return meta["funders"]
    funders: list[dict] = []
    # Build funder candidates from grant IDs (those carry a funder hint)
    by_funder: dict[str, list[str]] = {}
    for g in fs.facts.grant_ids:
        name = g.get("funder_hint")
        if not name:
            continue
        by_funder.setdefault(name, []).append(g["id"])
    for name, awards in by_funder.items():
        funders.append({"name": name, "doi": None, "award_numbers": awards})
    meta["funders"] = funders
    return funders


def _ensure_references(meta: dict, fs: Factsheet, docling_doc: dict) -> list[dict]:
    if meta.get("references"):
        return meta["references"]
    # Build from the references zone of the markdown
    md = docling_doc.get("markdown") or ""
    m = re.search(r"(?:^|\n)#{1,3}\s*(?:References|Bibliography|Works\s+cited)\s*\n(.+?)(?:\n#{1,3}\s|\Z)",
                  md, re.IGNORECASE | re.DOTALL)
    refs_text = m.group(1) if m else ""
    if not refs_text:
        return []
    # Split numbered or bulleted items
    items = re.split(r"\n\s*(?:\d{1,3}\.|\[\d{1,3}\]|\-)\s+", "\n" + refs_text.strip())
    items = [r.strip() for r in items if r.strip() and len(r.strip()) > 20]

    refs: list[dict] = []
    for raw in items:
        raw_one = re.sub(r"\s+", " ", raw)
        doi_m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", raw_one, re.IGNORECASE)
        year_m = re.search(r"\b(19|20)\d{2}\b", raw_one)
        refs.append({
            "raw": raw_one[:600],
            "doi": doi_m.group(0).rstrip(".,;)]>") if doi_m else None,
            "title": None,
            "year": int(year_m.group(0)) if year_m else None,
        })
    meta["references"] = refs
    return refs


# ============================================================================
# Public dispatcher
# ============================================================================

class AutofixReport(dict):
    pass


def run_autofix(action: str, metadata: dict, factsheet: Factsheet, docling_doc: dict) -> AutofixReport:
    """Apply a single named auto-fix and return what changed."""
    if action == "from_factsheet":
        return _autofix_from_factsheet(metadata, factsheet)
    if action == "from_docling_title":
        return _autofix_title(metadata, docling_doc, factsheet)
    if action == "from_docling_abstract":
        return _autofix_abstract(metadata, docling_doc)
    if action == "from_docling_refs":
        n = len(_ensure_references(metadata, factsheet, docling_doc))
        return AutofixReport({"action": action, "added_refs": n, "ok": True})
    if action == "from_license":
        return _autofix_oa(metadata, factsheet)
    if action == "resolve_orcids":
        return _autofix_orcids(metadata, factsheet)
    if action == "resolve_rors":
        return _autofix_rors(metadata, factsheet)
    if action == "resolve_references":
        return _autofix_references(metadata, factsheet, docling_doc)
    if action == "resolve_funders":
        return _autofix_funders(metadata, factsheet)
    if action == "crossref_by_doi":
        return _autofix_crossref_by_doi(metadata, factsheet)
    if action == "detect_preprint":
        return _autofix_preprint(metadata, factsheet)
    return AutofixReport({"action": action, "ok": False, "error": "unknown action"})


# ── factsheet-only fixes (free) ───────────────────────────────────────────

def _autofix_from_factsheet(meta: dict, fs: Factsheet) -> AutofixReport:
    changes: list[str] = []
    if not meta.get("doi") and fs.facts.doi:
        meta["doi"] = fs.facts.doi; changes.append("doi")
    if not meta.get("issn_print") and not meta.get("issn_electronic") and fs.facts.issns:
        meta["issn_electronic"] = fs.facts.issns[0]; changes.append("issn")
    if not meta.get("license_url") and fs.facts.license_url:
        meta["license_url"] = fs.facts.license_url; changes.append("license_url")
    if not meta.get("authors"):
        _ensure_authors(meta, fs); changes.append(f"authors ({len(meta['authors'])})")
    if not meta.get("funders"):
        _ensure_funders(meta, fs); changes.append(f"funders ({len(meta['funders'])})")
    if not meta.get("conflict_of_interest") and fs.boilerplate.conflict_of_interest:
        meta["conflict_of_interest"] = fs.boilerplate.conflict_of_interest; changes.append("conflict_of_interest")
    if not meta.get("data_availability") and fs.boilerplate.data_availability:
        meta["data_availability"] = fs.boilerplate.data_availability; changes.append("data_availability")
    return AutofixReport({"action": "from_factsheet", "ok": True, "changes": changes})


def _autofix_title(meta: dict, docling_doc: dict, fs: Factsheet) -> AutofixReport:
    if meta.get("title"):
        return AutofixReport({"action": "from_docling_title", "ok": True, "changes": []})
    doc = docling_doc.get("doc") or {}
    for t in (doc.get("texts") or []):
        if t.get("label") == "title" and t.get("text"):
            meta["title"] = t["text"].strip()
            return AutofixReport({"action": "from_docling_title", "ok": True, "changes": ["title"]})
    # Fallback: PDF metadata
    title = (fs.facts.pdf_xmp or {}).get("title")
    if title:
        meta["title"] = title.strip()
        return AutofixReport({"action": "from_docling_title", "ok": True, "changes": ["title (from PDF metadata)"]})
    return AutofixReport({"action": "from_docling_title", "ok": False, "error": "no title element"})


def _autofix_abstract(meta: dict, docling_doc: dict) -> AutofixReport:
    if meta.get("abstract"):
        return AutofixReport({"action": "from_docling_abstract", "ok": True, "changes": []})
    md = docling_doc.get("markdown") or ""
    m = re.search(r"(?:^|\n)#{1,3}\s*Abstract\s*\n(.+?)(?:\n#{1,3}\s|\n\n[A-Z][a-z]+\s+[A-Z][a-z]+|\Z)",
                  md, re.IGNORECASE | re.DOTALL)
    if m:
        meta["abstract"] = re.sub(r"\s+", " ", m.group(1)).strip()[:3000]
        return AutofixReport({"action": "from_docling_abstract", "ok": True, "changes": ["abstract"]})
    return AutofixReport({"action": "from_docling_abstract", "ok": False, "error": "abstract section not found"})


def _autofix_oa(meta: dict, fs: Factsheet) -> AutofixReport:
    if not (fs.facts.is_open_access_license or meta.get("license_url")):
        return AutofixReport({"action": "from_license", "ok": False, "error": "no license info"})
    meta["is_open_access"] = True
    return AutofixReport({"action": "from_license", "ok": True, "changes": ["is_open_access=True"]})


# ── enricher-API fixes (free APIs) ────────────────────────────────────────

def _autofix_orcids(meta: dict, fs: Factsheet) -> AutofixReport:
    authors = _ensure_authors(meta, fs)
    orcid = ORCIDClient()
    resolved = 0
    for a in authors:
        if a.get("orcid"):
            continue
        family = a.get("surname")
        given = a.get("given_name")
        affil = (a.get("affiliations") or [None])[0]
        if not family:
            continue
        candidates = orcid.search(given_name=given, family_name=family, affiliation=affil)
        if len(candidates) == 1:
            a["orcid"] = candidates[0]["orcid"]
            resolved += 1
    return AutofixReport({"action": "resolve_orcids", "ok": True, "resolved": resolved, "out_of": len(authors)})


def _autofix_rors(meta: dict, fs: Factsheet) -> AutofixReport:
    authors = _ensure_authors(meta, fs)
    ror = RORClient()
    aff_to_ror: dict[str, str] = {}
    for a in authors:
        rors_for_author: list[str | None] = []
        for affil in (a.get("affiliations") or []):
            if affil in aff_to_ror:
                rors_for_author.append(aff_to_ror[affil])
                continue
            cands = ror.search(affil)
            top = cands[0] if cands else None
            if top and (top.get("score") or 0) > 0.7:
                aff_to_ror[affil] = top["ror_id"]
                rors_for_author.append(top["ror_id"])
            else:
                rors_for_author.append(None)
        a["ror_ids"] = rors_for_author
    resolved = sum(1 for v in aff_to_ror.values() if v)
    return AutofixReport({"action": "resolve_rors", "ok": True, "resolved": resolved})


def _autofix_funders(meta: dict, fs: Factsheet) -> AutofixReport:
    funders = _ensure_funders(meta, fs)
    oa = OpenAlexClient()
    resolved = 0
    for fu in funders:
        if fu.get("doi") or not fu.get("name"):
            continue
        cands = oa.search_funder(fu["name"])
        if cands and cands[0].get("doi"):
            fu["doi"] = cands[0]["doi"]
            resolved += 1
    return AutofixReport({"action": "resolve_funders", "ok": True, "resolved": resolved, "out_of": len(funders)})


def _autofix_references(meta: dict, fs: Factsheet, docling_doc: dict) -> AutofixReport:
    refs = _ensure_references(meta, fs, docling_doc)
    cr = CrossrefClient()
    resolved = 0
    for r in refs[:100]:  # cap
        if r.get("doi"):
            continue
        # Try Crossref bibliographic search using the raw citation text
        cands = cr.search_work(query=r["raw"][:400])
        top = cands[0] if cands else None
        if top and (top.get("score") or 0) > 30:
            r["doi"] = top.get("doi")
            r["title"] = top.get("title")
            if not r.get("year") and top.get("issued"):
                r["year"] = (top["issued"] or [None])[0]
            resolved += 1
    return AutofixReport({"action": "resolve_references", "ok": True, "resolved": resolved, "out_of": len(refs)})


def _autofix_crossref_by_doi(meta: dict, fs: Factsheet) -> AutofixReport:
    doi = meta.get("doi") or fs.facts.doi
    if not doi:
        return AutofixReport({"action": "crossref_by_doi", "ok": False, "error": "no DOI to look up"})
    cr = CrossrefClient()
    rec = cr.by_doi(doi)
    if not rec:
        return AutofixReport({"action": "crossref_by_doi", "ok": False, "error": "DOI not found"})
    changes = []
    if not meta.get("journal_title"):
        ct = (rec.get("container-title") or [None])[0]
        if ct:
            meta["journal_title"] = ct; changes.append("journal_title")
    if not meta.get("issn_print") or not meta.get("issn_electronic"):
        for issn in (rec.get("ISSN") or []):
            if not meta.get("issn_electronic"):
                meta["issn_electronic"] = issn
            else:
                meta["issn_print"] = issn
        changes.append("issn")
    if not meta.get("publication_date"):
        issued = ((rec.get("issued") or {}).get("date-parts") or [[None]])[0]
        if issued and issued[0]:
            meta["publication_date"] = "-".join(f"{x:02d}" if isinstance(x, int) else str(x) for x in issued)
            changes.append("publication_date")
    if not meta.get("volume") and rec.get("volume"):
        meta["volume"] = rec["volume"]; changes.append("volume")
    if not meta.get("issue") and rec.get("issue"):
        meta["issue"] = rec["issue"]; changes.append("issue")
    if not meta.get("first_page") and rec.get("page"):
        page = rec["page"]
        if "-" in page:
            meta["first_page"], meta["last_page"] = page.split("-", 1)
        else:
            meta["first_page"] = page
        changes.append("pages")
    if not meta.get("title") and (rec.get("title") or []):
        meta["title"] = rec["title"][0]; changes.append("title")
    return AutofixReport({"action": "crossref_by_doi", "ok": True, "changes": changes})


def _autofix_preprint(meta: dict, fs: Factsheet) -> AutofixReport:
    pp = fs.facts.preprint_doi
    if not pp:
        return AutofixReport({"action": "detect_preprint", "ok": False, "error": "no preprint candidate detected"})
    meta["preprint_doi"] = pp
    return AutofixReport({"action": "detect_preprint", "ok": True, "changes": [f"preprint_doi={pp}"]})
