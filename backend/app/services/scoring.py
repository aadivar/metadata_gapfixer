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
Bucket = Literal["high", "medium", "manual"]
Status = Literal["present", "missing"]


class FieldDef(BaseModel):
    key: str
    label: str
    tier: Tier
    weight: int          # 1..10 — higher = more important within tier
    bucket: Bucket       # how to fix when missing
    autofix_action: str | None = None  # name of an /autofix endpoint, if any
    why: str             # one-line "what does this enable?" — shown as hover/help


# ============================================================================
# The rubric
# ============================================================================

RUBRIC: list[FieldDef] = [
    # ─── T0 Depositable ─────────────────────────────────────────────────────
    FieldDef(key="doi",            label="DOI",                tier="T0", weight=10, bucket="high",   autofix_action="from_factsheet", why="Without a DOI you cannot deposit."),
    FieldDef(key="title",          label="Article title",      tier="T0", weight=10, bucket="high",   autofix_action="from_docling_title", why="Mandatory in Crossref schema."),
    FieldDef(key="journal_title",  label="Journal title",      tier="T0", weight=8,  bucket="manual", why="From publisher profile or Crossref-by-ISSN lookup."),
    FieldDef(key="issn",           label="ISSN",               tier="T0", weight=8,  bucket="high",   autofix_action="from_factsheet", why="Identifies the journal."),
    FieldDef(key="publication_year", label="Publication year", tier="T0", weight=7,  bucket="high",   autofix_action="from_factsheet", why="Required for citation."),
    FieldDef(key="authors_any",    label="At least one author", tier="T0", weight=7, bucket="high",   autofix_action="from_factsheet", why="Required."),

    # ─── T1 Discoverable ────────────────────────────────────────────────────
    FieldDef(key="abstract",       label="Abstract",           tier="T1", weight=8,  bucket="high",   autofix_action="from_docling_abstract", why="Indexers and discovery layers rely on it."),
    FieldDef(key="full_author_names", label="Full author names (not initials)", tier="T1", weight=6, bucket="high", autofix_action="from_factsheet", why="Helps disambiguate authors."),
    FieldDef(key="publication_date_full", label="Precise pub date (Y/M/D)", tier="T1", weight=5, bucket="medium", autofix_action="crossref_by_doi", why="Required for ranking and timeliness."),
    FieldDef(key="volume_issue_pages", label="Volume / issue / pages", tier="T1", weight=5, bucket="medium", autofix_action="crossref_by_doi", why="Standard citation completeness."),
    FieldDef(key="license_url",    label="License URL",        tier="T1", weight=6,  bucket="high",   autofix_action="from_factsheet", why="Defines reuse permissions."),
    FieldDef(key="references_any", label="References (any form)", tier="T1", weight=6, bucket="high", autofix_action="from_docling_refs", why="Discoverability and citation graph."),

    # ─── T2 Linkable ────────────────────────────────────────────────────────
    FieldDef(key="orcid_for_corresponding", label="ORCID for corresponding author", tier="T2", weight=8, bucket="high", autofix_action="resolve_orcids", why="Connects this paper to the author's profile."),
    FieldDef(key="orcid_for_all_authors", label="ORCID for every author", tier="T2", weight=7, bucket="high", autofix_action="resolve_orcids", why="Full author disambiguation across the literature."),
    FieldDef(key="ror_for_all_affiliations", label="ROR for every affiliation", tier="T2", weight=7, bucket="high", autofix_action="resolve_rors", why="Connects affiliations to a global registry."),
    FieldDef(key="references_with_doi", label="References with DOIs", tier="T2", weight=8, bucket="high", autofix_action="resolve_references", why="Builds the citation graph at deposit time."),
    FieldDef(key="funder_doi",     label="Funder Registry DOI",  tier="T2", weight=6, bucket="high", autofix_action="resolve_funders", why="Connects funded research to the funder."),
    FieldDef(key="award_numbers",  label="Award / grant numbers", tier="T2", weight=5, bucket="high", autofix_action="from_factsheet", why="Funder reporting and impact attribution."),
    FieldDef(key="abstract_jats",  label="JATS-formatted abstract", tier="T2", weight=4, bucket="medium", why="Structured abstract enables better indexing."),
    FieldDef(key="oa_indicator",   label="Open-access indicator", tier="T2", weight=5, bucket="high", autofix_action="from_license", why="Signals open availability."),

    # ─── T3 Integrity-grade ─────────────────────────────────────────────────
    FieldDef(key="preprint_relation", label="Preprint → version-of-record link", tier="T3", weight=8, bucket="medium", autofix_action="detect_preprint", why="Critical for integrity — links the published paper to its open preprint."),
    FieldDef(key="crossmark_policy", label="Crossmark policy URL", tier="T3", weight=4, bucket="manual", why="Enables update / retraction tracking."),
    FieldDef(key="plain_language_summary", label="Plain-language summary", tier="T3", weight=4, bucket="medium", why="Public-facing accessibility."),
    FieldDef(key="conflict_of_interest", label="Conflict-of-interest statement", tier="T3", weight=5, bucket="high", autofix_action="from_factsheet", why="Required by many publishers, surfaces declared interests."),
    FieldDef(key="data_availability", label="Data availability statement", tier="T3", weight=5, bucket="high", autofix_action="from_factsheet", why="Reproducibility signal."),
    FieldDef(key="copyright_holder", label="Copyright holder", tier="T3", weight=3, bucket="manual", why="Publisher policy field."),
]


# Tier weights for the composite score
TIER_WEIGHTS: dict[Tier, int] = {"T0": 25, "T1": 30, "T2": 30, "T3": 15}


# ============================================================================
# Scoring engine
# ============================================================================

class FieldScore(BaseModel):
    key: str
    label: str
    tier: Tier
    weight: int
    bucket: Bucket
    status: Status
    value_preview: str | None = None
    autofix_action: str | None = None
    why: str


class TierScore(BaseModel):
    tier: Tier
    label: str
    score: int                # 0..100
    fields_present: int
    fields_total: int


class Scorecard(BaseModel):
    composite: int            # 0..100
    interpretation: str
    tiers: list[TierScore]
    fields: list[FieldScore]
    high_impact: list[FieldScore]
    medium: list[FieldScore]
    manual: list[FieldScore]
    facts_summary: dict[str, Any]


TIER_LABELS = {
    "T0": "Depositable",
    "T1": "Discoverable",
    "T2": "Linkable",
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
        complete = sum(1 for a in authors if a.get("given") and a.get("surname"))
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
        all_affs = []
        for a in authors:
            all_affs.extend(a.get("affiliations") or [])
        all_affs += list(fs.affiliations.values())
        if not all_affs:
            return (False, "no affiliations")
        with_ror = sum(1 for a in (_v("authors") or []) if a.get("ror_ids"))
        return (with_ror > 0 and with_ror >= len(all_affs), f"{with_ror}/{len(all_affs)} have ROR")
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

    return (False, None)


def score(factsheet: Factsheet, metadata: dict | None = None) -> Scorecard:
    """Compute a scorecard from a factsheet (and optionally a saved metadata dict)."""
    fields_out: list[FieldScore] = []
    tier_buckets: dict[Tier, dict[str, int]] = {t: {"present_w": 0, "total_w": 0, "n_present": 0, "n_total": 0} for t in TIER_WEIGHTS}

    for fd in RUBRIC:
        present, preview = _present(fd.key, factsheet, metadata)
        fs_field = FieldScore(
            key=fd.key, label=fd.label, tier=fd.tier, weight=fd.weight,
            bucket=fd.bucket, status="present" if present else "missing",
            value_preview=preview, autofix_action=fd.autofix_action, why=fd.why,
        )
        fields_out.append(fs_field)
        tb = tier_buckets[fd.tier]
        tb["total_w"] += fd.weight
        tb["n_total"] += 1
        if present:
            tb["present_w"] += fd.weight
            tb["n_present"] += 1

    tiers: list[TierScore] = []
    composite_num = composite_den = 0
    for t, weights in TIER_WEIGHTS.items():
        tb = tier_buckets[t]
        pct = round(100 * tb["present_w"] / tb["total_w"]) if tb["total_w"] else 0
        tiers.append(TierScore(
            tier=t, label=TIER_LABELS[t], score=pct,
            fields_present=tb["n_present"], fields_total=tb["n_total"],
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

    return Scorecard(
        composite=composite,
        interpretation=interpretation,
        tiers=tiers, fields=fields_out,
        high_impact=high_impact, medium=medium, manual=manual,
        facts_summary=facts_summary,
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
