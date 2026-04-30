"""Pipeline: parse with Docling, then render PDF pages + extract layout boxes.

NER and LLM reconciliation remain user-triggered from the GUI for step-by-step
debugging.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from .config import settings
from .db import Submission, get_session
from .services.docling_client import DoclingClient
from .services.factsheet import build_factsheet
from .services.page_render import render_pages_and_layout

log = logging.getLogger("pipeline")


def _set_status(sub_id: int, status: str, **fields) -> None:
    with get_session() as s:
        sub = s.get(Submission, sub_id)
        if not sub:
            return
        sub.status = status
        sub.updated_at = datetime.utcnow()
        for k, v in fields.items():
            setattr(sub, k, v)
        s.add(sub)
        s.commit()


def run_parse(sub_id: int) -> None:
    """Parse the upload with Docling and (for PDFs) render layout overlays."""
    try:
        with get_session() as s:
            sub = s.get(Submission, sub_id)
            if not sub:
                return
            upload_path = Path(sub.upload_path)

        outputs = settings.data_dir / "outputs"
        _set_status(sub_id, "parsing")

        doc = DoclingClient().convert(upload_path)
        docling_path = outputs / f"{sub_id}_docling.json"
        docling_path.write_text(json.dumps(doc, indent=2))

        # Render pages + layout for PDFs only.
        layout_path: str | None = None
        layout_pages: list[dict] = []
        if upload_path.suffix.lower() == ".pdf":
            try:
                pages_dir = outputs / f"{sub_id}_pages"
                layout = render_pages_and_layout(upload_path, doc, pages_dir)
                layout_pages = layout.get("pages") or []
                lp = outputs / f"{sub_id}_layout.json"
                lp.write_text(json.dumps(layout, indent=2))
                layout_path = str(lp)
            except Exception:
                log.exception("layout render failed (continuing without overlays)")

        # Deterministic pre-processor — no LLM cost. Powers the scorecard
        # and is the LLM agent's input on subsequent reconciliation.
        try:
            fs = build_factsheet(
                doc,
                layout_pages=layout_pages,
                pdf_path=upload_path if upload_path.suffix.lower() == ".pdf" else None,
            )
            fs_path = outputs / f"{sub_id}_factsheet.json"
            fs_path.write_text(fs.model_dump_json(indent=2))
        except Exception:
            log.exception("factsheet build failed (continuing — agent will see raw doc)")

        fields = {"docling_json_path": str(docling_path)}
        if layout_path:
            fields["layout_json_path"] = layout_path
        _set_status(sub_id, "parsed", **fields)

    except Exception as exc:
        log.exception("parse failed for %s", sub_id)
        _set_status(sub_id, "error", error=str(exc))
