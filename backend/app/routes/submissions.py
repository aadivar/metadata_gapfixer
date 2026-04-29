import json
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Body, File, HTTPException, UploadFile
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

    metadata = reconcile_metadata(doc, ents)
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
