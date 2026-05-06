"""Per-field deterministic auto-fixers.

Cost policy: auto-fix NEVER calls the LLM. The LLM disambiguation layer is
opt-in per field (or per batch) via the explicit `/disambiguate` endpoint,
so every paid call is the publisher's deliberate choice.

Decision rules per enricher field during auto-fix:
  1. Enricher returns 1 candidate → take it. Provenance source = "<api>_api".
  2. Enricher returns N>1 candidates → leave empty, attach the candidate
     list to provenance with source = "needs_pick" so the GUI can render
     either a manual-pick list (free) or an "Adjudicate with AI" button
     (one paid call when the publisher clicks).
  3. Enricher returns 0 candidates → empty, source = "no_candidates".

The disambiguate_field() function below is what `/disambiguate` calls — it
runs ONE LLM pick for ONE field, updates provenance, and writes the chosen
value into the metadata. Publishers can also call apply_pick() for a
manual choice with zero cost.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .enrichers import CrossrefClient, OpenAlexClient, ORCIDClient, RORClient
from .factsheet import Factsheet
from .llm_router import Alternative, Disambiguation, LLMRouter, router_for_submission

log = logging.getLogger("autofix")


# ============================================================================
# Provenance helper
# ============================================================================

def _set_prov(meta: dict, path: str, *, source: str, confidence: float = 1.0,
              reasoning: str = "", alternatives: list | None = None) -> None:
    prov = meta.setdefault("provenance", {})
    entry: dict[str, Any] = {"source": source, "confidence": round(confidence, 3)}
    if reasoning:
        entry["reasoning"] = reasoning
    if alternatives:
        entry["alternatives"] = alternatives
    prov[path] = entry


def _disambiguation_alts(d: Disambiguation) -> list[dict]:
    return [a.model_dump(exclude_none=True) for a in (d.ranked_alternatives or [])]


def _candidates_to_provenance(candidates: list[dict], id_keys: list[str]) -> list[dict]:
    """Convert raw enricher candidates into a stable shape for the GUI to render
    a manual-pick list. Each entry: {id, label, score, raw}."""
    out: list[dict] = []
    for c in candidates[:10]:
        cid = next((c.get(k) for k in id_keys if c.get(k)), None)
        # Build a short human label
        label_parts: list[str] = []
        for k in ("name", "display_name", "title", "container_title", "preferred_name"):
            if c.get(k):
                label_parts.append(str(c[k]))
                break
        # ORCID-specific
        if "orcid" in c and not label_parts:
            label_parts.append(c.get("orcid"))
        out.append({
            "id": str(cid) if cid else None,
            "label": " · ".join(label_parts)[:160] if label_parts else None,
            "score": c.get("score"),
            "raw": c,
        })
    return out


CONFIDENCE_THRESHOLD = 0.8


# ============================================================================
# Bootstrap helpers (unchanged)
# ============================================================================

def _ensure_authors(meta: dict, fs: Factsheet) -> list[dict]:
    if meta.get("authors"):
        return meta["authors"]
    out = []
    for i, a in enumerate(fs.authors):
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
        if a.orcid:
            _set_prov(meta, f"authors[{i}].orcid",
                      source="factsheet", confidence=1.0,
                      reasoning="Extracted from PDF text by regex.")
    meta["authors"] = out
    return out


def _ensure_funders(meta: dict, fs: Factsheet) -> list[dict]:
    if meta.get("funders"):
        return meta["funders"]
    funders: list[dict] = []
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

    # Tier 1-3: layout-aware detection (Docling label → section walk → visual cluster)
    from .references_layout import detect_references
    layout = detect_references(docling_doc)
    refs_strings: list[str] = []
    detection_method = "none"
    detection_confidence = 0.0
    detection_notes = ""
    page_range_str = ""

    if layout.items:
        refs_strings = [item.text for item in layout.items if item.text]
        detection_method = f"layout_{layout.method}"
        detection_confidence = layout.confidence
        detection_notes = layout.notes
        if layout.page_start and layout.page_end:
            page_range_str = (f"pp.{layout.page_start}" if layout.page_start == layout.page_end
                              else f"pp.{layout.page_start}-{layout.page_end}")
    else:
        # Fallback: markdown regex split
        md = docling_doc.get("markdown") or ""
        m = re.search(r"(?:^|\n)#{1,3}\s*(?:References|Bibliography|Works\s+cited)\s*\n(.+?)(?:\n#{1,3}\s|\Z)",
                      md, re.IGNORECASE | re.DOTALL)
        refs_text = m.group(1) if m else ""
        if refs_text:
            items = re.split(r"\n\s*(?:\d{1,3}\.|\[\d{1,3}\]|\-)\s+", "\n" + refs_text.strip())
            refs_strings = [r.strip() for r in items if r.strip() and len(r.strip()) > 20]
            detection_method = "markdown_regex"
            detection_confidence = 0.6
            detection_notes = "Fell back to markdown regex split (layout detection found no candidates)."

    refs: list[dict] = []
    for raw in refs_strings:
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

    # Record where the section came from — feeds the editor-confirmation UI
    if refs:
        meta.setdefault("provenance", {})["references"] = {
            "source": detection_method,
            "confidence": detection_confidence,
            "reasoning": (detection_notes + (f" {page_range_str}" if page_range_str else "")).strip(),
            "page_start": layout.page_start,
            "page_end": layout.page_end,
            "item_count": len(refs),
        }
    return refs


# ============================================================================
# Public dispatcher
# ============================================================================

class AutofixReport(dict):
    pass


def run_autofix(action: str, metadata: dict, factsheet: Factsheet,
                docling_doc: dict, *, sub_id: int | None = None) -> AutofixReport:
    """Apply a single named auto-fix. Always FREE — never calls the LLM."""
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
        meta["doi"] = fs.facts.doi
        _set_prov(meta, "doi", source="factsheet", confidence=1.0,
                  reasoning="Extracted from PDF metadata or full-text regex.")
        changes.append("doi")
    if not meta.get("issn_print") and not meta.get("issn_electronic") and fs.facts.issns:
        meta["issn_electronic"] = fs.facts.issns[0]
        _set_prov(meta, "issn_electronic", source="factsheet", confidence=0.85,
                  reasoning="Detected ISSN-pattern in PDF; assignment to print/electronic is heuristic.")
        changes.append("issn")
    if not meta.get("license_url") and fs.facts.license_url:
        meta["license_url"] = fs.facts.license_url
        _set_prov(meta, "license_url", source="factsheet", confidence=1.0,
                  reasoning="Creative Commons URL detected in PDF text.")
        changes.append("license_url")
    if not meta.get("authors"):
        _ensure_authors(meta, fs)
        changes.append(f"authors ({len(meta['authors'])})")
    if not meta.get("funders"):
        _ensure_funders(meta, fs)
        changes.append(f"funders ({len(meta['funders'])})")
    if not meta.get("conflict_of_interest") and fs.boilerplate.conflict_of_interest:
        meta["conflict_of_interest"] = fs.boilerplate.conflict_of_interest
        _set_prov(meta, "conflict_of_interest", source="boilerplate", confidence=0.9,
                  reasoning="Anchor-phrase match on 'Conflict of interest' / 'Competing interests'.")
        changes.append("conflict_of_interest")
    if not meta.get("data_availability") and fs.boilerplate.data_availability:
        meta["data_availability"] = fs.boilerplate.data_availability
        _set_prov(meta, "data_availability", source="boilerplate", confidence=0.9,
                  reasoning="Anchor-phrase match on 'Data availability'.")
        changes.append("data_availability")
    return AutofixReport({"action": "from_factsheet", "ok": True, "changes": changes})


def _autofix_title(meta: dict, docling_doc: dict, fs: Factsheet) -> AutofixReport:
    if meta.get("title"):
        return AutofixReport({"action": "from_docling_title", "ok": True, "changes": []})
    doc = docling_doc.get("doc") or {}
    for t in (doc.get("texts") or []):
        if t.get("label") == "title" and t.get("text"):
            meta["title"] = t["text"].strip()
            _set_prov(meta, "title", source="docling", confidence=1.0,
                      reasoning="Element labeled 'title' by Docling layout analysis.")
            return AutofixReport({"action": "from_docling_title", "ok": True, "changes": ["title"]})
    title = (fs.facts.pdf_xmp or {}).get("title")
    if title:
        meta["title"] = title.strip()
        _set_prov(meta, "title", source="pdf_xmp", confidence=0.7,
                  reasoning="Title from PDF XMP metadata; this is sometimes a filename.")
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
        _set_prov(meta, "abstract", source="docling", confidence=0.95,
                  reasoning="Section under heading 'Abstract' extracted from Docling markdown.")
        return AutofixReport({"action": "from_docling_abstract", "ok": True, "changes": ["abstract"]})
    return AutofixReport({"action": "from_docling_abstract", "ok": False, "error": "abstract section not found"})


def _autofix_oa(meta: dict, fs: Factsheet) -> AutofixReport:
    if not (fs.facts.is_open_access_license or meta.get("license_url")):
        return AutofixReport({"action": "from_license", "ok": False, "error": "no license info"})
    meta["is_open_access"] = True
    _set_prov(meta, "is_open_access", source="derived", confidence=1.0,
              reasoning="Derived from Creative Commons license URL.")
    return AutofixReport({"action": "from_license", "ok": True, "changes": ["is_open_access=True"]})


# ── enricher-API fixes — FREE, never calls the LLM ───────────────────────
# When the enricher returns multiple candidates, we attach them to the
# provenance with source="needs_pick". The publisher then either picks
# manually (free) or explicitly opts into a paid LLM disambiguation via
# the /disambiguate endpoint.

def _autofix_orcids(meta: dict, fs: Factsheet) -> AutofixReport:
    """Look up ORCID for each author by (given, family, affiliation).

    Behaviour matches the user-stated rule "lookup-first, never synthesise":
      - Sole candidate returned by the affiliation-restricted query → accept
        (the affiliation filter is strict enough that a singleton is reliable).
      - Multiple candidates → flag as needs_pick; the editor can adjudicate
        manually or pay for verify_authors.
      - Zero candidates → drop silently (provenance source="no_candidates"),
        do NOT write a placeholder ID.
    """
    authors = _ensure_authors(meta, fs)
    orcid = ORCIDClient()
    resolved = needs_pick = no_candidates = 0

    for i, a in enumerate(authors):
        if a.get("orcid"):
            continue
        family = a.get("surname")
        given = a.get("given_name")
        affil = (a.get("affiliations") or [None])[0]
        if not family:
            continue
        candidates = orcid.search(given_name=given, family_name=family, affiliation=affil)
        path = f"authors[{i}].orcid"
        if not candidates:
            no_candidates += 1
            # Silent drop: keep a.orcid as None, mark provenance so the
            # editor can see the lookup ran but found nothing.
            _set_prov(meta, path, source="no_candidates", confidence=0.0,
                      reasoning=f"ORCID API returned no matches for given='{given}' family='{family}' aff='{affil}'.")
        elif len(candidates) == 1:
            a["orcid"] = candidates[0]["orcid"]
            _set_prov(meta, path, source="orcid_api", confidence=1.0,
                      reasoning="Sole candidate returned by ORCID search restricted by name + affiliation.")
            resolved += 1
        else:
            # Multiple candidates: do NOT pick one. Attach the list and
            # flag needs_pick — the editor (or paid verify_authors) decides.
            entry = meta.setdefault("provenance", {})
            entry[path] = {
                "source": "needs_pick",
                "confidence": 0.0,
                "reasoning": f"{len(candidates)} candidates returned by ORCID; pick one or use AI to adjudicate.",
                "candidates": _candidates_to_provenance(candidates, ["orcid"]),
                "query": {"given": given, "family": family, "affiliation": affil},
                "task": "orcid_pick", "source_api": "ORCID",
            }
            needs_pick += 1

    return AutofixReport({"action": "resolve_orcids", "ok": True,
                          "resolved": resolved, "needs_pick": needs_pick,
                          "no_candidates": no_candidates, "out_of": len(authors)})


def _autofix_rors(meta: dict, fs: Factsheet) -> AutofixReport:
    authors = _ensure_authors(meta, fs)
    ror = RORClient()
    aff_cache: dict[str, dict] = {}  # affil -> {ror_id, source, candidates?}
    resolved = needs_pick = 0

    for i, a in enumerate(authors):
        rors_for_author: list[str | None] = []
        for j, affil in enumerate(a.get("affiliations") or []):
            path = f"authors[{i}].ror_ids[{j}]"
            if affil in aff_cache:
                cached = aff_cache[affil]
                rors_for_author.append(cached.get("ror_id"))
                _set_prov(meta, path, source=cached["source"], confidence=cached.get("confidence", 1.0),
                          reasoning=cached["reasoning"])
                continue
            cands = ror.search(affil)
            if not cands:
                aff_cache[affil] = {"ror_id": None, "source": "no_candidates",
                                    "reasoning": f"ROR API returned no matches for '{affil[:80]}'.",
                                    "confidence": 0.0}
                _set_prov(meta, path, source="no_candidates", confidence=0.0,
                          reasoning=aff_cache[affil]["reasoning"])
                rors_for_author.append(None)
            elif len(cands) == 1:
                rid = cands[0]["ror_id"]
                aff_cache[affil] = {"ror_id": rid, "source": "ror_api",
                                    "reasoning": "Sole candidate returned by ROR search.",
                                    "confidence": 1.0}
                _set_prov(meta, path, source="ror_api", confidence=1.0,
                          reasoning="Sole candidate returned by ROR search.")
                rors_for_author.append(rid)
                resolved += 1
            else:
                # ROR returns a ranked list with a `score` per hit. If the
                # top candidate is a clear winner (score ≥ 0.95 AND a margin
                # of ≥ 0.10 over the runner-up) we accept it deterministically.
                top = cands[0]
                top_score = float(top.get("score") or 0)
                next_score = float((cands[1].get("score") if len(cands) > 1 else 0) or 0)
                if top_score >= 0.95 and (top_score - next_score) >= 0.10:
                    rid = top["ror_id"]
                    reasoning = f"Top ROR candidate score={top_score:.2f}, next={next_score:.2f}; clear winner."
                    aff_cache[affil] = {"ror_id": rid, "source": "ror_api",
                                        "reasoning": reasoning, "confidence": 0.95}
                    _set_prov(meta, path, source="ror_api", confidence=0.95,
                              reasoning=reasoning)
                    rors_for_author.append(rid)
                    resolved += 1
                else:
                    # needs_pick — attach candidates, no LLM call
                    meta.setdefault("provenance", {})[path] = {
                        "source": "needs_pick", "confidence": 0.0,
                        "reasoning": f"{len(cands)} candidates returned by ROR (top score={top_score:.2f}); pick one or use AI to adjudicate.",
                        "candidates": _candidates_to_provenance(cands, ["ror_id"]),
                        "query": {"affiliation_string": affil},
                        "task": "ror_pick", "source_api": "ROR",
                    }
                    aff_cache[affil] = {"ror_id": None, "source": "needs_pick",
                                        "reasoning": "Multiple candidates — see provenance.",
                                        "confidence": 0.0}
                    rors_for_author.append(None)
                    needs_pick += 1
        a["ror_ids"] = rors_for_author

    return AutofixReport({"action": "resolve_rors", "ok": True,
                          "resolved": resolved, "needs_pick": needs_pick,
                          "unique_affiliations": len(aff_cache)})


def _autofix_funders(meta: dict, fs: Factsheet) -> AutofixReport:
    funders = _ensure_funders(meta, fs)
    oa = OpenAlexClient()
    resolved = needs_pick = 0

    for i, fu in enumerate(funders):
        if fu.get("doi") or not fu.get("name"):
            continue
        cands = oa.search_funder(fu["name"])
        path = f"funders[{i}].doi"
        if not cands:
            _set_prov(meta, path, source="no_candidates", confidence=0.0,
                      reasoning=f"OpenAlex returned no funder matches for '{fu['name']}'.")
        elif len(cands) == 1:
            fu["doi"] = cands[0].get("doi")
            _set_prov(meta, path, source="openalex_api", confidence=1.0,
                      reasoning="Sole funder candidate returned by OpenAlex.")
            resolved += 1
        else:
            meta.setdefault("provenance", {})[path] = {
                "source": "needs_pick", "confidence": 0.0,
                "reasoning": f"{len(cands)} candidates returned; pick one or use AI to adjudicate.",
                "candidates": _candidates_to_provenance(cands, ["openalex_id", "doi"]),
                "query": {"funder_name": fu["name"]},
                "task": "funder_pick", "source_api": "OpenAlex",
            }
            needs_pick += 1

    return AutofixReport({"action": "resolve_funders", "ok": True,
                          "resolved": resolved, "needs_pick": needs_pick,
                          "out_of": len(funders)})


def _autofix_references(meta: dict, fs: Factsheet, docling_doc: dict) -> AutofixReport:
    """Lookup-first DOI resolution for each reference:
       1. inline regex (already done at extraction time — counted here)
       2. Crossref Works bibliographic search
       3. OpenAlex Works search as a fallback when Crossref misses
       AI is reserved for the leftovers (publisher opt-in via
       /structure/structure_references)."""
    refs = _ensure_references(meta, fs, docling_doc)
    cr = CrossrefClient()
    oa = OpenAlexClient()
    resolved_inline = resolved_crossref = resolved_openalex = 0
    needs_pick = no_candidates = 0

    for i, r in enumerate(refs[:100]):
        path = f"references[{i}].doi"
        if r.get("doi"):
            _set_prov(meta, path, source="regex", confidence=1.0,
                      reasoning="DOI extracted directly from the citation text.")
            resolved_inline += 1
            continue
        # Pass 2 — Crossref Works
        cands = cr.search_work(query=r["raw"][:400])
        if len(cands) == 1:
            r["doi"] = cands[0].get("doi")
            r["title"] = cands[0].get("title")
            _set_prov(meta, path, source="crossref_api", confidence=0.95,
                      reasoning="Sole candidate returned by Crossref Works search.")
            resolved_crossref += 1
            continue
        if len(cands) > 1:
            meta.setdefault("provenance", {})[path] = {
                "source": "needs_pick", "confidence": 0.0,
                "reasoning": f"{len(cands)} candidates returned by Crossref; pick one or use AI to adjudicate.",
                "candidates": _candidates_to_provenance(cands, ["doi"]),
                "query": {"citation": r["raw"][:400]},
                "task": "reference_pick", "source_api": "Crossref Works",
            }
            needs_pick += 1
            continue
        # Pass 3 — OpenAlex fallback (Crossref returned nothing)
        oa_cands = oa.search_work(title=r["raw"][:400])
        if len(oa_cands) == 1 and oa_cands[0].get("doi"):
            r["doi"] = oa_cands[0]["doi"]
            r["title"] = oa_cands[0].get("title")
            _set_prov(meta, path, source="openalex_api", confidence=0.9,
                      reasoning="Sole candidate returned by OpenAlex Works search after Crossref miss.")
            resolved_openalex += 1
            continue
        if len(oa_cands) > 1:
            meta.setdefault("provenance", {})[path] = {
                "source": "needs_pick", "confidence": 0.0,
                "reasoning": f"{len(oa_cands)} candidates returned by OpenAlex; pick one or use AI to adjudicate.",
                "candidates": _candidates_to_provenance(oa_cands, ["doi", "openalex_id"]),
                "query": {"citation": r["raw"][:400]},
                "task": "reference_pick", "source_api": "OpenAlex Works",
            }
            needs_pick += 1
            continue
        _set_prov(meta, path, source="no_candidates", confidence=0.0,
                  reasoning="Neither Crossref nor OpenAlex returned a match for this citation.")
        no_candidates += 1

    return AutofixReport({
        "action": "resolve_references", "ok": True,
        "resolved_inline": resolved_inline,
        "resolved_crossref": resolved_crossref,
        "resolved_openalex": resolved_openalex,
        "resolved": resolved_inline + resolved_crossref + resolved_openalex,
        "needs_pick": needs_pick,
        "no_candidates": no_candidates,
        "out_of": len(refs),
    })


# ============================================================================
# Explicit, opt-in operations: manual pick + paid LLM disambiguation
# ============================================================================

def _apply_value_at_path(meta: dict, path: str, value: Any) -> None:
    """Write a value into the metadata at a path like 'authors[5].orcid' or 'references[12].doi'."""
    m = re.match(r"^([a-z_]+)(?:\[(\d+)\])?(?:\.([a-z_]+)(?:\[(\d+)\])?)?$", path)
    if not m:
        raise ValueError(f"unsupported path: {path}")
    top, i, sub, j = m.group(1), m.group(2), m.group(3), m.group(4)
    if i is None:
        if sub is None:
            meta[top] = value
        else:
            container = meta.setdefault(top, {})
            container[sub] = value
    else:
        i = int(i)
        arr = meta.setdefault(top, [])
        while len(arr) <= i:
            arr.append({})
        if sub is None:
            arr[i] = value
        elif j is None:
            arr[i][sub] = value
        else:
            j = int(j)
            inner = arr[i].setdefault(sub, [])
            while len(inner) <= j:
                inner.append(None)
            inner[j] = value


def apply_pick(meta: dict, path: str, chosen_id: str) -> dict:
    """Apply a manual pick: take the candidate with id=chosen_id from the
    provenance entry at `path`, write its value into the metadata, and update
    provenance to source='user_pick'. NO COST."""
    prov = (meta.get("provenance") or {}).get(path)
    if not prov or prov.get("source") != "needs_pick":
        return {"ok": False, "error": f"no needs_pick entry at {path}"}
    candidates = prov.get("candidates") or []
    chosen = next((c for c in candidates if str(c.get("id")) == str(chosen_id)), None)
    if not chosen:
        return {"ok": False, "error": f"chosen_id {chosen_id!r} not in candidate list"}

    # Pull the actual stored value from the candidate's raw record.
    raw = chosen.get("raw") or {}
    value = raw.get("orcid") or raw.get("ror_id") or raw.get("doi") or raw.get("openalex_id") or chosen.get("id")
    _apply_value_at_path(meta, path, value)
    _set_prov(meta, path, source="user_pick", confidence=1.0,
              reasoning="Manually picked from candidate list by editor.")
    return {"ok": True, "path": path, "chosen_id": chosen_id, "value": value}


def disambiguate_field(meta: dict, path: str, *, sub_id: int | None = None) -> dict:
    """Run ONE LLM disambiguation for the field at `path`. Updates provenance
    and writes the chosen value into the metadata. PAID — costs ~$0.0002.

    Returns: {ok, path, value?, confidence, reasoning, alternatives, usd_estimate?}
    """
    prov = (meta.get("provenance") or {}).get(path)
    if not prov:
        return {"ok": False, "error": f"no provenance at {path}"}
    if prov.get("source") not in ("needs_pick", "needs_review"):
        return {"ok": False, "error": f"field {path} is already resolved (source={prov.get('source')})"}
    candidates = prov.get("candidates") or []
    if not candidates:
        return {"ok": False, "error": f"no candidates stored for {path}"}

    router = router_for_submission(sub_id) if sub_id is not None else LLMRouter()
    raw_candidates = [c.get("raw") or {} for c in candidates]
    d = router.disambiguate(
        task=prov.get("task") or "orcid_pick",
        source=prov.get("source_api") or "enricher",
        query=prov.get("query") or {},
        candidates=raw_candidates,
    )
    alts = _disambiguation_alts(d)

    if d.chosen_id and d.confidence >= CONFIDENCE_THRESHOLD:
        chosen_raw = next((c for c in raw_candidates
                           if str(c.get("orcid") or c.get("ror_id") or c.get("doi") or c.get("openalex_id")) == d.chosen_id),
                          None)
        value = None
        if chosen_raw:
            value = chosen_raw.get("orcid") or chosen_raw.get("ror_id") or chosen_raw.get("doi") or chosen_raw.get("openalex_id")
        if value:
            _apply_value_at_path(meta, path, value)
        _set_prov(meta, path, source="llm_disambiguated", confidence=d.confidence,
                  reasoning=d.reasoning, alternatives=alts)
        # keep the candidate list around so the user can still override
        meta["provenance"][path]["candidates"] = candidates
        return {"ok": True, "path": path, "value": value,
                "confidence": d.confidence, "reasoning": d.reasoning,
                "alternatives": alts}
    else:
        _set_prov(meta, path, source="needs_review", confidence=d.confidence,
                  reasoning=d.reasoning, alternatives=alts)
        meta["provenance"][path]["candidates"] = candidates
        meta["provenance"][path]["query"] = prov.get("query")
        meta["provenance"][path]["task"] = prov.get("task")
        meta["provenance"][path]["source_api"] = prov.get("source_api")
        return {"ok": True, "path": path, "value": None,
                "confidence": d.confidence, "reasoning": d.reasoning,
                "alternatives": alts, "needs_editor_confirmation": True}


def estimate_disambiguation_cost(meta: dict, paths: list[str] | None = None) -> dict:
    """Estimate cost for adjudicating either specified paths or ALL needs_pick fields."""
    prov = meta.get("provenance") or {}
    if paths is None:
        paths = [p for p, v in prov.items() if v.get("source") == "needs_pick"]
    # Mini-tier average: ~500 in / ~150 out tokens per call.
    per_call_usd = (500 / 1_000_000) * 0.15 + (150 / 1_000_000) * 0.60  # ≈ $0.000165
    return {
        "fields": paths,
        "call_count": len(paths),
        "per_call_usd": round(per_call_usd, 6),
        "total_usd": round(per_call_usd * len(paths), 6),
        "model": "gpt-4o-mini",
    }


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
            meta["journal_title"] = ct
            _set_prov(meta, "journal_title", source="crossref_api", confidence=1.0,
                      reasoning=f"From the Crossref record for DOI {doi}.")
            changes.append("journal_title")
    if not meta.get("issn_print") or not meta.get("issn_electronic"):
        for issn in (rec.get("ISSN") or []):
            if not meta.get("issn_electronic"):
                meta["issn_electronic"] = issn
            else:
                meta["issn_print"] = issn
        if rec.get("ISSN"):
            _set_prov(meta, "issn_electronic", source="crossref_api", confidence=1.0,
                      reasoning=f"From the Crossref record for DOI {doi}.")
            changes.append("issn")
    if not meta.get("publication_date"):
        issued = ((rec.get("issued") or {}).get("date-parts") or [[None]])[0]
        if issued and issued[0]:
            meta["publication_date"] = "-".join(f"{x:02d}" if isinstance(x, int) else str(x) for x in issued)
            _set_prov(meta, "publication_date", source="crossref_api", confidence=1.0,
                      reasoning=f"From the Crossref record for DOI {doi}.")
            changes.append("publication_date")
    if not meta.get("volume") and rec.get("volume"):
        meta["volume"] = rec["volume"]
        _set_prov(meta, "volume", source="crossref_api", confidence=1.0,
                  reasoning=f"From the Crossref record for DOI {doi}.")
        changes.append("volume")
    if not meta.get("issue") and rec.get("issue"):
        meta["issue"] = rec["issue"]
        _set_prov(meta, "issue", source="crossref_api", confidence=1.0,
                  reasoning=f"From the Crossref record for DOI {doi}.")
        changes.append("issue")
    if not meta.get("first_page") and rec.get("page"):
        page = rec["page"]
        if "-" in page:
            meta["first_page"], meta["last_page"] = page.split("-", 1)
        else:
            meta["first_page"] = page
        _set_prov(meta, "first_page", source="crossref_api", confidence=1.0,
                  reasoning=f"From the Crossref record for DOI {doi}.")
        changes.append("pages")
    if not meta.get("title") and (rec.get("title") or []):
        meta["title"] = rec["title"][0]
        _set_prov(meta, "title", source="crossref_api", confidence=1.0,
                  reasoning=f"From the Crossref record for DOI {doi}.")
        changes.append("title")
    return AutofixReport({"action": "crossref_by_doi", "ok": True, "changes": changes})


def _autofix_preprint(meta: dict, fs: Factsheet) -> AutofixReport:
    pp = fs.facts.preprint_doi
    if not pp:
        return AutofixReport({"action": "detect_preprint", "ok": False, "error": "no preprint candidate detected"})
    meta["preprint_doi"] = pp
    _set_prov(meta, "preprint_doi", source="factsheet", confidence=0.9,
              reasoning="Preprint-style DOI pattern (bioRxiv / medRxiv / OSF / Research Square) detected in PDF text.")
    return AutofixReport({"action": "detect_preprint", "ok": True, "changes": [f"preprint_doi={pp}"]})
