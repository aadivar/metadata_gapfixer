"""LLM structurers — turn messy content regions into clean structured JSON.

Distinct from the picker layer (`disambiguate_field`): structurers take the
RAW text of a content region and produce a full structured record (with
per-field confidence). Used where deterministic regex/parsers fail because
the structure is encoded in prose, mixed-up superscripts, or initials.

Four tasks, each its own opt-in endpoint:
  - structure_authors    — author block + affiliation block → linked records
  - structure_references — citation list + Crossref candidates → enriched refs
  - structure_funding    — funding statement → funder/grant/attribution
  - structure_credit     — Author Contributions section → CRediT roles per author

Cost (gpt-4o-mini, indicative):
  - structure_authors:    ~$0.0006 per paper (single call)
  - structure_references: ~$0.009 per paper (5 batches × 10 refs)
  - structure_funding:    ~$0.0003 per paper
  - structure_credit:     ~$0.0005 per paper
  Full premium = ~$0.011 per paper.

Every call is publisher-opt-in via /structure/{task} and goes into the
per-submission cost ledger.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .enrichers import CrossrefClient, OpenAlexClient, ORCIDClient, RORClient
from .factsheet import Factsheet
from .llm_router import LLMRouter, router_for_submission

log = logging.getLogger("structurers")


# ============================================================================
# CRediT taxonomy (fixed 14-role list per https://credit.niso.org/)
# ============================================================================

CREDIT_TAXONOMY = [
    "Conceptualization",
    "Data curation",
    "Formal Analysis",
    "Funding acquisition",
    "Investigation",
    "Methodology",
    "Project administration",
    "Resources",
    "Software",
    "Supervision",
    "Validation",
    "Visualization",
    "Writing - original draft",
    "Writing - review & editing",
]


# ============================================================================
# 1. structure_authors
# ============================================================================

class StructuredAffiliation(BaseModel):
    string: str
    marker: str | None = None


class OrcidCandidateInline(BaseModel):
    orcid: str
    score: float
    evidence: str


class StructuredAuthor(BaseModel):
    given: str | None = None
    surname: str
    full_name: str
    affiliations: list[StructuredAffiliation] = Field(default_factory=list)
    is_corresponding: bool = False
    email: str | None = None
    orcid_candidates: list[OrcidCandidateInline] = Field(default_factory=list)
    confidence: float = 0.0


class StructuredAuthors(BaseModel):
    authors: list[StructuredAuthor]
    notes: str = ""


_AUTHORS_SYSTEM = """You are a scholarly author/affiliation structurer.

Given the raw text of an article's title page (the author block followed by
the affiliation block), produce a clean structured list of authors with
their affiliations correctly linked.

Rules:
- Use marker characters (Unicode superscripts ¹²³, plain trailing letters
  like 'a' 'b' 'c', or symbols * † ‡ § ¶) to link authors to affiliations.
- A name like "Andrea R Schmitzer*†" with marker '*' for corresponding and
  '†' for institution-3 means: corresponding author, affiliated with the
  institution under marker †.
- Split full names into given + surname when both are present. Preserve
  accents, hyphens, apostrophes exactly. Never invent authors.
- The corresponding author is usually flagged with * or ✉ and/or has a
  shown email. If unsure, set is_corresponding=false.
- Per-author confidence: 0.95+ when the marker linking is unambiguous;
  0.7-0.9 when one inference is involved; below 0.7 when guessing.
- If the input is empty or doesn't look like an author block, return
  authors=[] and explain in notes."""


def _docling_page1_text(docling_doc: dict) -> tuple[str, str]:
    """Return (author_block_text, affiliation_block_text) from page 1 of Docling."""
    doc = docling_doc.get("doc") or {}
    texts = doc.get("texts") or []
    page1: list[str] = []
    title_seen = False
    for t in texts:
        provs = t.get("prov") or []
        if not any((p.get("page_no") or p.get("page")) == 1 for p in provs):
            continue
        label = t.get("label") or ""
        if label in ("page_header", "page_footer", "footnote", "caption"):
            continue
        text = (t.get("text") or "").strip()
        if not text:
            continue
        if not title_seen and label == "title":
            title_seen = True
            continue
        page1.append(text)

    # Heuristic split: separate author-likely lines from affiliation-likely lines
    author_lines, affil_lines = [], []
    for line in page1:
        is_aff = any(w in line for w in (
            "University", "Department", "Institute", "Hospital", "Laboratory",
            "Faculty", "School of", "Centre", "Center", "College",
            "Université", "Universidad", "Universität", "Università",
        ))
        # Very long lines that are mostly prose → skip (this is the abstract bleed)
        if len(line) > 1500:
            continue
        if is_aff:
            affil_lines.append(line)
        else:
            author_lines.append(line)

    return "\n".join(author_lines), "\n".join(affil_lines)


def structure_authors(meta: dict, fs: Factsheet, docling_doc: dict, *,
                      sub_id: int | None = None) -> dict:
    """LLM-structured authors + affiliations. Replaces meta['authors']."""
    author_block, affil_block = _docling_page1_text(docling_doc)
    if not author_block.strip() and not affil_block.strip():
        return {"ok": False, "error": "no title-page text found"}

    user_payload = json.dumps({
        "author_block": author_block[:4000],
        "affiliation_block": affil_block[:4000],
    }, ensure_ascii=False)

    router = router_for_submission(sub_id) if sub_id is not None else LLMRouter()
    result = router.call(
        task="structure_authors",
        system=_AUTHORS_SYSTEM,
        user=user_payload,
        schema=StructuredAuthors,
        strict=False,
    )

    # Map structured authors back into the metadata model shape
    out_authors: list[dict] = []
    for a in result.authors:
        out_authors.append({
            "given_name": a.given,
            "surname": a.surname,
            "full_name": a.full_name,
            "orcid": a.orcid_candidates[0].orcid if a.orcid_candidates else None,
            "is_corresponding": a.is_corresponding,
            "email": a.email,
            "affiliations": [aff.string for aff in a.affiliations],
            "ror_ids": [],
        })
    meta["authors"] = out_authors

    # Provenance entries
    prov = meta.setdefault("provenance", {})
    for i, a in enumerate(result.authors):
        prov[f"authors[{i}]"] = {
            "source": "llm_structured",
            "confidence": a.confidence,
            "reasoning": f"Structured by LLM from page-1 author + affiliation blocks.",
            "task": "structure_authors",
        }
        if a.orcid_candidates:
            prov[f"authors[{i}].orcid"] = {
                "source": "llm_suggested",
                "confidence": a.orcid_candidates[0].score,
                "reasoning": a.orcid_candidates[0].evidence,
                "alternatives": [c.model_dump() for c in a.orcid_candidates],
            }

    return {
        "ok": True, "task": "structure_authors",
        "authors_count": len(out_authors),
        "notes": result.notes,
    }


# ============================================================================
# 2. structure_references
# ============================================================================

class RefCandidate(BaseModel):
    doi: str | None = None
    title: str | None = None
    container_title: str | None = None
    year: int | None = None
    score: float | None = None


class StructuredReference(BaseModel):
    raw: str
    doi: str | None = None
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    match_source: str = "none"   # "crossref" | "openalex" | "none"
    match_evidence: str = ""
    confidence: float = 0.0


class StructuredReferences(BaseModel):
    references: list[StructuredReference]


_REFS_SYSTEM = """You are a scholarly reference enrichment assistant.

For each input citation, you are given the raw citation string AND a list
of candidate works returned by Crossref Works search. Produce one
StructuredReference per input citation, in the same order.

Rules:
- If a candidate clearly matches the citation (author surnames + year +
  first words of title align), pick its DOI and report match_source='crossref'
  with confidence ≥ 0.85 and match_evidence citing what aligned.
- If no candidate is a clear match, set doi=null, match_source='none',
  confidence < 0.5, and explain what was missing.
- NEVER invent a DOI not present in the candidates list.
- Preserve the raw citation exactly as input.
- Authors should be returned as ['Surname, F.', ...] (last-name first, initial)."""


def _split_references_text(docling_doc: dict, fs: Factsheet) -> list[str]:
    """Pull raw reference strings from the references section.

    Uses the layout-aware detector first (handles non-standard heading
    labels and visually-clustered references) and falls back to markdown
    regex if no layout cues are found.
    """
    from .references_layout import detect_references
    layout = detect_references(docling_doc)
    if layout.items:
        return [re.sub(r"\s+", " ", item.text).strip()[:600]
                for item in layout.items if item.text and len(item.text.strip()) > 20]

    md = docling_doc.get("markdown") or ""
    m = re.search(r"(?:^|\n)#{1,3}\s*(?:References|Bibliography|Works\s+cited)\s*\n(.+?)(?:\n#{1,3}\s|\Z)",
                  md, re.IGNORECASE | re.DOTALL)
    refs_text = m.group(1) if m else ""
    if not refs_text:
        return []
    items = re.split(r"\n\s*(?:\d{1,3}\.|\[\d{1,3}\]|\-)\s+", "\n" + refs_text.strip())
    return [re.sub(r"\s+", " ", r).strip()[:600] for r in items if len(r.strip()) > 20]


def structure_references(meta: dict, fs: Factsheet, docling_doc: dict, *,
                         sub_id: int | None = None,
                         batch_size: int = 10, max_refs: int = 100) -> dict:
    """Per-batch LLM enrichment of references. Updates meta['references']."""
    raw_refs = _split_references_text(docling_doc, fs)[:max_refs]
    if not raw_refs:
        return {"ok": False, "error": "no references section found"}

    cr = CrossrefClient()
    router = router_for_submission(sub_id) if sub_id is not None else LLMRouter()

    # Pre-fetch candidates per reference (free) and call LLM per batch
    enriched: list[dict] = []
    for batch_start in range(0, len(raw_refs), batch_size):
        batch = raw_refs[batch_start: batch_start + batch_size]
        items_for_llm: list[dict] = []
        for raw in batch:
            cands = cr.search_work(query=raw[:400])[:5]
            items_for_llm.append({
                "raw": raw,
                "candidates": [{
                    "doi": c.get("doi"),
                    "title": c.get("title"),
                    "container_title": c.get("container_title"),
                    "year": (c.get("issued") or [None])[0] if c.get("issued") else None,
                    "score": c.get("score"),
                } for c in cands],
            })

        result = router.call(
            task="structure_references",
            system=_REFS_SYSTEM,
            user=json.dumps({"items": items_for_llm}, ensure_ascii=False),
            schema=StructuredReferences,
            strict=False,
        )
        for sr in result.references:
            enriched.append(sr.model_dump())

    # Map back into metadata
    refs_out: list[dict] = []
    for i, sr in enumerate(enriched):
        refs_out.append({
            "raw": sr["raw"],
            "doi": sr.get("doi"),
            "title": sr.get("title"),
            "year": sr.get("year"),
        })
        meta.setdefault("provenance", {})[f"references[{i}].doi"] = {
            "source": "llm_structured" if sr.get("doi") else "no_match",
            "confidence": sr.get("confidence", 0.0),
            "reasoning": sr.get("match_evidence", ""),
            "task": "structure_references",
            "match_source": sr.get("match_source"),
        }
    meta["references"] = refs_out

    return {
        "ok": True, "task": "structure_references",
        "total": len(enriched),
        "matched": sum(1 for r in enriched if r.get("doi")),
    }


# ============================================================================
# 3. structure_funding
# ============================================================================

class FunderDoiCandidate(BaseModel):
    doi: str
    score: float


class StructuredFunder(BaseModel):
    funder_name: str
    funder_doi_candidates: list[FunderDoiCandidate] = Field(default_factory=list)
    award_numbers: list[str] = Field(default_factory=list)
    attributed_to_authors: list[str] = Field(default_factory=list)
    raw_evidence: str = ""
    confidence: float = 0.0


class StructuredFunding(BaseModel):
    funders: list[StructuredFunder]


_FUNDING_SYSTEM = """You are a scholarly funding statement structurer.

You are given the raw text of a paper's funding/acknowledgements section
AND the list of authors (with their initials computed). Produce a list of
distinct funders with their associated grants and attributed authors.

Rules:
- Match initials in attributions ('R01-CA12345 to AK') to author full
  names from the provided author list. If 'AK' could be 'Anil Kumar',
  return 'Anil Kumar' in attributed_to_authors.
- award_numbers should preserve formatting ('R01-CA12345', 'PJT-180383').
- raw_evidence is the substring of the funding text that supports each
  funder's row.
- Never invent funders not mentioned in the text.
- Per-funder confidence: 0.9+ when funder name and grant are explicitly
  stated; lower when one is inferred."""


def structure_funding(meta: dict, fs: Factsheet, docling_doc: dict, *,
                      sub_id: int | None = None) -> dict:
    """LLM structuring of funding statement → meta['funders']."""
    funding_text = fs.boilerplate.funding_text or ""
    if not funding_text:
        return {"ok": False, "error": "no funding statement found in factsheet"}

    # Author list with initials
    authors = meta.get("authors") or [a.model_dump() for a in fs.authors]
    author_index = []
    for a in authors:
        full = a.get("full_name") or f"{a.get('given_name') or ''} {a.get('surname') or ''}".strip()
        if not full:
            continue
        # Initials = first letter of each capitalized word
        initials = "".join(w[0].upper() for w in full.split() if w and w[0].isalpha())
        author_index.append({"full_name": full, "initials": initials})

    user_payload = json.dumps({
        "funding_text": funding_text,
        "authors": author_index,
    }, ensure_ascii=False)

    router = router_for_submission(sub_id) if sub_id is not None else LLMRouter()
    result = router.call(
        task="structure_funding",
        system=_FUNDING_SYSTEM,
        user=user_payload,
        schema=StructuredFunding,
        strict=False,
    )

    # Resolve each funder name to a Funder Registry DOI via OpenAlex (free).
    # We add the API hits as alternates; the LLM's chosen DOI candidate (if any)
    # remains the primary.
    oa = OpenAlexClient()
    out_funders: list[dict] = []
    for i, f in enumerate(result.funders):
        api_hits = oa.search_funder(f.funder_name)[:5]
        primary_doi = (f.funder_doi_candidates[0].doi if f.funder_doi_candidates
                       else (api_hits[0].get("doi") if api_hits and api_hits[0].get("doi") else None))
        out_funders.append({
            "name": f.funder_name,
            "doi": primary_doi,
            "award_numbers": f.award_numbers,
        })
        meta.setdefault("provenance", {})[f"funders[{i}]"] = {
            "source": "llm_structured",
            "confidence": f.confidence,
            "reasoning": f.raw_evidence,
            "attributed_to_authors": f.attributed_to_authors,
            "task": "structure_funding",
            "openalex_candidates": api_hits,
        }
    meta["funders"] = out_funders

    return {
        "ok": True, "task": "structure_funding",
        "funders_count": len(out_funders),
    }


# ============================================================================
# 4. structure_credit
# ============================================================================

class CreditRoleEntry(BaseModel):
    role: str   # one of CREDIT_TAXONOMY
    evidence: str
    confidence: float


class StructuredContributor(BaseModel):
    author_name: str
    author_initials: str
    credit_roles: list[CreditRoleEntry] = Field(default_factory=list)


class StructuredCredits(BaseModel):
    contributors: list[StructuredContributor]


CREDIT_SYSTEM_TPL = """You are a CRediT contributor-roles structurer.

Given an Author Contributions paragraph AND the full author list (with
initials), produce one StructuredContributor per author with their CRediT
roles. The CRediT taxonomy is a CLOSED set of 14 roles — you may ONLY use
these:
{taxonomy}

Rules:
- Map informal phrases to CRediT roles:
  * 'designed the experiments / conceived the study' → Conceptualization
    (and often Methodology)
  * 'performed the experiments / collected data' → Investigation
  * 'analysed data / statistical analysis' → Formal Analysis
  * 'wrote the manuscript / drafted the paper' → Writing - original draft
  * 'edited / revised the manuscript' → Writing - review & editing
  * 'managed the project / coordinated' → Project administration
  * 'supervised' → Supervision
  * 'obtained funding / acquired funding' → Funding acquisition
- Match initials ('AK', 'BC', 'DE') to the corresponding author full names
  from the author list provided.
- Every author in the list must appear in contributors, even if you can
  only assign them no roles (empty credit_roles list).
- evidence should quote the substring of the contributions text supporting
  each role assignment.
- Never invent roles outside the 14-term taxonomy."""


def _find_credit_section(docling_doc: dict) -> str | None:
    md = docling_doc.get("markdown") or ""
    m = re.search(
        r"(?:^|\n)#{1,3}\s*(?:Author\s+contributions?|Author\s+contribution\s+statements?|"
        r"CRediT\s+(?:taxonomy|author\s+statement)|Contributions?)\s*\n(.+?)(?:\n#{1,3}\s|\Z)",
        md, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    text = re.sub(r"\s+", " ", m.group(1)).strip()
    return text[:3000] if text else None


def structure_credit(meta: dict, fs: Factsheet, docling_doc: dict, *,
                     sub_id: int | None = None) -> dict:
    """LLM mapping of an Author Contributions section to CRediT roles per author."""
    credit_text = _find_credit_section(docling_doc)
    if not credit_text:
        return {"ok": False, "error": "no Author Contributions section found"}

    authors = meta.get("authors") or [a.model_dump() for a in fs.authors]
    author_index = []
    for a in authors:
        full = a.get("full_name") or f"{a.get('given_name') or ''} {a.get('surname') or ''}".strip()
        if not full:
            continue
        initials = "".join(w[0].upper() for w in full.split() if w and w[0].isalpha())
        author_index.append({"full_name": full, "initials": initials})

    if not author_index:
        return {"ok": False, "error": "no authors to attribute roles to"}

    user_payload = json.dumps({
        "contributions_text": credit_text,
        "authors": author_index,
    }, ensure_ascii=False)

    router = router_for_submission(sub_id) if sub_id is not None else LLMRouter()
    result = router.call(
        task="structure_credit",
        system=CREDIT_SYSTEM_TPL.format(taxonomy="\n  - " + "\n  - ".join(CREDIT_TAXONOMY)),
        user=user_payload,
        schema=StructuredCredits,
        strict=False,
    )

    # Filter out hallucinated roles (defensive)
    valid_roles = set(r.lower() for r in CREDIT_TAXONOMY)
    contributions: list[dict] = []
    for c in result.contributors:
        roles = [
            {"role": cr.role, "evidence": cr.evidence, "confidence": cr.confidence}
            for cr in c.credit_roles
            if cr.role.lower() in valid_roles
        ]
        contributions.append({
            "author_name": c.author_name,
            "author_initials": c.author_initials,
            "roles": roles,
        })
    meta["credit_contributions"] = contributions
    meta.setdefault("provenance", {})["credit_contributions"] = {
        "source": "llm_structured",
        "confidence": 0.9,
        "reasoning": "Mapped from Author Contributions section using the 14-role CRediT taxonomy.",
        "task": "structure_credit",
    }

    return {
        "ok": True, "task": "structure_credit",
        "contributors": len(contributions),
        "total_roles": sum(len(c["roles"]) for c in contributions),
    }


# ============================================================================
# 5. verify_authors — multi-source author + affiliation verification
# ============================================================================

class VerifiedAffiliation(BaseModel):
    string: str
    verified_ror: str | None = None
    verified_ror_name: str | None = None


class RejectedAlternative(BaseModel):
    id: str
    reason: str


class VerifiedAuthor(BaseModel):
    author_name: str
    verified_orcid: str | None = None
    openalex_id: str | None = None
    verified_affiliations: list[VerifiedAffiliation] = Field(default_factory=list)
    evidence_chain: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    alternatives_rejected: list[RejectedAlternative] = Field(default_factory=list)


_VERIFY_SYSTEM = """You are a scholarly author verification assistant.

You will receive an author's name + affiliation(s) as they appear in a
published paper, AND a dossier of candidate matches from THREE authoritative
sources:
  - ORCID (with each candidate's employment / education history)
  - OpenAlex (with each candidate's work count, top institutions, country)
  - ROR (with each candidate's preferred display name, country, type)

Cross-check across all three sources. Return a verified_orcid, an openalex_id,
a verified_ror per affiliation, an evidence_chain (2-4 short factual statements
citing what aligned), and a confidence in [0,1].

Confidence rules:
- ≥ 0.9 requires evidence from at least 2 of the 3 sources (ORCID employment,
  OpenAlex affiliation, ROR display-name match).
- 0.7–0.9: one source matches strongly; another is plausible.
- < 0.7: leave verified_orcid null; list candidates in alternatives_rejected
  with concrete reasons.

Hard rules:
- NEVER invent ORCIDs or RORs that are not in the candidate lists provided.
- If a candidate's field of work is clearly different from the paper's domain
  (e.g. computer science profile vs a chemistry paper), reject it with that
  reason.
- evidence_chain entries are SHORT factual lines, not generic praise."""


def _author_initials(full_name: str) -> str:
    return "".join(w[0].upper() for w in (full_name or "").split() if w and w[0].isalpha())


def _orcid_candidate_dossier(orcid_client: ORCIDClient, given: str | None,
                             family: str | None, affil: str | None,
                             max_with_record: int = 2) -> list[dict]:
    """Search ORCID by name; for the top few candidates, also fetch their full
    record so the LLM has employment history to reason against."""
    if not family:
        return []
    candidates = orcid_client.search(given_name=given, family_name=family,
                                     affiliation=affil)[:5]
    out: list[dict] = []
    for i, c in enumerate(candidates):
        orcid_id = c.get("orcid")
        if not orcid_id:
            continue
        rec = orcid_client.record(orcid_id) if i < max_with_record else None
        employment = []
        if isinstance(rec, dict):
            try:
                # ORCID public API: activities-summary.employments.affiliation-group[*]
                af = (rec.get("activities-summary") or {}).get("employments") or {}
                groups = af.get("affiliation-group") or []
                for g in groups:
                    summaries = g.get("summaries") or []
                    for s in summaries:
                        emp = s.get("employment-summary") or {}
                        org = emp.get("organization") or {}
                        start = (emp.get("start-date") or {}).get("year", {}).get("value")
                        end = (emp.get("end-date") or {}).get("year", {}).get("value")
                        employment.append({
                            "organization": org.get("name"),
                            "department": emp.get("department-name"),
                            "role": emp.get("role-title"),
                            "start_year": start,
                            "end_year": end,
                        })
            except Exception:
                employment = []
        out.append({"orcid": orcid_id, "employment": employment[:8]})
    return out


def _openalex_author_dossier(oa_client: OpenAlexClient, full_name: str,
                             affil: str | None) -> list[dict]:
    return oa_client.search_author(full_name, affil)[:5]


def _ror_dossier(ror_client: RORClient, affil: str) -> list[dict]:
    return ror_client.search(affil)[:5]


def verify_authors(meta: dict, fs: Factsheet, docling_doc: dict, *,
                   sub_id: int | None = None) -> dict:
    """Per-author multi-source verification (ORCID + OpenAlex + ROR). Updates
    meta['authors'][i].orcid and meta['authors'][i].ror_ids with cited evidence."""
    authors = meta.get("authors") or []
    if not authors:
        # Bootstrap from factsheet if metadata authors is empty
        for a in fs.authors:
            authors.append({
                "given_name": a.given,
                "surname": a.surname,
                "full_name": a.name,
                "orcid": a.orcid,
                "is_corresponding": a.is_corresponding,
                "email": a.email,
                "affiliations": [fs.affiliations[m] for m in a.markers if m in fs.affiliations],
                "ror_ids": [],
            })
        meta["authors"] = authors
    if not authors:
        return {"ok": False, "error": "no authors to verify"}

    orcid = ORCIDClient()
    oa = OpenAlexClient()
    ror = RORClient()
    router = router_for_submission(sub_id) if sub_id is not None else LLMRouter()

    # Cache ROR results per unique affiliation string
    ror_cache: dict[str, list[dict]] = {}

    verified_count = 0
    needs_review = 0
    for i, a in enumerate(authors):
        full = a.get("full_name") or f"{a.get('given_name') or ''} {a.get('surname') or ''}".strip()
        family = a.get("surname")
        given = a.get("given_name")
        affils = a.get("affiliations") or []
        primary_affil = affils[0] if affils else None

        # Pre-fetch candidate dossiers (free APIs)
        orcid_dossier = _orcid_candidate_dossier(orcid, given, family, primary_affil)
        oa_dossier = _openalex_author_dossier(oa, full, primary_affil)
        ror_dossiers: dict[str, list[dict]] = {}
        for affil in affils:
            if affil not in ror_cache:
                ror_cache[affil] = _ror_dossier(ror, affil)
            ror_dossiers[affil] = ror_cache[affil]

        if not orcid_dossier and not oa_dossier and not any(ror_dossiers.values()):
            meta.setdefault("provenance", {})[f"authors[{i}]"] = {
                "source": "no_candidates", "confidence": 0.0,
                "reasoning": "No ORCID / OpenAlex / ROR candidates returned.",
            }
            continue

        # Single LLM call per author
        user_payload = json.dumps({
            "author": {"name": full, "given": given, "family": family,
                       "affiliations": affils, "is_corresponding": a.get("is_corresponding")},
            "orcid_candidates": orcid_dossier,
            "openalex_candidates": oa_dossier,
            "ror_candidates_per_affiliation": ror_dossiers,
        }, ensure_ascii=False)

        result = router.call(
            task="verify_authors",
            system=_VERIFY_SYSTEM,
            user=user_payload,
            schema=VerifiedAuthor,
            strict=False,
        )

        # Apply to metadata
        if result.verified_orcid and result.confidence >= 0.7:
            a["orcid"] = result.verified_orcid
            verified_count += 1
        elif result.confidence < 0.7:
            needs_review += 1
        # ROR per affiliation
        ror_ids: list[str | None] = []
        for affil in affils:
            match = next((va for va in result.verified_affiliations
                          if (va.string or "").strip() == affil.strip()), None)
            ror_ids.append(match.verified_ror if (match and match.verified_ror) else None)
        a["ror_ids"] = ror_ids

        # Provenance for the author block
        prov = meta.setdefault("provenance", {})
        prov[f"authors[{i}]"] = {
            "source": "llm_verified",
            "confidence": result.confidence,
            "confirmed": False,
            "reasoning": " · ".join(result.evidence_chain) if result.evidence_chain else "Verified across ORCID + OpenAlex + ROR.",
            "evidence_chain": result.evidence_chain,
            "openalex_id": result.openalex_id,
            "alternatives_rejected": [alt.model_dump() for alt in result.alternatives_rejected],
            "task": "verify_authors",
        }
        if result.verified_orcid:
            prov[f"authors[{i}].orcid"] = {
                "source": "llm_verified",
                "confidence": result.confidence,
                "reasoning": " · ".join(result.evidence_chain),
            }

    return {
        "ok": True, "task": "verify_authors",
        "authors_total": len(authors),
        "verified": verified_count,
        "needs_review": needs_review,
    }


# ============================================================================
# Cost estimation
# ============================================================================

# Indicative token usage per task — refined as we observe real costs
_TASK_TOKEN_ESTIMATES: dict[str, tuple[int, int, int]] = {
    # task: (avg_in_per_call, avg_out_per_call, calls_per_paper)
    "structure_authors":    (1_500,   500, 1),
    "structure_references": (3_500, 2_000, 5),
    "structure_funding":    (1_000,   400, 1),
    "structure_credit":     (1_500,   500, 1),
    "verify_authors":       (3_000,   800, 11),  # one call per author; assume ~11
}

_PRICE_MINI = (0.15 / 1_000_000, 0.60 / 1_000_000)  # (input, output) USD/token


def estimate_structurer_cost(task: str) -> dict:
    in_tok, out_tok, calls = _TASK_TOKEN_ESTIMATES.get(task, (2_000, 1_000, 1))
    per_call = in_tok * _PRICE_MINI[0] + out_tok * _PRICE_MINI[1]
    return {
        "task": task,
        "calls": calls,
        "per_call_usd": round(per_call, 6),
        "total_usd": round(per_call * calls, 6),
        "model": "gpt-4o-mini",
    }


def estimate_all_structurers() -> dict:
    rows = [estimate_structurer_cost(t) for t in _TASK_TOKEN_ESTIMATES]
    return {
        "tasks": rows,
        "grand_total_usd": round(sum(r["total_usd"] for r in rows), 6),
    }


# ============================================================================
# Public dispatcher
# ============================================================================

STRUCTURERS = {
    "structure_authors":    structure_authors,
    "structure_references": structure_references,
    "structure_funding":    structure_funding,
    "structure_credit":     structure_credit,
    "verify_authors":       verify_authors,
}


def run_structurer(task: str, meta: dict, fs: Factsheet, docling_doc: dict, *,
                   sub_id: int | None = None) -> dict:
    fn = STRUCTURERS.get(task)
    if not fn:
        return {"ok": False, "error": f"unknown structurer task: {task}"}
    try:
        return fn(meta, fs, docling_doc, sub_id=sub_id)
    except Exception as exc:
        log.exception("structurer %s failed", task)
        return {"ok": False, "error": str(exc), "task": task}
