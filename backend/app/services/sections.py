"""Walk a DoclingDocument JSON and produce an ordered list of sections.

A section starts at a `title` or `section_header` text item and runs until the
next header at the same-or-shallower level. We carry along the page range and
char count so the GUI can show useful summaries without loading the full text.
"""

from __future__ import annotations

from typing import Any

HEADER_LABELS = {"title", "section_header", "page_header"}
TEXT_LABELS = {"text", "paragraph", "list_item", "caption", "footnote", "code", "formula"}


def _resolve(doc: dict, ref: str) -> dict | None:
    # Docling refs look like "#/texts/12" — walk into the JSON.
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


def _page(item: dict) -> int | None:
    prov = item.get("prov") or []
    if prov and isinstance(prov, list):
        first = prov[0]
        if isinstance(first, dict):
            return first.get("page_no") or first.get("page")
    return None


def extract_sections(docling_doc: dict) -> list[dict]:
    doc = docling_doc.get("doc") or {}
    body = (doc.get("body") or {}).get("children") or doc.get("body") or []
    if isinstance(body, dict):
        body = body.get("children") or []

    items: list[dict] = []
    for entry in body:
        ref = entry.get("$ref") if isinstance(entry, dict) else None
        node = _resolve(doc, ref) if ref else (entry if isinstance(entry, dict) else None)
        if node:
            items.append(node)

    # Fallback: if body walk produced nothing, just use all texts in order.
    if not items:
        items = list(doc.get("texts") or [])

    sections: list[dict] = []
    current: dict | None = None

    def _flush() -> None:
        if current is None:
            return
        text = "\n".join(current["_chunks"]).strip()
        sections.append({
            "id": len(sections),
            "level": current["level"],
            "label": current["label"],
            "heading": current["heading"],
            "text": text,
            "char_count": len(text),
            "page_start": current["page_start"],
            "page_end": current["page_end"],
        })

    for item in items:
        label = item.get("label") or ""
        text = (item.get("text") or "").strip()
        page = _page(item)

        if label in HEADER_LABELS:
            _flush()
            current = {
                "level": int(item.get("level") or (1 if label == "title" else 2)),
                "label": label,
                "heading": text or "(untitled)",
                "_chunks": [],
                "page_start": page,
                "page_end": page,
            }
        else:
            if current is None:
                # Pre-header content (typically the title page block).
                current = {
                    "level": 0,
                    "label": "preamble",
                    "heading": "Front matter",
                    "_chunks": [],
                    "page_start": page,
                    "page_end": page,
                }
            if label in TEXT_LABELS or label == "":
                if text:
                    current["_chunks"].append(text)
                if page is not None:
                    current["page_end"] = page

    _flush()
    return sections
