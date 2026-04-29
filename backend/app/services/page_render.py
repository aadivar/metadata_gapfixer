"""Render each PDF page to a PNG and extract per-element bounding boxes from
the Docling document so the GUI can show a click-to-select layout overlay.

Coordinate handling:
  - Docling bboxes are in PDF points with `coord_origin` either BOTTOMLEFT (PDF
    convention) or TOPLEFT (image convention). We normalize everything to
    TOPLEFT pixels relative to the rendered PNG.
  - PNG is rendered at PAGE_DPI; px = pt * DPI / 72.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

log = logging.getLogger("page_render")

PAGE_DPI = 150
PT_TO_PX = PAGE_DPI / 72.0

# Categorical color groups for the GUI overlay.
CATEGORY: dict[str, str] = {
    "title": "header",
    "section_header": "header",
    "page_header": "furniture",
    "page_footer": "furniture",
    "footnote": "furniture",
    "text": "text",
    "paragraph": "text",
    "list_item": "text",
    "caption": "text",
    "table": "table",
    "picture": "figure",
    "code": "code",
    "formula": "formula",
}


def _resolve(doc: dict, ref: str) -> dict | None:
    if not ref or not ref.startswith("#/"):
        return None
    node: Any = doc
    for part in ref[2:].split("/"):
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
            if node is None:
                return None
        else:
            return None
    return node if isinstance(node, dict) else None


def _bbox_to_px(bbox: dict, page_height_pt: float) -> dict:
    """Normalize a Docling bbox dict to TOPLEFT pixel coords."""
    l = float(bbox.get("l", 0))
    t = float(bbox.get("t", 0))
    r = float(bbox.get("r", 0))
    b = float(bbox.get("b", 0))
    origin = (bbox.get("coord_origin") or "TOPLEFT").upper()
    if origin == "BOTTOMLEFT":
        # Flip Y: PDF y grows upward from page bottom.
        t_new = page_height_pt - max(t, b)
        b_new = page_height_pt - min(t, b)
        t, b = t_new, b_new
    return {
        "x": round(l * PT_TO_PX, 1),
        "y": round(t * PT_TO_PX, 1),
        "w": round((r - l) * PT_TO_PX, 1),
        "h": round((b - t) * PT_TO_PX, 1),
    }


def _iter_layout_items(doc: dict) -> list[dict]:
    """Walk texts + tables + pictures, return items with text+label+prov."""
    items: list[dict] = []
    for it in doc.get("texts") or []:
        items.append({
            "label": it.get("label") or "text",
            "text": (it.get("text") or "").strip(),
            "prov": it.get("prov") or [],
        })
    for it in doc.get("tables") or []:
        items.append({
            "label": "table",
            "text": _table_text(it),
            "prov": it.get("prov") or [],
        })
    for it in doc.get("pictures") or []:
        items.append({
            "label": "picture",
            "text": (it.get("caption_text") or it.get("text") or "").strip(),
            "prov": it.get("prov") or [],
        })
    return items


def _table_text(table: dict) -> str:
    data = table.get("data") or {}
    grid = data.get("grid") or []
    rows: list[str] = []
    for row in grid:
        cells = [(c.get("text") or "").strip() for c in (row or [])]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def render_pages_and_layout(pdf_path: Path, docling_doc: dict, out_dir: Path) -> dict:
    """Render each page as PNG and emit a layout dict.

    Returns:
        {
          "pages": [{page, w_px, h_px, image_path, boxes: [...]}],
          "page_count": N,
          "dpi": 150,
        }
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = docling_doc.get("doc") or {}
    items = _iter_layout_items(doc)

    pdf = fitz.open(str(pdf_path))
    pages_out: list[dict] = []
    box_id = 0

    for page_idx in range(len(pdf)):
        page = pdf[page_idx]
        page_no = page_idx + 1
        page_height_pt = page.rect.height

        pix = page.get_pixmap(dpi=PAGE_DPI, alpha=False)
        img_path = out_dir / f"page_{page_no:03d}.png"
        pix.save(str(img_path))

        boxes = []
        for it in items:
            for prov in it["prov"]:
                if (prov.get("page_no") or prov.get("page")) != page_no:
                    continue
                bb = prov.get("bbox") or {}
                if not bb:
                    continue
                px_box = _bbox_to_px(bb, page_height_pt)
                boxes.append({
                    "id": box_id,
                    "label": it["label"],
                    "category": CATEGORY.get(it["label"], "other"),
                    "text": it["text"],
                    "bbox": px_box,
                })
                box_id += 1

        pages_out.append({
            "page": page_no,
            "w_px": pix.width,
            "h_px": pix.height,
            "image_path": str(img_path),
            "boxes": boxes,
        })

    pdf.close()
    log.info("rendered %d pages, %d boxes for %s", len(pages_out), box_id, pdf_path.name)
    return {"pages": pages_out, "page_count": len(pages_out), "dpi": PAGE_DPI}
