import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Body, File, HTTPException, UploadFile

log = logging.getLogger("submissions")
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlmodel import select

from ..config import settings
from ..db import Submission, get_session
from ..models import JournalArticleMetadata, SubmissionOut
from ..pipeline import run_parse
from ..services.ner import LABEL_PRESETS, run_ner
from ..services.sections import extract_sections

router = APIRouter(prefix="/submissions", tags=["submissions"])

ALLOWED_SUFFIXES = {".pdf", ".docx", ".doc"}


# --- Upload + status --------------------------------------------------------

@router.post("", response_model=SubmissionOut)
async def upload(background: BackgroundTasks, file: UploadFile = File(...)) -> SubmissionOut:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"Unsupported file type: {suffix}")

    uploads = settings.data_dir / "uploads"
    target = uploads / f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{file.filename}"
    with target.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    with get_session() as s:
        sub = Submission(filename=file.filename or target.name, upload_path=str(target))
        s.add(sub)
        s.commit()
        s.refresh(sub)
        sub_id = sub.id

    background.add_task(run_parse, sub_id)
    return SubmissionOut(id=sub_id, filename=file.filename or target.name, status="uploaded")


@router.get("", response_model=list[SubmissionOut])
def list_submissions() -> list[SubmissionOut]:
    with get_session() as s:
        rows = s.exec(select(Submission).order_by(Submission.created_at.desc())).all()
        return [SubmissionOut(id=r.id, filename=r.filename, status=r.status, error=r.error) for r in rows]


@router.get("/{sub_id}", response_model=SubmissionOut)
def get_submission(sub_id: int) -> SubmissionOut:
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub:
            raise HTTPException(404, "not found")
        return SubmissionOut(id=sub.id, filename=sub.filename, status=sub.status, error=sub.error)


@router.delete("/{sub_id}")
def delete_submission(sub_id: int):
    """Remove a submission: DB row, uploaded file, and all generated artefacts."""
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub:
            raise HTTPException(404, "not found")
        paths_to_remove = [
            sub.upload_path,
            sub.docling_json_path,
            sub.layout_json_path,
            sub.entities_json_path,
            sub.metadata_json_path,
            sub.crossref_xml_path,
        ]
        outputs = settings.data_dir / "outputs"
        # Files we generate by convention but don't track in DB
        paths_to_remove += [
            str(outputs / f"{sub_id}_factsheet.json"),
            str(outputs / f"{sub_id}_cost.json"),
        ]
        # Page images live in a directory of their own
        pages_dir = outputs / f"{sub_id}_pages"

        removed = 0
        for p in paths_to_remove:
            if not p:
                continue
            try:
                Path(p).unlink(missing_ok=True)
                removed += 1
            except Exception as exc:
                log.warning("could not remove %s: %s", p, exc)
        if pages_dir.exists():
            try:
                shutil.rmtree(pages_dir)
                removed += 1
            except Exception as exc:
                log.warning("could not remove %s: %s", pages_dir, exc)

        s.delete(sub)
        s.commit()

    return {"ok": True, "removed_paths": removed}


# --- Sections (Docling layout) ---------------------------------------------

@router.get("/{sub_id}/sections")
def get_sections(sub_id: int):
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        doc = json.loads(Path(sub.docling_json_path).read_text())
    sections = extract_sections(doc)
    summary = [
        {k: v for k, v in s.items() if k != "text"}
        for s in sections
    ]
    return {"sections": summary, "count": len(summary)}


@router.get("/{sub_id}/sections/{section_id}")
def get_section(sub_id: int, section_id: int):
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        doc = json.loads(Path(sub.docling_json_path).read_text())
    sections = extract_sections(doc)
    if section_id < 0 or section_id >= len(sections):
        raise HTTPException(404, "section not found")
    return sections[section_id]


@router.get("/{sub_id}/markdown")
def get_markdown(sub_id: int):
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        doc = json.loads(Path(sub.docling_json_path).read_text())
    return {"markdown": doc.get("markdown") or "", "text": doc.get("text") or ""}


# --- Factsheet (deterministic L0 + L2 + L3 extraction) ---------------------

def _load_factsheet(sub_id: int):
    from ..services.factsheet import Factsheet
    fs_path = settings.data_dir / "outputs" / f"{sub_id}_factsheet.json"
    if not fs_path.exists():
        raise HTTPException(404, "factsheet not built (re-parse this submission)")
    return Factsheet.model_validate_json(fs_path.read_text())


def _load_metadata(sub_id: int) -> dict:
    """Load saved metadata if present, else return empty dict."""
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if sub and sub.metadata_json_path and Path(sub.metadata_json_path).exists():
            return json.loads(Path(sub.metadata_json_path).read_text())
    return {}


def _save_metadata(sub_id: int, meta: dict) -> Path:
    path = settings.data_dir / "outputs" / f"{sub_id}_metadata.json"
    path.write_text(json.dumps(meta, indent=2, default=str))
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if sub:
            sub.metadata_json_path = str(path)
            sub.updated_at = datetime.utcnow()
            s.add(sub)
            s.commit()
    return path


@router.get("/{sub_id}/factsheet")
def get_factsheet(sub_id: int):
    fs_path = settings.data_dir / "outputs" / f"{sub_id}_factsheet.json"
    if not fs_path.exists():
        raise HTTPException(404, "factsheet not built (re-parse this submission)")
    return JSONResponse(json.loads(fs_path.read_text()))


@router.get("/{sub_id}/references_layout")
def get_references_layout(sub_id: int):
    """Layout-detected references section: per-item text + bboxes + page range
    + detection method + confidence. Used by the GUI to show the detected
    section on the page render for editor confirmation."""
    from ..services.references_layout import detect_references
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        docling_doc = json.loads(Path(sub.docling_json_path).read_text())
    return detect_references(docling_doc).model_dump()


# --- Scorecard -------------------------------------------------------------

@router.get("/{sub_id}/score")
def get_score(sub_id: int):
    from ..services.scoring import score
    fs = _load_factsheet(sub_id)
    meta = _load_metadata(sub_id)
    return score(fs, meta).model_dump()


# --- Per-field auto-fix ----------------------------------------------------

class AutofixRequest(BaseModel):
    action: str


@router.post("/{sub_id}/autofix")
def post_autofix(sub_id: int, req: AutofixRequest):
    from ..services.autofix import run_autofix
    from ..services.scoring import score

    fs = _load_factsheet(sub_id)
    meta = _load_metadata(sub_id)

    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        docling_doc = json.loads(Path(sub.docling_json_path).read_text())

    report = run_autofix(req.action, meta, fs, docling_doc, sub_id=sub_id)
    _save_metadata(sub_id, meta)
    new_score = score(fs, meta)
    return {"report": report, "score": new_score.model_dump()}


# --- "Fix everything we can" ------------------------------------------------

@router.post("/{sub_id}/autofix/all")
def post_autofix_all(sub_id: int):
    from ..services.autofix import run_autofix
    from ..services.scoring import score, RUBRIC

    fs = _load_factsheet(sub_id)
    meta = _load_metadata(sub_id)

    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        docling_doc = json.loads(Path(sub.docling_json_path).read_text())

    actions_done: list[dict] = []
    seen: set[str] = set()
    for fd in RUBRIC:
        if fd.bucket != "high" or not fd.autofix_action or fd.autofix_action in seen:
            continue
        seen.add(fd.autofix_action)
        actions_done.append(run_autofix(fd.autofix_action, meta, fs, docling_doc, sub_id=sub_id))

    _save_metadata(sub_id, meta)
    new_score = score(fs, meta)
    sub_status_to_ready(sub_id)
    return {"reports": actions_done, "score": new_score.model_dump()}


def sub_status_to_ready(sub_id: int) -> None:
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if sub and sub.status == "parsed":
            sub.status = "ready"
            sub.updated_at = datetime.utcnow()
            s.add(sub)
            s.commit()


# --- LLM cost ledger -------------------------------------------------------

@router.get("/{sub_id}/cost")
def get_cost(sub_id: int):
    ledger_path = settings.data_dir / "outputs" / f"{sub_id}_cost.json"
    if not ledger_path.exists():
        return {"calls": [], "total_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0}
    return JSONResponse(json.loads(ledger_path.read_text()))


# --- Layout (PDF page images + bounding boxes) -----------------------------

def _load_layout(sub_id: int) -> dict:
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.layout_json_path:
            raise HTTPException(404, "no layout (DOCX uploads or render failed)")
        return json.loads(Path(sub.layout_json_path).read_text())


@router.get("/{sub_id}/pages")
def list_pages(sub_id: int):
    layout = _load_layout(sub_id)
    return {
        "page_count": layout["page_count"],
        "dpi": layout["dpi"],
        "pages": [
            {"page": p["page"], "w_px": p["w_px"], "h_px": p["h_px"], "box_count": len(p["boxes"])}
            for p in layout["pages"]
        ],
    }


@router.get("/{sub_id}/pages/{page_no}/image")
def page_image(sub_id: int, page_no: int):
    layout = _load_layout(sub_id)
    for p in layout["pages"]:
        if p["page"] == page_no:
            return FileResponse(p["image_path"], media_type="image/png")
    raise HTTPException(404, "page not found")


@router.get("/{sub_id}/pages/{page_no}/boxes")
def page_boxes(sub_id: int, page_no: int):
    layout = _load_layout(sub_id)
    for p in layout["pages"]:
        if p["page"] == page_no:
            return {"page": page_no, "w_px": p["w_px"], "h_px": p["h_px"], "boxes": p["boxes"]}
    raise HTTPException(404, "page not found")


# --- NER (on-demand, no persistence) ---------------------------------------

class NerRequest(BaseModel):
    text: str
    labels: dict[str, str] | None = None
    preset: str | None = None  # "header" | "abstract" | "funding" | "references"


@router.get("/presets/labels")
def get_label_presets():
    return LABEL_PRESETS


@router.post("/{sub_id}/ner")
def post_ner(sub_id: int, req: NerRequest):
    labels = req.labels
    if not labels and req.preset:
        labels = LABEL_PRESETS.get(req.preset)
    if not labels:
        raise HTTPException(400, "either `labels` or a known `preset` is required")
    entities = run_ner(req.text, labels)
    return {"entities": entities, "label_count": len(labels), "char_count": len(req.text)}


# --- Save accumulated entities + reconcile with LLM ------------------------

class EntitiesSnapshot(BaseModel):
    """Section-id -> list of entities, plus the labels used per section."""
    per_section: dict[str, list[dict]] = {}
    notes: str | None = None


@router.put("/{sub_id}/entities")
def save_entities(sub_id: int, snapshot: EntitiesSnapshot):
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub:
            raise HTTPException(404, "not found")
        path = settings.data_dir / "outputs" / f"{sub_id}_entities.json"
        path.write_text(snapshot.model_dump_json(indent=2))
        sub.entities_json_path = str(path)
        sub.updated_at = datetime.utcnow()
        s.add(sub)
        s.commit()
    return {"ok": True, "path": str(path)}


@router.get("/{sub_id}/entities")
def get_entities(sub_id: int):
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.entities_json_path:
            raise HTTPException(404, "entities not saved yet")
        return JSONResponse(json.loads(Path(sub.entities_json_path).read_text()))


@router.post("/{sub_id}/reconcile")
def reconcile(sub_id: int):
    from ..services.llm_agent import reconcile_metadata

    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path or not sub.entities_json_path:
            raise HTTPException(400, "need parsed doc and saved entities first")
        doc = json.loads(Path(sub.docling_json_path).read_text())
        ents = json.loads(Path(sub.entities_json_path).read_text())

    metadata = reconcile_metadata(doc, ents, sub_id=sub_id)
    meta_path = settings.data_dir / "outputs" / f"{sub_id}_metadata.json"
    meta_path.write_text(metadata.model_dump_json(indent=2))
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        sub.metadata_json_path = str(meta_path)
        sub.status = "ready"
        sub.updated_at = datetime.utcnow()
        s.add(sub)
        s.commit()
    return metadata.model_dump()


# --- Metadata edit + XML ---------------------------------------------------

@router.get("/{sub_id}/metadata")
def get_metadata(sub_id: int):
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.metadata_json_path:
            raise HTTPException(404, "metadata not ready")
        return JSONResponse(json.loads(Path(sub.metadata_json_path).read_text()))


@router.get("/{sub_id}/provenance")
def get_provenance(sub_id: int):
    """Return just the per-field provenance map for the GUI's audit trail."""
    meta = _load_metadata(sub_id)
    return {"provenance": meta.get("provenance") or {}}


# --- Confirm / reject — editor's teaching signals --------------------------

class ConfirmRequest(BaseModel):
    field_path: str   # e.g. "title", "doi", "authors[3].orcid"


@router.post("/{sub_id}/confirm")
def post_confirm(sub_id: int, req: ConfirmRequest):
    """Editor confirms a field's value is correct. Promotes provenance.confirmed=True."""
    from ..services.scoring import score
    meta = _load_metadata(sub_id)
    fs = _load_factsheet(sub_id)
    prov = meta.setdefault("provenance", {})
    entry = prov.get(req.field_path) or {}
    entry["confirmed"] = True
    entry["confirmed_at"] = datetime.utcnow().isoformat(timespec="seconds")
    prov[req.field_path] = entry
    _save_metadata(sub_id, meta)
    return {"ok": True, "field_path": req.field_path, "score": score(fs, meta).model_dump()}


class LocateSelection(BaseModel):
    page: int
    box_ids: list[int]


class LocateRequest(BaseModel):
    field_path: str
    page: int = 0
    box_ids: list[int] = []
    # Multi-page selections: the editor lasso'd boxes across multiple pages
    # (e.g. a reference list that spans pp.45-49). When provided, takes
    # priority over the single-page (page, box_ids) form.
    selections: list[LocateSelection] | None = None


@router.post("/{sub_id}/locate")
def post_locate(sub_id: int, req: LocateRequest):
    """Editor pointed to box(es) on a PDF page (or pages) to fill a field.
    Joins the text of the selected boxes, optionally regex-extracts an
    identifier if the field expects one (DOI / ORCID / ISSN), and writes
    the value into metadata with provenance source='user_locate'."""
    import re as _re
    from ..services.autofix import _apply_value_at_path
    from ..services.scoring import score

    layout = _load_layout(sub_id)

    # Normalise to a list of (page, box_ids) tuples, in page order.
    selections: list[tuple[int, set[int]]]
    if req.selections:
        selections = [(s.page, set(s.box_ids)) for s in req.selections if s.box_ids]
    elif req.page and req.box_ids:
        selections = [(req.page, set(req.box_ids))]
    else:
        selections = []
    if not selections:
        raise HTTPException(400, "no boxes selected")
    selections.sort(key=lambda t: t[0])

    selected: list[dict] = []
    pages_used: list[int] = []
    for pg, ids in selections:
        page_doc = next((p for p in layout["pages"] if p["page"] == pg), None)
        if not page_doc:
            raise HTTPException(404, f"page {pg} not found")
        page_selected = [b for b in page_doc["boxes"] if b["id"] in ids]
        if page_selected:
            selected.extend(page_selected)
            pages_used.append(pg)
    if not selected:
        raise HTTPException(400, "no boxes selected")
    joined = " ".join((b.get("text") or "").strip() for b in selected if b.get("text"))
    joined = _re.sub(r"\s+", " ", joined).strip()

    # Field-aware extraction
    fp_low = req.field_path.lower()
    value: object = joined
    extraction_note = ""
    if "doi" in fp_low:
        m = _re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", joined, _re.IGNORECASE)
        if m:
            value = m.group(0).rstrip(".,;)]>")
            extraction_note = " (DOI regex-extracted from selection)"
        else:
            raise HTTPException(400, "no DOI pattern found in the selected text")
    elif "orcid" in fp_low:
        m = _re.search(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b", joined)
        if m:
            value = m.group(0)
            extraction_note = " (ORCID regex-extracted)"
        else:
            raise HTTPException(400, "no ORCID pattern (####-####-####-###X) in the selected text")
    elif fp_low.endswith("issn") or fp_low.endswith("issn_electronic") or fp_low.endswith("issn_print"):
        m = _re.search(r"\b\d{4}-\d{3}[\dX]\b", joined)
        if m:
            value = m.group(0)
            extraction_note = " (ISSN regex-extracted)"
        else:
            raise HTTPException(400, "no ISSN pattern (####-###X) in the selected text")
    elif "ror" in fp_low:
        m = _re.search(r"https?://ror\.org/[a-z0-9]+", joined, _re.IGNORECASE)
        if m:
            value = m.group(0)
            extraction_note = " (ROR URL regex-extracted)"
        elif _re.match(r"^[a-z0-9]{6,}$", joined.strip()):
            value = f"https://ror.org/{joined.strip()}"
            extraction_note = " (ROR ID coerced to URL form)"
    elif fp_low in ("references", "references_any", "references_with_doi"):
        # Split the selected boxes into individual references. Strategy:
        #   1. Each selected box is a candidate reference — Docling usually
        #      produces one bbox per reference in a reference list.
        #   2. Boxes that don't open with a numbered marker ("1.", "[1]")
        #      or a Vancouver-style "Surname Initials," pattern are treated
        #      as continuations of the previous reference (paragraph wrap).
        #   3. Any merged group that still looks like multiple refs run
        #      together gets an inline split as a fallback.
        # This handles both numbered (`1. Singh RK ...`) and unnumbered
        # author-year styles (`Singh RK, Dhama K ... PMID: 31006350 Ukoaka
        # BM, Okesanya OJ ...`).
        # Each selected box is treated as one candidate reference — Docling's
        # layout analysis nearly always produces one bbox per ref in a
        # reference list, and the editor's selection is what we trust.
        # Continuation-merging tripped on edge cases (`de Wit E,`, `Lo MK,`,
        # `Aditi, Shariff M.`, hyphenated initials) and silently lost refs,
        # so we drop it.
        # The inline-split fallback below catches the rare box that contains
        # two refs concatenated.
        inline_split_re = _re.compile(
            r"(?<=[.\d])\s+(?=[A-Z][A-Za-zÀ-ſ\-]{2,}(?:[ \-][A-Z][A-Za-zÀ-ſ\-]{2,})?\s+[A-Z]{1,4},)"
        )

        box_texts: list[str] = []
        for b in selected:
            t = _re.sub(r"\s+", " ", (b.get("text") or "")).strip()
            if t and len(t) >= 30:
                box_texts.append(t)
        # Drop a leading "References" / "Bibliography" section header
        if box_texts and box_texts[0].lower().strip(": ").startswith(("references", "bibliography")):
            box_texts = box_texts[1:]

        chunks: list[str] = []
        for t in box_texts:
            for p in inline_split_re.split(t):
                p = p.strip()
                if p and len(p) >= 30:
                    chunks.append(p)

        doi_re = _re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", _re.IGNORECASE)
        year_re = _re.compile(r"\b(19|20)\d{2}\b")
        # PDF text extraction often breaks long URLs at the slashes (e.g.
        # `10.1093/ bioinformatics/bty560`). Glue those back together
        # before running the strict DOI regex so we don't lose the DOI.
        doi_glue_re = _re.compile(
            r"(\b10\.\d{4,9}/)\s+([A-Za-z0-9])"
        )
        refs: list[dict] = []
        for c in chunks:
            normalised = doi_glue_re.sub(r"\1\2", c)
            doi_m = doi_re.search(normalised)
            year_m = year_re.search(c)
            refs.append({
                "raw": c[:1000],
                "doi": (doi_m.group(0).rstrip(".,;)]>") if doi_m else None),
                "title": None,
                "year": int(year_m.group(0)) if year_m else None,
            })
        if not refs:
            raise HTTPException(400, "no reference-shaped chunks found in the selected text")
        with_doi = sum(1 for r in refs if r["doi"])
        value = refs
        extraction_note = (
            f" (split {len(refs)} refs from {len(box_texts)} boxes; {with_doi} DOIs found inline)"
        )
        # Force the write target to `references` regardless of which
        # rubric key the editor clicked Locate on.
        req = LocateRequest(
            page=req.page, box_ids=req.box_ids, selections=req.selections,
            field_path="references",
        )

    meta = _load_metadata(sub_id)
    fs = _load_factsheet(sub_id)
    try:
        _apply_value_at_path(meta, req.field_path, value)
    except Exception as exc:
        raise HTTPException(400, f"could not write to {req.field_path}: {exc}")

    prov = meta.setdefault("provenance", {})
    pages_label = (
        f"page {pages_used[0]}" if len(pages_used) == 1
        else f"pages {pages_used[0]}–{pages_used[-1]}" if pages_used
        else "selection"
    )
    prov[req.field_path] = {
        "source": "user_locate",
        "confidence": 1.0,
        "confirmed": True,
        "reasoning": f"Editor pointed to {len(selected)} box(es) on {pages_label}{extraction_note}.",
        "located_page": pages_used[0] if pages_used else req.page,
        "located_pages": pages_used,
        "located_box_ids": [b["id"] for b in selected],
        "located_text": joined[:400],
    }

    # When references just got written via locate, run the free
    # Crossref→OpenAlex bibliographic lookup pass on every ref that didn't
    # already have an inline DOI. The XML deposit needs DOIs, and waiting
    # for the editor to remember to click "Run automated extraction" leads
    # to incomplete deposits.
    if req.field_path == "references" and isinstance(value, list):
        try:
            from ..services.autofix import _autofix_references
            with get_session() as s:
                sub = s.get(Submission, sub_id)
                docling_doc = (
                    json.loads(Path(sub.docling_json_path).read_text())
                    if sub and sub.docling_json_path else {}
                )
            _autofix_references(meta, fs, docling_doc)
        except Exception:
            # Lookup is best-effort — never block the locate write on it.
            pass

    _save_metadata(sub_id, meta)
    return {
        "ok": True,
        "field_path": req.field_path,
        "value": value,
        "score": score(fs, meta).model_dump(),
    }


class LocateAuthorsAffiliationsRequest(BaseModel):
    page: int
    author_box_ids: list[int] = []
    affiliation_box_ids: list[int] = []


@router.post("/{sub_id}/locate/authors_affiliations")
def post_locate_authors_affiliations(sub_id: int, req: LocateAuthorsAffiliationsRequest):
    """Editor tagged boxes on a page as authors vs. affiliations. Backend joins
    each set's text and runs the structure_authors LLM call to produce a
    properly linked author list (with affiliations attached per superscript
    marker). Single paid call (~$0.0006). Replaces the three sequential
    interactions for the author/affiliation gap cards."""
    from ..services.scoring import score
    from ..services.structurers import structure_authors

    if not req.author_box_ids and not req.affiliation_box_ids:
        raise HTTPException(400, "select at least one author or affiliation box")

    layout = _load_layout(sub_id)
    page = next((p for p in layout["pages"] if p["page"] == req.page), None)
    if not page:
        raise HTTPException(404, f"page {req.page} not found")
    boxes_by_id = {b["id"]: b for b in page["boxes"]}

    def _join(ids: list[int]) -> str:
        out = []
        for bid in ids:
            b = boxes_by_id.get(bid)
            if b and b.get("text"):
                out.append(b["text"].strip())
        return "\n".join(out)

    author_text = _join(req.author_box_ids)
    affil_text = _join(req.affiliation_box_ids)

    fs = _load_factsheet(sub_id)
    meta = _load_metadata(sub_id)
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        docling_doc = json.loads(Path(sub.docling_json_path).read_text())

    report = structure_authors(
        meta, fs, docling_doc, sub_id=sub_id,
        override_blocks={"author_block": author_text, "affiliation_block": affil_text},
    )

    # Annotate provenance with the editor's region selection so the publisher
    # learning loop can replay this on future papers from the same publisher.
    prov = meta.setdefault("provenance", {})
    prov["authors"] = {
        "source": "user_locate+llm_structured",
        "confidence": 0.95,
        "confirmed": True,
        "reasoning": (
            f"Editor pointed to {len(req.author_box_ids)} author box(es) and "
            f"{len(req.affiliation_box_ids)} affiliation box(es) on page {req.page}; "
            f"structure_authors linked them."
        ),
        "located_page": req.page,
        "located_author_box_ids": req.author_box_ids,
        "located_affiliation_box_ids": req.affiliation_box_ids,
        "task": "structure_authors",
    }
    _save_metadata(sub_id, meta)
    return {"report": report, "score": score(fs, meta).model_dump()}


@router.post("/{sub_id}/reject")
def post_reject(sub_id: int, req: ConfirmRequest):
    """Editor rejects a field's value. Clears value at path; flips provenance source
    to 'needs_locate' so the GUI can render a Locate-in-document interaction."""
    from ..services.autofix import _apply_value_at_path
    from ..services.scoring import score
    meta = _load_metadata(sub_id)
    fs = _load_factsheet(sub_id)
    try:
        _apply_value_at_path(meta, req.field_path, None)
    except Exception:
        pass
    prov = meta.setdefault("provenance", {})
    entry = prov.get(req.field_path) or {}
    entry["source"] = "needs_locate"
    entry["confidence"] = 0.0
    entry["confirmed"] = False
    entry["reasoning"] = "Editor rejected the auto-extracted value; awaiting manual locate."
    prov[req.field_path] = entry
    _save_metadata(sub_id, meta)
    return {"ok": True, "field_path": req.field_path, "score": score(fs, meta).model_dump()}


# --- Manual pick (FREE) and explicit AI disambiguation (PAID) --------------

class PickRequest(BaseModel):
    field_path: str   # e.g. "authors[5].orcid", "funders[0].doi", "references[12].doi"
    chosen_id: str    # the `id` of one of the candidates in the provenance list


@router.post("/{sub_id}/pick")
def post_manual_pick(sub_id: int, req: PickRequest):
    """Apply a manual editor pick from the candidate list. ZERO LLM cost."""
    from ..services.autofix import apply_pick
    from ..services.scoring import score
    meta = _load_metadata(sub_id)
    fs = _load_factsheet(sub_id)
    result = apply_pick(meta, req.field_path, req.chosen_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "pick failed"))
    _save_metadata(sub_id, meta)
    return {"result": result, "score": score(fs, meta).model_dump()}


class DisambiguateRequest(BaseModel):
    field_path: str | None = None
    field_paths: list[str] | None = None  # batch — pass either field_path or field_paths


@router.post("/{sub_id}/disambiguate/estimate")
def post_disambiguate_estimate(sub_id: int, req: DisambiguateRequest):
    """Preview the LLM cost before the publisher commits. NO LLM call here."""
    from ..services.autofix import estimate_disambiguation_cost
    meta = _load_metadata(sub_id)
    paths = [req.field_path] if req.field_path else req.field_paths
    return estimate_disambiguation_cost(meta, paths)


# --- Structurers (LLM as content structurer, opt-in) ----------------------

class StructureRequest(BaseModel):
    task: str   # "structure_authors" | "structure_references" | "structure_funding" | "structure_credit"


@router.get("/structure/estimate")
def structure_estimate_all():
    """Cost estimate for every structurer task (no LLM call, no submission needed)."""
    from ..services.structurers import estimate_all_structurers
    return estimate_all_structurers()


@router.post("/{sub_id}/structure/{task}/estimate")
def post_structure_estimate(sub_id: int, task: str):
    from ..services.structurers import estimate_structurer_cost, STRUCTURERS
    if task not in STRUCTURERS:
        raise HTTPException(400, f"unknown structurer: {task}")
    return estimate_structurer_cost(task)


@router.post("/{sub_id}/enrich/all")
def post_enrich_all(sub_id: int):
    """Run every premium AI enrichment task in sequence:
    verify_authors → structure_references → structure_funding → structure_credit.
    Each is publisher-opt-in; this is the 'one click for the works' button."""
    from ..services.scoring import score
    from ..services.structurers import run_structurer

    fs = _load_factsheet(sub_id)
    meta = _load_metadata(sub_id)
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        docling_doc = json.loads(Path(sub.docling_json_path).read_text())

    sequence = ["verify_authors", "structure_references", "structure_funding", "structure_credit"]
    reports = []
    for task in sequence:
        rep = run_structurer(task, meta, fs, docling_doc, sub_id=sub_id)
        reports.append(rep)
    _save_metadata(sub_id, meta)
    return {"reports": reports, "score": score(fs, meta).model_dump()}


class StructureRequest(BaseModel):
    text_override: str | None = None  # editor-located source text (currently
                                      # honoured by structure_credit only)


@router.post("/{sub_id}/structure/{task}")
def post_structure(sub_id: int, task: str,
                   req: StructureRequest | None = Body(default=None)):
    """Run one LLM structurer for this submission. PAID — recorded in cost ledger.

    Optional body: `{"text_override": "..."}` lets the editor supply the
    source text directly (e.g. after using Locate to point at the
    Author Contributions paragraph). Only `structure_credit` honours
    this today; other tasks ignore it harmlessly."""
    from ..services.scoring import score
    from ..services.structurers import run_structurer, STRUCTURERS

    if task not in STRUCTURERS:
        raise HTTPException(400, f"unknown structurer task: {task}")

    fs = _load_factsheet(sub_id)
    meta = _load_metadata(sub_id)
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.docling_json_path:
            raise HTTPException(404, "not parsed yet")
        docling_doc = json.loads(Path(sub.docling_json_path).read_text())

    text_override = req.text_override if req else None
    report = run_structurer(task, meta, fs, docling_doc, sub_id=sub_id,
                            text_override=text_override)
    if report.get("ok"):
        _save_metadata(sub_id, meta)
    return {"report": report, "score": score(fs, meta).model_dump()}


@router.post("/{sub_id}/disambiguate")
def post_disambiguate(sub_id: int, req: DisambiguateRequest):
    """Run AI disambiguation for one or more specified fields. PAID — every
    call goes into the per-submission cost ledger and is the publisher's
    deliberate, opt-in spend.
    """
    from ..services.autofix import disambiguate_field, estimate_disambiguation_cost
    from ..services.scoring import score

    meta = _load_metadata(sub_id)
    fs = _load_factsheet(sub_id)

    paths: list[str]
    if req.field_path:
        paths = [req.field_path]
    elif req.field_paths:
        paths = req.field_paths
    else:
        # default = adjudicate ALL needs_pick fields (the publisher already
        # consented by hitting this endpoint without naming specific paths)
        paths = [p for p, v in (meta.get("provenance") or {}).items()
                 if v.get("source") == "needs_pick"]

    if not paths:
        return {"ok": True, "results": [], "score": score(fs, meta).model_dump()}

    estimate = estimate_disambiguation_cost(meta, paths)
    results = []
    for p in paths:
        results.append(disambiguate_field(meta, p, sub_id=sub_id))

    _save_metadata(sub_id, meta)
    return {
        "ok": True,
        "results": results,
        "estimate": estimate,
        "score": score(fs, meta).model_dump(),
    }


@router.put("/{sub_id}/metadata")
def update_metadata(sub_id: int, metadata: JournalArticleMetadata):
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub:
            raise HTTPException(404, "not found")
        path = Path(sub.metadata_json_path or settings.data_dir / "outputs" / f"{sub_id}_metadata.json")
        path.write_text(metadata.model_dump_json(indent=2))
        sub.metadata_json_path = str(path)
        sub.updated_at = datetime.utcnow()
        s.add(sub)
        s.commit()
    return {"ok": True}


@router.post("/{sub_id}/xml")
def build_xml(sub_id: int):
    from ..services.crossref_xml import build_crossref_xml

    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.metadata_json_path:
            raise HTTPException(400, "metadata not ready")
        meta = JournalArticleMetadata.model_validate_json(Path(sub.metadata_json_path).read_text())
        xml_path = settings.data_dir / "outputs" / f"{sub_id}_crossref.xml"
        build_crossref_xml(meta, xml_path)
        sub.crossref_xml_path = str(xml_path)
        sub.updated_at = datetime.utcnow()
        s.add(sub)
        s.commit()
    return {"path": str(xml_path)}


@router.get("/{sub_id}/xml")
def download_xml(sub_id: int):
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub or not sub.crossref_xml_path:
            raise HTTPException(404, "xml not built")
        return FileResponse(sub.crossref_xml_path, media_type="application/xml", filename=f"{sub_id}_crossref.xml")
