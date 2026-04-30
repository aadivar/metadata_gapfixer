"""Deterministic pre-processor: extracts everything regex / Docling layout /
header parsing can solve, BEFORE we spend any LLM tokens on it.

The output (Factsheet) is the LLM's input. The LLM never sees the raw markdown —
it sees `facts` (trust literally), `authors`/`affiliations` (already structured),
and `boilerplate` (already isolated). This eliminates ~70% of LLM work and
removes most hallucination opportunities.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import fitz  # PyMuPDF
from pydantic import BaseModel, Field

log = logging.getLogger("factsheet")


# ============================================================================
# Models
# ============================================================================

class Facts(BaseModel):
    doi: Optional[str] = None
    orcids: list[str] = Field(default_factory=list)
    rors: list[str] = Field(default_factory=list)
    issns: list[str] = Field(default_factory=list)
    arxiv_ids: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    license_url: Optional[str] = None
    is_open_access_license: bool = False
    grant_ids: list[dict] = Field(default_factory=list)  # [{id, funder_hint}]
    preprint_doi: Optional[str] = None
    pdf_xmp: dict = Field(default_factory=dict)


class FactAuthor(BaseModel):
    name: str
    given: Optional[str] = None
    surname: Optional[str] = None
    markers: list[str] = Field(default_factory=list)
    is_corresponding: bool = False
    email: Optional[str] = None
    orcid: Optional[str] = None


class Boilerplate(BaseModel):
    funding_text: Optional[str] = None
    conflict_of_interest: Optional[str] = None
    data_availability: Optional[str] = None
    code_availability: Optional[str] = None
    ethics_statement: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)


class Factsheet(BaseModel):
    facts: Facts
    authors: list[FactAuthor] = Field(default_factory=list)
    affiliations: dict[str, str] = Field(default_factory=dict)  # marker -> string
    boilerplate: Boilerplate = Field(default_factory=Boilerplate)
    coverage: dict[str, Any] = Field(default_factory=dict)  # for the scorecard later


# ============================================================================
# Regex patterns
# ============================================================================

DOI_RX        = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
ORCID_RX      = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b")
ROR_RX        = re.compile(r"https?://ror\.org/[a-z0-9]+", re.IGNORECASE)
ISSN_RX       = re.compile(r"\bISSN[\s:]*([\dX]{4}-[\dX]{4})\b|(?<![\d-])\d{4}-\d{3}[\dX](?![\d-])", re.IGNORECASE)
ARXIV_RX      = re.compile(r"\barXiv[:\s]?(\d{4}\.\d{4,5}(?:v\d+)?)\b", re.IGNORECASE)
EMAIL_RX      = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
CC_LICENSE_RX = re.compile(r"https?://creativecommons\.org/licenses/[a-z\-]+/\d+\.\d+/?", re.IGNORECASE)

PREPRINT_DOI_RX = re.compile(
    r"10\.1101/[\d.]+|"           # bioRxiv / medRxiv
    r"10\.21203/rs[\.\d/-]+|"     # Research Square
    r"10\.31219/osf\.io/[a-z0-9]+|"  # OSF Preprints
    r"10\.31234/osf\.io/[a-z0-9]+",
    re.IGNORECASE,
)

# Funder-specific grant ID patterns (extend over time)
GRANT_PATTERNS = [
    (r"\b(R0[1-9]|U[\d]{2}|P[\d]{2}|K[\d]{2}|F[\d]{2})[-\s]?[A-Z]{0,4}[\d]{5,7}\b", "NIH"),
    (r"\bNSF[-\s]?[\d]{6,8}\b", "NSF"),
    (r"\b(?:ERC|H2020|HORIZON)[-\s]?[A-Z0-9]{2,}[-\s]?[\d]{4,}\b", "European Commission"),
    (r"\b\d{6}/Z/\d{2}/Z\b", "Wellcome Trust"),
    (r"\bCIHR[-\s]?(?:PJT|FDN|MOP|GRA|EGM|EUS)[-\s]?[\d]{5,7}\b", "CIHR"),
    (r"\bDFG[-\s]?[A-Z]{2}[\s-][\d]{3,5}/[\d]+[-\s]?[\d]+\b", "DFG"),
    (r"\b\d{2}-\d{5}\b", None),  # generic NSERC-like; keep loose
]


# ============================================================================
# Layer 0 — regex sweeps + PDF metadata
# ============================================================================

def _read_pdf_xmp(pdf_path: Path) -> dict:
    try:
        with fitz.open(str(pdf_path)) as d:
            md = dict(d.metadata or {})
            # Keep only nonempty, exclude binary noise
            return {k: v for k, v in md.items() if v and isinstance(v, str)}
    except Exception:
        return {}


def _normalize_doi(s: str) -> str:
    return s.rstrip(".,;)]>").strip()


def _extract_facts(markdown: str, header_text: str, pdf_path: Path | None) -> Facts:
    f = Facts()

    # PDF /Info dict
    if pdf_path:
        f.pdf_xmp = _read_pdf_xmp(pdf_path)
        for key in ("doi", "DOI"):
            if key in f.pdf_xmp:
                f.doi = _normalize_doi(str(f.pdf_xmp[key]))
                break

    # DOI: prefer earliest occurrence (usually the article's own DOI)
    if not f.doi:
        m = DOI_RX.search(markdown)
        if m:
            f.doi = _normalize_doi(m.group(0))

    # Preprint DOI (look in the WHOLE doc, not just header — preprint statements
    # can appear in cover pages, footnotes, or 'data availability')
    p = PREPRINT_DOI_RX.search(markdown)
    if p:
        f.preprint_doi = _normalize_doi(p.group(0))

    # ORCIDs (dedupe, preserve order)
    seen = set()
    for m in ORCID_RX.finditer(markdown):
        v = m.group(0)
        if v not in seen:
            seen.add(v)
            f.orcids.append(v)

    # ROR IDs
    for m in ROR_RX.finditer(markdown):
        v = m.group(0).rstrip("/")
        if v not in f.rors:
            f.rors.append(v)

    # ISSNs (filter false positives — must appear near "ISSN" OR look like ISSN)
    for m in ISSN_RX.finditer(markdown):
        v = (m.group(1) or m.group(0)).upper()
        if v and v not in f.issns:
            f.issns.append(v)

    # arXiv IDs
    for m in ARXIV_RX.finditer(markdown):
        v = m.group(1)
        if v not in f.arxiv_ids:
            f.arxiv_ids.append(v)

    # Emails (filter — only keep ones that look like author/correspondence emails)
    for m in EMAIL_RX.finditer(markdown):
        v = m.group(0).lower().rstrip(".,;")
        if v not in f.emails:
            f.emails.append(v)

    # License URL
    m = CC_LICENSE_RX.search(markdown)
    if m:
        f.license_url = m.group(0).rstrip("/")
        f.is_open_access_license = True

    # Grant IDs (with funder hint)
    for pattern, funder in GRANT_PATTERNS:
        for m in re.finditer(pattern, markdown, re.IGNORECASE):
            gid = m.group(0).strip()
            if not any(g["id"] == gid for g in f.grant_ids):
                f.grant_ids.append({"id": gid, "funder_hint": funder})

    return f


# ============================================================================
# Layer 2 — Header zone parser (authors + affiliations + superscripts)
# ============================================================================

SUPERSCRIPT_DIGITS = {"⁰":"0","¹":"1","²":"2","³":"3","⁴":"4","⁵":"5","⁶":"6","⁷":"7","⁸":"8","⁹":"9"}
SUPERSCRIPT_LETTERS = {"ᵃ":"a","ᵇ":"b","ᶜ":"c","ᵈ":"d","ᵉ":"e","ᶠ":"f","ᵍ":"g","ʰ":"h","ⁱ":"i",
                       "ʲ":"j","ᵏ":"k","ˡ":"l","ᵐ":"m","ⁿ":"n","ᵒ":"o","ᵖ":"p","ʳ":"r","ˢ":"s",
                       "ᵗ":"t","ᵘ":"u","ᵛ":"v","ʷ":"w","ˣ":"x","ʸ":"y","ᶻ":"z"}
SUPERSCRIPT_SET = set(SUPERSCRIPT_DIGITS) | set(SUPERSCRIPT_LETTERS)
SYMBOL_MARKERS = "*†‡§¶‖#"

# Author name token: at least two letter-tokens, allowing accents, hyphens, apostrophes
NAME_RX = re.compile(r"[A-ZÀ-Ý][\w'\-À-ÿ]+(?:\s+[A-ZÀ-Ý]\.?\,?)?(?:\s+[A-ZÀ-Ý][\w'\-À-ÿ]+)+")

# Affiliation entry: marker (digit/letter/symbol) at start of line, then text
AFFIL_LINE_RX = re.compile(
    r"^\s*([0-9a-zA-Z" + re.escape(SYMBOL_MARKERS) + r"]+|[" +
    "".join(SUPERSCRIPT_SET) + r"]+)\s*[\)\.]?\s+(.{10,})$",
)


def _strip_trailing_markers(token: str) -> tuple[str, list[str]]:
    """From 'Smith¹²' return ('Smith', ['1','2']); from 'Jones*†' → ('Jones', ['*','†']);
    from 'Andréa R Schmitzer a' → ('Andréa R Schmitzer', ['a'])."""
    name = token.rstrip(",.")
    markers: list[str] = []

    # Case A: space-separated trailing markers like "Name a b *"
    while True:
        m = re.search(r"\s+([a-zA-Z\d¹²³⁴⁵⁶⁷⁸⁹⁰" + re.escape(SYMBOL_MARKERS) + r"])\s*$", name)
        if not m:
            break
        marker = m.group(1)
        # Only treat as marker if remaining name still looks like a person name (≥2 caps tokens)
        candidate = name[:m.start()].rstrip()
        cap_tokens = re.findall(r"[A-ZÀ-Ý][a-zà-ÿ\-']+", candidate)
        if len(cap_tokens) < 2:
            break
        # Normalize unicode super → ascii
        if marker in SUPERSCRIPT_DIGITS: marker = SUPERSCRIPT_DIGITS[marker]
        elif marker in SUPERSCRIPT_LETTERS: marker = SUPERSCRIPT_LETTERS[marker]
        markers.append(marker)
        name = candidate

    # Case B: glued trailing markers like "Smith¹²" or "Jones*†"
    while name:
        last = name[-1]
        if last in SUPERSCRIPT_DIGITS:
            markers.append(SUPERSCRIPT_DIGITS[last]); name = name[:-1]
        elif last in SUPERSCRIPT_LETTERS:
            markers.append(SUPERSCRIPT_LETTERS[last]); name = name[:-1]
        elif last in SYMBOL_MARKERS:
            markers.append(last); name = name[:-1]
        elif last.isdigit() and len(name) > 3 and name[-2].isalpha():
            markers.append(last); name = name[:-1]
        else:
            break
    return name.strip(), list(reversed(markers))


def _split_name(full: str) -> tuple[Optional[str], Optional[str]]:
    """Naïve given/surname split. The LLM can normalize later if needed."""
    parts = full.split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return None, parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _parse_author_block(text: str) -> list[FactAuthor]:
    """Author block is a comma+'and'-separated list, optionally with markers."""
    # Normalize 'and' into commas, drop trailing periods
    s = re.sub(r"\s+and\s+", ", ", text, flags=re.IGNORECASE)
    s = s.replace("\n", " ").strip().rstrip(".")
    tokens = [t.strip() for t in s.split(",") if t.strip()]

    authors: list[FactAuthor] = []
    for tok in tokens:
        # If prose like "This/The/We/Background/Abstract" appears inside the token,
        # cut everything from that word onwards (eLife-style assessment text bleeding).
        prose_cut = re.search(r"\b(this|the|we|here|background|abstract|in\s+this|in\s+the\s+present)\b",
                              tok, re.IGNORECASE)
        if prose_cut:
            tok = tok[: prose_cut.start()].strip().rstrip(",")
        if not tok or len(tok) > 60:
            break
        if not re.search(r"[A-ZÀ-Ý][a-zà-ÿ]", tok):
            continue
        name, markers = _strip_trailing_markers(tok)
        if not name or len(name) < 3:
            continue
        # Sanity: a personal name has at most 5 word tokens AND ≥2 of them start with uppercase
        # (filters prose like "a subunit of F1Fo-ATP synthase").
        words = name.split()
        cap_starts = sum(1 for w in words if w[:1].isupper())
        if len(words) > 5 or cap_starts < 2:
            break
        is_corr = any(m in ("*", "✉", "†") for m in markers)
        given, surname = _split_name(name)
        authors.append(FactAuthor(
            name=name, given=given, surname=surname,
            markers=markers, is_corresponding=is_corr,
        ))
    return authors


def _parse_affiliation_block(text: str) -> dict[str, str]:
    """Affiliation block: each line starts with a marker."""
    affs: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) < 12:
            continue
        m = AFFIL_LINE_RX.match(line)
        if m:
            marker_raw = m.group(1).strip()
            body = m.group(2).strip().rstrip(",;.")
            # Normalize marker (super-letter to letter)
            marker = "".join(SUPERSCRIPT_DIGITS.get(c) or SUPERSCRIPT_LETTERS.get(c) or c for c in marker_raw)
            affs[marker] = body
    return affs


_AFFIL_KEYWORDS = (
    "University", "Department", "Institute", "Hospital", "Laboratory",
    "Faculty", "School of", "Centre", "Center", "Université", "Universidad",
    "Universität", "Università", "College", "Inc.", "Ltd.", "GmbH",
    "Academy", "Foundation", "Research Council",
)
_FURNITURE_LABELS = {"page_header", "page_footer", "footnote", "caption"}

# Editorial / publisher boilerplate often present on title pages — skip it
# when classifying author blocks.
_EDITORIAL_REJECT = re.compile(
    r"\b("
    r"preprint|reviewed|revised|submitted|accepted|published|"
    r"version\s+of\s+record|version[\s\-]?\d|"
    r"copyright|©|license|creative\s+commons|cc[-\s]?by|"
    r"doi\.org|crossmark|"
    r"editor|editorial|reviewing|reviewer|"
    r"correspondence|corresponding\s+author|"
    r"received|in\s+revised\s+form"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_author_block(text: str) -> int:
    """Score 0..100 — how confident are we this text is the author block?
    Higher beats other candidates. 0 = reject."""
    if _EDITORIAL_REJECT.search(text):
        return 0
    if any(w in text for w in _AFFIL_KEYWORDS):
        return 0
    if not ("," in text or " and " in text.lower()):
        return 0
    cap_tokens = re.findall(r"[A-ZÀ-Ý][a-zà-ÿ\-']+", text)
    if len(cap_tokens) < 4 or len(text) > 1500:
        return 0
    # Count likely-name pairs: "Firstname Lastname" patterns
    pairs = re.findall(r"[A-ZÀ-Ý][a-zà-ÿ\-']+(?:\s+[A-ZÀ-Ý]\.?){0,2}\s+[A-ZÀ-Ý][a-zà-ÿ\-']+", text)
    # Marker hints — author lines often have trailing single letter/digit/symbol
    marker_hits = len(re.findall(r"[A-Za-zà-ÿ][a-z]+\s*[¹²³⁴⁵⁶⁷⁸⁹⁰a-z*†‡§¶]\b", text))
    score = min(100, len(pairs) * 12 + marker_hits * 3)
    return score


def _parse_header_zone(doc_json: dict, pages: list[dict]) -> tuple[list[FactAuthor], dict[str, str], str]:
    """Walk the first page's text items, classify each by content (not by label),
    and pull out author + affiliation blocks. Robust to the title being labeled
    title / section_header / text — content drives the decision.
    """
    texts = doc_json.get("texts") or []
    page1_items: list[dict] = []
    for t in texts:
        for prov in (t.get("prov") or []):
            if (prov.get("page_no") or prov.get("page")) == 1:
                page1_items.append(t)
                break

    affil_text: list[str] = []
    header_text: list[str] = []
    # Score every candidate; pick the best author block instead of first-match.
    author_candidates: list[tuple[int, str]] = []  # (score, text)

    for item in page1_items:
        label = item.get("label") or ""
        if label in _FURNITURE_LABELS:
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        header_text.append(text)

        starts_with_marker = bool(re.match(
            r"^[\d¹²³⁴⁵⁶⁷⁸⁹⁰a-z*†‡§¶‖#]\s*[\)\.]?\s+", text
        ))
        kw_aff = any(w in text for w in _AFFIL_KEYWORDS)

        score = _looks_like_author_block(text)
        if score > 0:
            author_candidates.append((score, text))
        if kw_aff or starts_with_marker:
            affil_text.append(text)

    # Pick the highest-scoring author block (and any subsequent ones with similar score
    # to handle wrapped lines).
    author_text: list[str] = []
    if author_candidates:
        author_candidates.sort(key=lambda x: -x[0])
        top_score = author_candidates[0][0]
        for score, txt in author_candidates:
            if score >= max(top_score * 0.6, 20):
                author_text.append(txt)

    authors = _parse_author_block("\n".join(author_text)) if author_text else []
    affs = _parse_affiliation_block("\n".join(affil_text)) if affil_text else {}
    return authors, affs, "\n".join(header_text)


def _attach_emails_and_orcids(authors: list[FactAuthor], facts: Facts, header_text: str) -> None:
    """Attach the corresponding-author email and any ORCID inline with the author block."""
    if not authors:
        return

    # If only one corresponding marker, attach the single email (if present in header)
    header_emails = [e for e in EMAIL_RX.findall(header_text)]
    corresponding = [a for a in authors if a.is_corresponding]
    if len(corresponding) == 1 and len(header_emails) == 1:
        corresponding[0].email = header_emails[0].lower()

    # Inline ORCIDs immediately following a name in the header — attach to the nearest author
    # (best-effort: just zip if counts match)
    inline_orcids = ORCID_RX.findall(header_text)
    if inline_orcids and len(inline_orcids) <= len(authors):
        for author, orcid in zip(authors, inline_orcids):
            author.orcid = orcid


# ============================================================================
# Layer 3 — Boilerplate-anchored sections
# ============================================================================

ANCHOR_PATTERNS = {
    "funding_text":         r"(?:^|\n)(?:Funding|Funding\s+sources|Financial\s+support|Funding\s+Information)[\s:.\-]+",
    "conflict_of_interest": r"(?:^|\n)(?:Conflicts?\s+of\s+interest|Competing\s+interests|Declarations?\s+of\s+interest|Disclosure)[\s:.\-]+",
    "data_availability":    r"(?:^|\n)(?:Data\s+availability|Data\s+access|Data\s+statement)[\s:.\-]+",
    "code_availability":    r"(?:^|\n)(?:Code\s+availability|Software\s+availability)[\s:.\-]+",
    "ethics_statement":     r"(?:^|\n)(?:Ethics(?:\s+statement)?|IRB\s+approval|Animal\s+welfare)[\s:.\-]+",
}


def _extract_boilerplate(markdown: str) -> Boilerplate:
    out = Boilerplate()
    for field, pattern in ANCHOR_PATTERNS.items():
        m = re.search(pattern, markdown, re.IGNORECASE | re.MULTILINE)
        if not m:
            continue
        # Take the next ~600 chars or until the next section heading
        start = m.end()
        rest = markdown[start:start + 800]
        # Cut at next heading (markdown ## or single line title)
        cut = re.search(r"\n#{1,3}\s|\n[A-Z][A-Z\s]{6,}\n", rest)
        text = rest[:cut.start()].strip() if cut else rest.strip()
        # Tighten: collapse whitespace, drop trailing junk
        text = re.sub(r"\s+", " ", text).strip().rstrip(".,;")
        if 5 < len(text) < 1500:
            setattr(out, field, text)

    # Keywords boilerplate
    m = re.search(r"(?:^|\n)Keywords?[\s:]+(.{1,400})", markdown, re.IGNORECASE)
    if m:
        kws = re.split(r"[;,·•\n]", m.group(1))
        out.keywords = [k.strip().rstrip(".") for k in kws if 1 < len(k.strip()) < 80][:20]

    return out


# ============================================================================
# Public entry point
# ============================================================================

def build_factsheet(docling_doc: dict, layout_pages: list[dict] | None = None,
                    pdf_path: Path | None = None) -> Factsheet:
    """Build a Factsheet from the Docling output (and optionally the source PDF).

    `docling_doc` is the dict returned by DoclingClient.convert (has `doc` and `markdown`).
    """
    doc_json = docling_doc.get("doc") or {}
    markdown = docling_doc.get("markdown") or ""

    # Header zone parsing FIRST so we know the header text for Facts extraction
    authors, affiliations, header_text = _parse_header_zone(doc_json, layout_pages or [])

    facts = _extract_facts(markdown, header_text, pdf_path)
    _attach_emails_and_orcids(authors, facts, header_text)
    boiler = _extract_boilerplate(markdown)

    coverage = {
        "authors_with_marker":      sum(1 for a in authors if a.markers),
        "authors_with_email":       sum(1 for a in authors if a.email),
        "authors_with_orcid":       sum(1 for a in authors if a.orcid),
        "affiliations_count":       len(affiliations),
        "facts_doi":                bool(facts.doi),
        "facts_license":            bool(facts.license_url),
        "facts_orcids":             len(facts.orcids),
        "facts_rors":               len(facts.rors),
        "facts_grants":             len(facts.grant_ids),
        "facts_preprint":           bool(facts.preprint_doi),
        "boilerplate_funding":      bool(boiler.funding_text),
        "boilerplate_coi":          bool(boiler.conflict_of_interest),
        "boilerplate_data":         bool(boiler.data_availability),
    }

    fs = Factsheet(
        facts=facts,
        authors=authors,
        affiliations=affiliations,
        boilerplate=boiler,
        coverage=coverage,
    )
    log.info("factsheet built: %d authors, %d affiliations, doi=%s preprint=%s",
             len(authors), len(affiliations), facts.doi, facts.preprint_doi)
    return fs
