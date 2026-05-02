"""Score a paper's metadata completeness against four published tiers:

  T0  Depositable           — Crossref schema 5.4.0 required fields
  T1  Discoverable          — Crossref schema recommended fields
  T2  Linkable              — Crossref Participation Reports / Nexus benchmarks
  T3  Integrity-grade       — Joint Crossref+DataCite "metadata for integrity" guide

Each field has a tier and a weight. A field is "present" if extracted from the
factsheet, persisted in metadata, or supplied by the publisher profile.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .factsheet import Factsheet


Tier = Literal["T0", "T1", "T2", "T3"]
# The five Crossref Research Nexus dimensions (per nexus-score.vercel.app
# and Crossref Participation Reports). `mandatory` is the sixth pseudo-
# dimension — Crossref's deposit-minimum gate, which must be satisfied
# before the weighted Research Nexus score even applies.
Dimension = Literal["mandatory", "provenance", "people", "funding", "access", "organizations"]
Bucket = Literal["high", "medium", "manual"]
Status = Literal["present", "missing"]
Leverage = Literal["deterministic", "api", "ai"]


class FieldDef(BaseModel):
    key: str
    label: str
    tier: Tier
    dimension: Dimension = "access"      # which Research Nexus dimension this field belongs to
    weight: int          # 1..10 — higher = more important within dimension
    bucket: Bucket       # how to fix when missing
    autofix_action: str | None = None  # name of an /autofix endpoint, if any
    why: str             # one-line "what does this enable?" — shown as hover/help
    # LLM leverage signal: where does this field's value most usefully come from?
    #   "deterministic" → regex / Docling / PDF metadata; LLM adds nothing
    #   "api"           → free enricher API lookup; LLM only useful for ambiguous picks
    #   "ai"            → LLM is a step-change (prose synthesis, multi-source verification,
    #                     mapping free text → closed taxonomy)
    llm_leverage: Leverage = "deterministic"
    # Indicative cost (USD) per gap when llm_leverage="ai" — used by the
    # GUI to show "~$0.0NN to enrich all AI gaps in this tier" estimates.
    ai_cost_estimate: float = 0.0
    # When llm_leverage="ai", which structurer task should the per-field
    # "Run AI extraction" button hit? Maps to one of the names in
    # `services.structurers.STRUCTURER_TASKS` and the
    # POST /submissions/{id}/structure/{task} endpoint.
    structurer_task: str | None = None


# ============================================================================
# The rubric
# ============================================================================

RUBRIC: list[FieldDef] = [
    # ─── Mandatory (Crossref deposit minimum) ───────────────────────────────
    # These are gating: a record can't even be deposited until they're present.
    FieldDef(key="doi",                   label="DOI",                                 tier="T0", dimension="mandatory", weight=10, bucket="high",   autofix_action="from_factsheet",     llm_leverage="deterministic", why="Without a DOI you cannot deposit."),
    FieldDef(key="title",                 label="Article title",                       tier="T0", dimension="mandatory", weight=10, bucket="high",   autofix_action="from_docling_title", llm_leverage="deterministic", why="Mandatory in Crossref schema."),
    FieldDef(key="journal_title",         label="Journal title",                       tier="T0", dimension="mandatory", weight=8,  bucket="manual",                                       llm_leverage="api",           why="From publisher profile or Crossref-by-ISSN lookup."),
    FieldDef(key="issn",                  label="ISSN",                                tier="T0", dimension="mandatory", weight=8,  bucket="high",   autofix_action="from_factsheet",     llm_leverage="deterministic", why="Identifies the journal."),
    FieldDef(key="publication_year",      label="Publication year",                    tier="T0", dimension="mandatory", weight=7,  bucket="high",   autofix_action="from_factsheet",     llm_leverage="api",           why="Required for citation."),
    FieldDef(key="authors_any",           label="At least one author",                 tier="T0", dimension="mandatory", weight=7,  bucket="high",   autofix_action="from_factsheet",     llm_leverage="ai", ai_cost_estimate=0.0006, structurer_task="structure_authors", why="Required. AI cleans up superscript-marker linking when deterministic fails."),
    FieldDef(key="publication_date_full", label="Precise pub date (Y/M/D)",            tier="T1", dimension="mandatory", weight=5,  bucket="medium", autofix_action="crossref_by_doi",    llm_leverage="api",           why="Crossref-by-DOI lookup."),
    FieldDef(key="volume_issue_pages",    label="Volume / issue / pages",              tier="T1", dimension="mandatory", weight=5,  bucket="medium", autofix_action="crossref_by_doi",    llm_leverage="api",           why="Crossref-by-DOI lookup."),
    FieldDef(key="copyright_holder",      label="Copyright holder",                    tier="T3", dimension="mandatory", weight=3,  bucket="manual",                                      llm_leverage="deterministic", why="Publisher policy — your input."),

    # ─── Provenance (25%) — citations, similarity, update policies ──────────
    FieldDef(key="references_any",        label="References (any form)",               tier="T1", dimension="provenance",  weight=6, bucket="high",   autofix_action="from_docling_refs",    llm_leverage="deterministic", why="Discoverability and citation graph. Layout-aware detection + inline DOI regex during Locate."),
    FieldDef(key="references_with_doi",   label="References with DOIs",                tier="T2", dimension="provenance",  weight=8, bucket="high",   autofix_action="resolve_references",   llm_leverage="api", ai_cost_estimate=0.009, structurer_task="structure_references", why="Lookup-first: inline DOI regex → Crossref bibliographic search → OpenAlex match. AI picks among ambiguous candidates only for the leftovers."),
    FieldDef(key="preprint_relation",     label="Preprint → version-of-record link",   tier="T3", dimension="provenance",  weight=8, bucket="medium", autofix_action="detect_preprint",      llm_leverage="deterministic", why="bioRxiv / medRxiv DOI pattern detection."),
    FieldDef(key="crossmark_policy",      label="Crossmark policy URL",                tier="T3", dimension="provenance",  weight=4, bucket="manual",                                       llm_leverage="deterministic", why="Update / correction / retraction policy."),
    FieldDef(key="conflict_of_interest",  label="Conflict-of-interest statement",      tier="T3", dimension="provenance",  weight=5, bucket="high",   autofix_action="from_factsheet",       llm_leverage="deterministic", why="Boilerplate-anchor extraction."),
    FieldDef(key="data_availability",     label="Data / code availability statement",  tier="T3", dimension="provenance",  weight=5, bucket="high",   autofix_action="from_factsheet",       llm_leverage="deterministic", why="Linkage from the article to its underlying data."),

    # ─── People (20%) — author identity + contributor roles ─────────────────
    FieldDef(key="full_author_names",     label="Full author names (not initials)",    tier="T1", dimension="people",      weight=6, bucket="high",   autofix_action="from_factsheet",       llm_leverage="ai", ai_cost_estimate=0.0006, structurer_task="structure_authors", why="AI splits given/surname and resolves messy formatting."),
    FieldDef(key="orcid_for_corresponding", label="ORCID for corresponding author",    tier="T2", dimension="people",      weight=8, bucket="high",   autofix_action="resolve_orcids",       llm_leverage="ai", ai_cost_estimate=0.001,  structurer_task="verify_authors",   why="Verified by AI across ORCID + OpenAlex + ROR."),
    FieldDef(key="orcid_for_all_authors", label="ORCID for every author",              tier="T2", dimension="people",      weight=7, bucket="high",   autofix_action="resolve_orcids",       llm_leverage="ai", ai_cost_estimate=0.011,  structurer_task="verify_authors",   why="Full author disambiguation across ORCID + OpenAlex + ROR with cited evidence."),
    FieldDef(key="credit_roles",          label="CRediT contributor roles",            tier="T3", dimension="people",      weight=6, bucket="high",                                          llm_leverage="ai", ai_cost_estimate=0.0005, structurer_task="structure_credit", why="LLM maps free-text contributions onto the 14-role CRediT taxonomy."),

    # ─── Funding (20%) — funder + award ─────────────────────────────────────
    FieldDef(key="funder_doi",            label="Funder Registry DOI",                 tier="T2", dimension="funding",     weight=6, bucket="high",   autofix_action="resolve_funders",      llm_leverage="api",          why="OpenAlex funder lookup; AI only when 2+ candidates match."),
    FieldDef(key="award_numbers",         label="Award / grant numbers",               tier="T2", dimension="funding",     weight=5, bucket="high",   autofix_action="from_factsheet",       llm_leverage="ai", ai_cost_estimate=0.0003, structurer_task="structure_funding", why="AI links grant numbers to specific funders + authors."),

    # ─── Access (20%) — discoverability + reuse rights ──────────────────────
    FieldDef(key="abstract",              label="Abstract",                            tier="T1", dimension="access",      weight=8, bucket="high",   autofix_action="from_docling_abstract", llm_leverage="deterministic", why="Indexers and discovery layers rely on it."),
    FieldDef(key="abstract_jats",         label="JATS-formatted abstract",             tier="T2", dimension="access",      weight=4, bucket="medium",                                        llm_leverage="ai", ai_cost_estimate=0.0005, why="Structured JATS markup needs the LLM."),
    FieldDef(key="license_url",           label="License URL",                         tier="T1", dimension="access",      weight=6, bucket="high",   autofix_action="from_factsheet",       llm_leverage="deterministic", why="Defines reuse permissions."),
    FieldDef(key="oa_indicator",          label="Open-access indicator",               tier="T2", dimension="access",      weight=5, bucket="high",   autofix_action="from_license",         llm_leverage="deterministic", why="Derived from license URL."),
    FieldDef(key="plain_language_summary", label="Plain-language summary",             tier="T3", dimension="access",      weight=4, bucket="medium",                                        llm_leverage="ai", ai_cost_estimate=0.0005, why="Locating + extracting from prose if not labeled."),

    # ─── Organizations (15%) — affiliations + RORs ──────────────────────────
    FieldDef(key="affiliations_listed",   label="Affiliations extracted",              tier="T1", dimension="organizations", weight=6, bucket="high", autofix_action="from_factsheet",       llm_leverage="ai", ai_cost_estimate=0.0006, structurer_task="structure_authors", why="Per-author affiliation strings are required to attach ROR IDs."),
    FieldDef(key="ror_for_all_affiliations", label="ROR for every affiliation",        tier="T2", dimension="organizations", weight=7, bucket="high", autofix_action="resolve_rors",         llm_leverage="ai", ai_cost_estimate=0.0,    structurer_task="verify_authors",   why="Resolved as part of the per-author verification (no extra cost)."),
]


# ============================================================================
# Research Nexus dimensions — weights and labels (per nexus-score.vercel.app)
# ============================================================================

DIMENSION_DEFS: list[dict] = [
    {"key": "mandatory",     "label": "Mandatory",     "weight": 0,  "description": "Crossref minimum to register a DOI — DOI, title, author, date, target URL."},
    {"key": "provenance",    "label": "Provenance",    "weight": 25, "description": "Establishes trust through citation links, content verification, and update policies."},
    {"key": "people",        "label": "People",        "weight": 20, "description": "Connects researchers to their work through persistent identifiers and contributor roles."},
    {"key": "funding",       "label": "Funding",       "weight": 20, "description": "Tracks research funding sources and enables compliance reporting."},
    {"key": "access",        "label": "Access",        "weight": 20, "description": "Improves discoverability and clarifies usage rights."},
    {"key": "organizations", "label": "Organizations", "weight": 15, "description": "Links research outputs to institutions and organizations."},
]
DIMENSION_WEIGHT: dict[str, int] = {d["key"]: d["weight"] for d in DIMENSION_DEFS}


# Tier weights for the composite score
TIER_WEIGHTS: dict[Tier, int] = {"T0": 25, "T1": 30, "T2": 30, "T3": 15}


# ============================================================================
# Scoring engine
# ============================================================================

class FieldScore(BaseModel):
    key: str
    label: str
    tier: Tier
    dimension: Dimension = "access"
    weight: int
    bucket: Bucket
    status: Status
    value_preview: str | None = None
    autofix_action: str | None = None
    why: str
    llm_leverage: Leverage = "deterministic"
    ai_cost_estimate: float = 0.0
    structurer_task: str | None = None
    # Provenance for present fields (so the GUI can render confirmed vs pending state)
    provenance_source: str | None = None
    provenance_confidence: float | None = None
    provenance_confirmed: bool = False
    provenance_reasoning: str | None = None
    # Map from the field-key to its actual metadata path(s) so the GUI can call
    # /confirm, /reject, /pick on the right path.
    metadata_paths: list[str] = []


class TierBreakdown(BaseModel):
    deterministic: int = 0
    api: int = 0
    ai: int = 0
    ai_cost_estimate_usd: float = 0.0   # to enrich all unresolved AI gaps in this tier


class TierScore(BaseModel):
    tier: Tier
    label: str
    score: int                # 0..100
    fields_present: int
    fields_total: int
    breakdown: TierBreakdown = TierBreakdown()


class DimensionScore(BaseModel):
    """One of the six dimensions (`mandatory` + the five Research Nexus
    dimensions). `score` is the percentage of weighted points present
    within this dimension. `weight` is the dimension's contribution to the
    overall Research Nexus score (0 for `mandatory`, since that's a gate)."""
    key: Dimension
    label: str
    weight: int
    score: int                # 0..100
    fields_present: int
    fields_total: int
    description: str


class NexusPillar(BaseModel):
    """One of the four Crossref Research Nexus pillars (Participation Reports
    framing). The numerator/denominator are *underlying entity counts*, not
    rubric-field counts — so the pillar tracks progress smoothly (e.g. 7/9
    ORCIDs) rather than flipping only at 100%.
    """
    key: str                  # researchers | funders | organizations | outputs
    label: str
    numerator: int
    denominator: int
    caption: str              # e.g. "7/9 authors with ORCID"
    status: str               # complete | partial | empty | not_applicable


class ResearchNexus(BaseModel):
    pillars: list[NexusPillar]
    pillars_complete: int     # number of pillars at 100%
    pillars_started: int      # number of pillars with any progress > 0
    overall_pct: int          # weighted across pillars (avg of per-pillar %)


class Scorecard(BaseModel):
    composite: int            # 0..100  — legacy tier-weighted composite (kept)
    interpretation: str
    tiers: list[TierScore]
    fields: list[FieldScore]
    high_impact: list[FieldScore]
    medium: list[FieldScore]
    manual: list[FieldScore]
    facts_summary: dict[str, Any]
    estimated_full_enrichment_usd: float = 0.0   # cost to resolve every remaining AI gap
    research_nexus: ResearchNexus | None = None
    # Per-dimension scores (mandatory + five Research Nexus dimensions)
    dimensions: list[DimensionScore] = []
    # Crossref's deposit-readiness gate
    mandatory_ready: bool = False
    mandatory_present: int = 0
    mandatory_total: int = 0
    # Weighted Research Nexus score across the five non-mandatory dimensions
    research_nexus_score: int = 0


TIER_LABELS = {
    "T0": "Depositable",
    "T1": "Discoverable",
    "T2": "Research Nexus",
    "T3": "Integrity-grade",
}


def _present(field: str, fs: Factsheet, meta: dict | None) -> tuple[bool, str | None]:
    """Decide whether a field is present, and return a short preview string."""
    f = fs.facts
    m = meta or {}

    def _v(key: str) -> Any:
        return m.get(key)

    if field == "doi":
        v = _v("doi") or f.doi
        return (bool(v), v)
    if field == "title":
        v = _v("title")
        return (bool(v), v[:80] if v else None)
    if field == "journal_title":
        v = _v("journal_title")
        return (bool(v), v)
    if field == "issn":
        v = _v("issn_print") or _v("issn_electronic") or (f.issns[0] if f.issns else None)
        return (bool(v), v)
    if field == "publication_year":
        v = _v("publication_date") or (f.pdf_xmp.get("creationDate") if isinstance(f.pdf_xmp, dict) else None)
        return (bool(v), str(v)[:10] if v else None)
    if field == "authors_any":
        n = len(_v("authors") or fs.authors or [])
        return (n > 0, f"{n} authors" if n else None)

    if field == "abstract":
        v = _v("abstract")
        return (bool(v), f"{len(v)} chars" if v else None)
    if field == "full_author_names":
        authors = _v("authors") or [a.model_dump() for a in fs.authors]
        if not authors:
            return (False, None)
        # Saved metadata uses `given_name`; factsheet's Author model uses `given`.
        # Accept either, plus `full_name` as a final fallback.
        complete = sum(
            1 for a in authors
            if (a.get("given_name") or a.get("given") or a.get("full_name"))
            and a.get("surname")
        )
        return (complete == len(authors), f"{complete}/{len(authors)} have given+surname")
    if field == "publication_date_full":
        v = _v("publication_date") or ""
        ok = bool(v) and len(v) >= 10  # YYYY-MM-DD
        return (ok, v if v else None)
    if field == "volume_issue_pages":
        v = _v("volume") or _v("first_page")
        return (bool(v), f"vol={_v('volume')} pages={_v('first_page')}-{_v('last_page')}" if v else None)
    if field == "license_url":
        v = _v("license_url") or f.license_url
        return (bool(v), v)
    if field == "references_any":
        refs = _v("references") or []
        return (len(refs) > 0, f"{len(refs)} refs")
    if field == "affiliations_listed":
        # Two angles:
        #   1) every author has at least one affiliation string attached
        #   2) we have at least one affiliation overall
        authors = _v("authors") or [a.model_dump() for a in fs.authors]
        if not authors:
            return (False, "no authors yet — extract those first")
        with_aff = sum(1 for a in authors if (a.get("affiliations") or []))
        unique_affs: set[str] = set()
        for a in authors:
            for aff in (a.get("affiliations") or []):
                if aff and aff.strip():
                    unique_affs.add(aff.strip())
        # Only fall back to factsheet affiliations if metadata authors
        # have nothing — otherwise we double-count.
        if not unique_affs:
            unique_affs.update(s.strip() for s in fs.affiliations.values() if s and s.strip())
        if not unique_affs:
            return (False, "0 affiliations parsed")
        ok = with_aff == len(authors)
        return (ok, f"{len(unique_affs)} unique · {with_aff}/{len(authors)} authors linked")

    if field == "orcid_for_corresponding":
        authors = _v("authors") or [a.model_dump() for a in fs.authors]
        corr = [a for a in authors if a.get("is_corresponding")]
        if not corr:
            return (False, "no corresponding author marked")
        ok = all(a.get("orcid") for a in corr)
        return (ok, f"{sum(1 for a in corr if a.get('orcid'))}/{len(corr)} have ORCID")
    if field == "orcid_for_all_authors":
        authors = _v("authors") or [a.model_dump() for a in fs.authors]
        if not authors:
            return (False, None)
        with_orcid = sum(1 for a in authors if a.get("orcid"))
        return (with_orcid == len(authors), f"{with_orcid}/{len(authors)} have ORCID")
    if field == "ror_for_all_affiliations":
        authors = _v("authors") or [a.model_dump() for a in fs.authors]
        # Map each unique affiliation STRING → did any author resolve it
        # to a ROR? (Multiple authors at the same institution share both
        # the string and the ROR — counting either side without dedup
        # would double-count.)
        aff_has_ror: dict[str, bool] = {}
        for a in authors:
            affs = a.get("affiliations") or []
            rors = a.get("ror_ids") or []
            for j, aff in enumerate(affs):
                key = (aff or "").strip()
                if not key:
                    continue
                ror = rors[j] if j < len(rors) else None
                aff_has_ror[key] = bool(ror) or aff_has_ror.get(key, False)
        if not aff_has_ror:
            # Fall back to the factsheet's affiliations only if metadata
            # has none — otherwise the metadata authors are authoritative.
            for s in fs.affiliations.values():
                if s and s.strip():
                    aff_has_ror[s.strip()] = False
        if not aff_has_ror:
            return (False, "no affiliations to resolve")
        n_affs = len(aff_has_ror)
        n_resolved = sum(1 for v in aff_has_ror.values() if v)
        return (n_resolved == n_affs, f"{n_resolved}/{n_affs} affiliations have ROR")
    if field == "references_with_doi":
        refs = _v("references") or []
        if not refs:
            return (False, "no references")
        with_doi = sum(1 for r in refs if r.get("doi"))
        return (with_doi == len(refs), f"{with_doi}/{len(refs)} have DOI")
    if field == "funder_doi":
        funders = _v("funders") or []
        if not funders:
            return (False, "no funders")
        with_doi = sum(1 for fu in funders if fu.get("doi"))
        return (with_doi == len(funders), f"{with_doi}/{len(funders)} have Funder Registry DOI")
    if field == "award_numbers":
        funders = _v("funders") or []
        grants = f.grant_ids
        if funders:
            ok = all(fu.get("award_numbers") for fu in funders)
            n = sum(len(fu.get("award_numbers") or []) for fu in funders)
            return (ok, f"{n} award numbers across funders")
        return (len(grants) > 0, f"{len(grants)} grant IDs detected" if grants else None)
    if field == "abstract_jats":
        # Out of scope for deterministic detection — flag as missing for now
        return (False, None)
    if field == "oa_indicator":
        return (bool(f.is_open_access_license or _v("license_url")), "license is OA" if f.is_open_access_license else None)

    if field == "preprint_relation":
        v = f.preprint_doi or _v("preprint_doi")
        return (bool(v), v)
    if field == "crossmark_policy":
        v = _v("crossmark_policy_url")
        return (bool(v), v)
    if field == "plain_language_summary":
        v = _v("plain_language_summary")
        return (bool(v), f"{len(v)} chars" if v else None)
    if field == "conflict_of_interest":
        v = fs.boilerplate.conflict_of_interest or _v("conflict_of_interest")
        return (bool(v), v[:80] if v else None)
    if field == "data_availability":
        v = fs.boilerplate.data_availability or _v("data_availability")
        return (bool(v), v[:80] if v else None)
    if field == "copyright_holder":
        v = _v("copyright_holder")
        return (bool(v), v)
    if field == "credit_roles":
        contribs = _v("credit_contributions") or []
        if not contribs:
            return (False, None)
        n_with_roles = sum(1 for c in contribs if (c.get("roles") or []))
        ok = n_with_roles > 0
        total_roles = sum(len(c.get("roles") or []) for c in contribs)
        return (ok, f"{n_with_roles}/{len(contribs)} authors · {total_roles} roles")

    return (False, None)


_FIELD_KEY_TO_PATHS: dict[str, list[str]] = {
    "doi": ["doi"],
    "title": ["title"],
    "journal_title": ["journal_title"],
    "issn": ["issn_electronic", "issn_print"],
    "publication_year": ["publication_date"],
    "abstract": ["abstract"],
    "publication_date_full": ["publication_date"],
    "volume_issue_pages": ["volume", "issue", "first_page"],
    "license_url": ["license_url"],
    "affiliations_listed": ["authors"],   # affiliations live as a sub-property of authors
    "preprint_relation": ["preprint_doi"],
    "oa_indicator": ["is_open_access"],
    "conflict_of_interest": ["conflict_of_interest"],
    "data_availability": ["data_availability"],
    "crossmark_policy": ["crossmark_policy_url"],
    "plain_language_summary": ["plain_language_summary"],
    "copyright_holder": ["copyright_holder"],
    "credit_roles": ["credit_contributions"],
    "references_any": ["references"],
    "references_with_doi": ["references"],
}


def score(factsheet: Factsheet, metadata: dict | None = None) -> Scorecard:
    """Compute a scorecard from a factsheet (and optionally a saved metadata dict)."""
    fields_out: list[FieldScore] = []
    tier_buckets: dict[Tier, dict[str, Any]] = {t: {
        "present_w": 0, "total_w": 0, "n_present": 0, "n_total": 0,
        "det": 0, "api": 0, "ai": 0, "ai_cost": 0.0,
    } for t in TIER_WEIGHTS}
    prov = (metadata or {}).get("provenance") or {}
    total_ai_cost = 0.0

    for fd in RUBRIC:
        present, preview = _present(fd.key, factsheet, metadata)
        paths = _FIELD_KEY_TO_PATHS.get(fd.key, [])
        prov_entry: dict = {}
        for p in paths:
            if p in prov:
                prov_entry = prov[p]
                break
        fs_field = FieldScore(
            key=fd.key, label=fd.label, tier=fd.tier, dimension=fd.dimension, weight=fd.weight,
            bucket=fd.bucket, status="present" if present else "missing",
            value_preview=preview, autofix_action=fd.autofix_action, why=fd.why,
            llm_leverage=fd.llm_leverage, ai_cost_estimate=fd.ai_cost_estimate,
            structurer_task=fd.structurer_task,
            provenance_source=prov_entry.get("source"),
            provenance_confidence=prov_entry.get("confidence"),
            provenance_confirmed=bool(prov_entry.get("confirmed")),
            provenance_reasoning=prov_entry.get("reasoning"),
            metadata_paths=paths,
        )
        fields_out.append(fs_field)
        tb = tier_buckets[fd.tier]
        tb["total_w"] += fd.weight
        tb["n_total"] += 1
        if present:
            tb["present_w"] += fd.weight
            tb["n_present"] += 1
        # Leverage breakdown
        if fd.llm_leverage == "deterministic": tb["det"] += 1
        elif fd.llm_leverage == "api":         tb["api"] += 1
        elif fd.llm_leverage == "ai":
            tb["ai"] += 1
            if not present:  # only count cost for unresolved AI gaps
                tb["ai_cost"] += fd.ai_cost_estimate
                total_ai_cost += fd.ai_cost_estimate

    tiers: list[TierScore] = []
    composite_num = composite_den = 0
    for t, weights in TIER_WEIGHTS.items():
        tb = tier_buckets[t]
        pct = round(100 * tb["present_w"] / tb["total_w"]) if tb["total_w"] else 0
        tiers.append(TierScore(
            tier=t, label=TIER_LABELS[t], score=pct,
            fields_present=tb["n_present"], fields_total=tb["n_total"],
            breakdown=TierBreakdown(
                deterministic=tb["det"], api=tb["api"], ai=tb["ai"],
                ai_cost_estimate_usd=round(tb["ai_cost"], 6),
            ),
        ))
        composite_num += pct * weights
        composite_den += weights
    composite = round(composite_num / composite_den) if composite_den else 0

    interpretation = _interpret(composite, tiers)

    high_impact = [f for f in fields_out if f.status == "missing" and f.bucket == "high"]
    medium      = [f for f in fields_out if f.status == "missing" and f.bucket == "medium"]
    manual      = [f for f in fields_out if f.status == "missing" and f.bucket == "manual"]

    facts_summary = {
        "doi": factsheet.facts.doi,
        "preprint_doi": factsheet.facts.preprint_doi,
        "license_url": factsheet.facts.license_url,
        "orcids_in_pdf": len(factsheet.facts.orcids),
        "rors_in_pdf": len(factsheet.facts.rors),
        "grant_ids": len(factsheet.facts.grant_ids),
        "authors_parsed": len(factsheet.authors),
        "affiliations_parsed": len(factsheet.affiliations),
        "boilerplate_funding": bool(factsheet.boilerplate.funding_text),
        "boilerplate_coi": bool(factsheet.boilerplate.conflict_of_interest),
        "boilerplate_data": bool(factsheet.boilerplate.data_availability),
    }

    research_nexus = _compute_research_nexus(factsheet, metadata or {})
    dimensions = _compute_dimension_scores(fields_out)
    by_dim = {d.key: d for d in dimensions}
    mandatory = by_dim.get("mandatory")
    mandatory_ready = bool(mandatory) and mandatory.score >= 100
    mandatory_present = mandatory.fields_present if mandatory else 0
    mandatory_total = mandatory.fields_total if mandatory else 0
    # Weighted Research Nexus score = sum(dim.weight * dim.score) / sum(dim.weight)
    rn_dims = [d for d in dimensions if d.weight > 0]
    rn_total_weight = sum(d.weight for d in rn_dims) or 1
    rn_score = round(sum(d.weight * d.score for d in rn_dims) / rn_total_weight)

    return Scorecard(
        composite=composite,
        interpretation=interpretation,
        tiers=tiers, fields=fields_out,
        high_impact=high_impact, medium=medium, manual=manual,
        facts_summary=facts_summary,
        estimated_full_enrichment_usd=round(total_ai_cost, 6),
        research_nexus=research_nexus,
        dimensions=dimensions,
        mandatory_ready=mandatory_ready,
        mandatory_present=mandatory_present,
        mandatory_total=mandatory_total,
        research_nexus_score=rn_score,
    )


def _compute_dimension_scores(fields: list[FieldScore]) -> list[DimensionScore]:
    """Roll up the rubric into one score per dimension. Score = weighted
    points present / weighted points possible, expressed as 0..100."""
    out: list[DimensionScore] = []
    for d in DIMENSION_DEFS:
        dim_fields = [f for f in fields if f.dimension == d["key"]]
        present_w = sum(f.weight for f in dim_fields if f.status == "present")
        total_w = sum(f.weight for f in dim_fields) or 1
        score_pct = round(100 * present_w / total_w)
        out.append(DimensionScore(
            key=d["key"], label=d["label"], weight=d["weight"],
            score=score_pct,
            fields_present=sum(1 for f in dim_fields if f.status == "present"),
            fields_total=len(dim_fields),
            description=d["description"],
        ))
    return out


def _compute_research_nexus(fs: Factsheet, meta: dict) -> ResearchNexus:
    """Build the four Crossref Research Nexus pillar stats from the actual
    underlying entity counts (not rubric all-or-nothing). This is what
    Participation Reports surface in their member dashboards."""
    authors = meta.get("authors") or [a.model_dump() for a in fs.authors]

    # Researchers — fraction of authors with a verified ORCID
    n_authors = len(authors)
    n_with_orcid = sum(1 for a in authors if a.get("orcid"))
    if n_authors == 0:
        researchers = NexusPillar(
            key="researchers", label="Researchers (ORCID)",
            numerator=0, denominator=0,
            caption="No authors extracted yet.",
            status="not_applicable",
        )
    else:
        researchers = NexusPillar(
            key="researchers", label="Researchers (ORCID)",
            numerator=n_with_orcid, denominator=n_authors,
            caption=f"{n_with_orcid}/{n_authors} authors with ORCID",
            status="complete" if n_with_orcid == n_authors else
                   "partial" if n_with_orcid > 0 else "empty",
        )

    # Funders — fraction of funders with a Funder Registry DOI
    funders = meta.get("funders") or []
    n_funders = len(funders)
    n_funders_doi = sum(1 for fu in funders if fu.get("doi"))
    grant_ids_count = len(fs.facts.grant_ids or [])
    if n_funders == 0 and grant_ids_count == 0:
        funders_pillar = NexusPillar(
            key="funders", label="Funders (Funder Registry + grants)",
            numerator=0, denominator=0,
            caption="No funders or grant IDs detected yet.",
            status="empty",
        )
    elif n_funders == 0 and grant_ids_count > 0:
        funders_pillar = NexusPillar(
            key="funders", label="Funders (Funder Registry + grants)",
            numerator=0, denominator=grant_ids_count,
            caption=f"0/{grant_ids_count} grant IDs linked to a Funder Registry entry",
            status="partial",   # we have grant IDs, just not yet linked
        )
    else:
        funders_pillar = NexusPillar(
            key="funders", label="Funders (Funder Registry + grants)",
            numerator=n_funders_doi, denominator=n_funders,
            caption=f"{n_funders_doi}/{n_funders} funders with Funder Registry DOI",
            status="complete" if n_funders_doi == n_funders else
                   "partial" if n_funders_doi > 0 else "empty",
        )

    # Organizations — fraction of unique affiliation STRINGS that have a
    # ROR linked. Multiple authors at the same institution share both the
    # affiliation string and the ROR, so we walk authors and dedupe on
    # the affiliation string. We do NOT mix in fs.affiliations.values()
    # any more — when meta["authors"] is populated, that's the authoritative
    # source; otherwise fall back to the factsheet authors (above).
    aff_has_ror: dict[str, bool] = {}
    for a in authors:
        affs = a.get("affiliations") or []
        rors = a.get("ror_ids") or []
        for j, aff in enumerate(affs):
            key = (aff or "").strip()
            if not key:
                continue
            ror = rors[j] if j < len(rors) else None
            # Once we've seen any author resolve this affiliation to a ROR,
            # treat it as resolved — don't let a later None overwrite True.
            aff_has_ror[key] = bool(ror) or aff_has_ror.get(key, False)
    n_affs = len(aff_has_ror)
    n_resolved = sum(1 for v in aff_has_ror.values() if v)
    if n_affs == 0:
        orgs_pillar = NexusPillar(
            key="organizations", label="Organizations (ROR)",
            numerator=0, denominator=0,
            caption="No affiliations parsed yet.",
            status="not_applicable",
        )
    else:
        orgs_pillar = NexusPillar(
            key="organizations", label="Organizations (ROR)",
            numerator=n_resolved, denominator=n_affs,
            caption=f"{n_resolved}/{n_affs} unique affiliations linked to ROR",
            status="complete" if n_resolved == n_affs else
                   "partial" if n_resolved > 0 else "empty",
        )

    # Outputs — fraction of references that have a DOI (cited-by linking)
    references = meta.get("references") or []
    n_refs = len(references)
    n_refs_doi = sum(1 for r in references if r.get("doi"))
    if n_refs == 0:
        outputs_pillar = NexusPillar(
            key="outputs", label="Outputs (refs with DOI)",
            numerator=0, denominator=0,
            caption="No references extracted yet.",
            status="empty",
        )
    else:
        outputs_pillar = NexusPillar(
            key="outputs", label="Outputs (refs with DOI)",
            numerator=n_refs_doi, denominator=n_refs,
            caption=f"{n_refs_doi}/{n_refs} references have a DOI",
            status="complete" if n_refs_doi == n_refs else
                   "partial" if n_refs_doi > 0 else "empty",
        )

    pillars = [researchers, funders_pillar, orgs_pillar, outputs_pillar]
    pillars_complete = sum(1 for p in pillars if p.status == "complete")
    pillars_started = sum(1 for p in pillars if p.status in ("complete", "partial"))
    # Overall % = average of per-pillar pct (treating not_applicable as 0)
    pcts = [
        round(100 * p.numerator / p.denominator) if p.denominator > 0 else 0
        for p in pillars
    ]
    overall = round(sum(pcts) / len(pcts)) if pcts else 0

    return ResearchNexus(
        pillars=pillars,
        pillars_complete=pillars_complete,
        pillars_started=pillars_started,
        overall_pct=overall,
    )


def _interpret(composite: int, tiers: list[TierScore]) -> str:
    t0 = next(t.score for t in tiers if t.tier == "T0")
    t1 = next(t.score for t in tiers if t.tier == "T1")
    t2 = next(t.score for t in tiers if t.tier == "T2")
    if t0 < 80:
        return "Not yet depositable — Crossref minimum required fields are missing."
    if t1 < 50:
        return "Depositable, but the record is bare. It will be hard for indexers to use."
    if t2 < 40:
        return "Depositable and discoverable, but invisible to most cross-system linking."
    if t2 < 70:
        return "Linkable, but the integrity story is thin."
    if composite < 80:
        return "Strong record. A few high-leverage gaps left."
    return "Comprehensive metadata — this record will earn high integrity scores."
