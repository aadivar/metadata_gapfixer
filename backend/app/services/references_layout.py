"""Detect the references section from layout cues — not just markdown regex.

Three-tier strategy, falls through if the previous tier fails:

  A. Docling-labeled `reference` elements (best — model-level classification)
  B. Section walk: find a heading `References | Bibliography | Works cited`
     and take subsequent list_item / text elements until the next heading.
  C. Visual clustering: in the back third of pages, find the largest dense
     cluster of similarly-sized text elements with similar left edges.
     This catches papers where Docling didn't label the heading and where
     citations aren't in a numbered/bulleted format.

Output carries per-item bboxes (in PDF point space, with coord_origin) so
the GUI can show the detected section highlighted on the page render and
the editor can confirm or relocate.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger("references_layout")


class ReferenceItem(BaseModel):
    text: str
    page: int = 0
    bbox: dict | None = None     # raw Docling bbox: {l, t, r, b, coord_origin}
    label: str = ""


class ReferencesLayout(BaseModel):
    items: list[ReferenceItem] = Field(default_factory=list)
    method: str = "none"         # "label" | "section_walk" | "visual_cluster" | "none"
    confidence: float = 0.0
    page_start: int | None = None
    page_end: int | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item_text(item: dict) -> str:
    return (item.get("text") or "").strip()


def _bbox_height(bbox: dict | None) -> float:
    if not bbox:
        return 0
    return abs(float(bbox.get("b", 0)) - float(bbox.get("t", 0)))


def _bbox_left(bbox: dict | None) -> float:
    return float(bbox.get("l", 0)) if bbox else 0


def _page_of(item: dict) -> int:
    provs = item.get("prov") or []
    if not provs:
        return 0
    p = provs[0]
    return p.get("page_no") or p.get("page") or 0


def _to_ref_item(t: dict) -> ReferenceItem:
    provs = t.get("prov") or []
    prov = provs[0] if provs else {}
    return ReferenceItem(
        text=_item_text(t),
        page=prov.get("page_no") or prov.get("page") or 0,
        bbox=prov.get("bbox"),
        label=(t.get("label") or "").lower(),
    )


def _page_range(items: list[ReferenceItem]) -> tuple[int | None, int | None]:
    pages = [i.page for i in items if i.page]
    if not pages:
        return None, None
    return min(pages), max(pages)


# ---------------------------------------------------------------------------
# Tier A — Docling-labeled "reference"
# ---------------------------------------------------------------------------

def _tier_a_label(texts: list[dict]) -> list[ReferenceItem]:
    items = [
        _to_ref_item(t) for t in texts
        if (t.get("label") or "").lower() == "reference" and _item_text(t)
    ]
    return items if len(items) >= 5 else []


# ---------------------------------------------------------------------------
# Tier B — Section walk
# ---------------------------------------------------------------------------

REFERENCES_HEADING_RX = re.compile(
    r"^\s*(references|bibliography|works\s+cited|literature\s+cited|"
    r"references\s+and\s+notes|reference\s+list)\s*$",
    re.IGNORECASE,
)


def _tier_b_section_walk(texts: list[dict]) -> list[ReferenceItem]:
    in_refs = False
    out: list[ReferenceItem] = []
    for t in texts:
        label = (t.get("label") or "").lower()
        text = _item_text(t)
        if label in ("section_header", "title"):
            if REFERENCES_HEADING_RX.match(text):
                in_refs = True
                continue
            elif in_refs:
                break  # left the references section at the next heading
        if in_refs and label in ("list_item", "text", "paragraph", "reference") and len(text) > 20:
            out.append(_to_ref_item(t))
    return out if len(out) >= 5 else []


# ---------------------------------------------------------------------------
# Tier C — Visual clustering
# ---------------------------------------------------------------------------

_SKIP_LABELS = {"section_header", "title", "page_header", "page_footer",
                "footnote", "caption", "table", "picture", "formula"}


def _tier_c_visual_cluster(texts: list[dict]) -> list[ReferenceItem]:
    """Find the largest cluster of similarly-shaped text elements in the back
    third of pages. References have a distinctive visual signature: small
    uniform heights, consistent left edges, dense packing."""
    # Page count
    max_page = 0
    for t in texts:
        max_page = max(max_page, _page_of(t))
    if max_page < 3:
        return []  # short docs don't have enough signal

    threshold_page = max(1, int(max_page * 0.65))

    # Gather candidates with their geometric metrics
    candidates: list[tuple[dict, float, float]] = []  # (item, height, left_edge)
    for t in texts:
        label = (t.get("label") or "").lower()
        if label in _SKIP_LABELS:
            continue
        page = _page_of(t)
        if page < threshold_page:
            continue
        provs = t.get("prov") or []
        if not provs:
            continue
        bbox = provs[0].get("bbox") or {}
        h = _bbox_height(bbox)
        x = _bbox_left(bbox)
        text = _item_text(t)
        if h < 6 or h > 120 or len(text) < 30:
            continue
        candidates.append((t, h, x))

    if len(candidates) < 5:
        return []

    # Bucket by (height ~4pt, left_edge ~30pt). The largest bucket of
    # small-height items is most likely the references list.
    buckets: dict[tuple[int, int], list[dict]] = {}
    for item, h, x in candidates:
        key = (round(h / 4), round(x / 30))
        buckets.setdefault(key, []).append(item)

    if not buckets:
        return []

    largest = max(buckets.values(), key=len)
    if len(largest) < 5:
        return []

    return [_to_ref_item(t) for t in largest]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect_references(docling_doc: dict) -> ReferencesLayout:
    doc = docling_doc.get("doc") or {}
    texts = doc.get("texts") or []

    # Tier A
    items = _tier_a_label(texts)
    if items:
        ps, pe = _page_range(items)
        log.info("references via Tier A (label): %d items pp.%s-%s", len(items), ps, pe)
        return ReferencesLayout(
            items=items, method="label", confidence=0.98,
            page_start=ps, page_end=pe,
            notes=f"Docling labeled {len(items)} elements as 'reference'.",
        )

    # Tier B
    items = _tier_b_section_walk(texts)
    if items:
        ps, pe = _page_range(items)
        log.info("references via Tier B (section walk): %d items pp.%s-%s", len(items), ps, pe)
        return ReferencesLayout(
            items=items, method="section_walk", confidence=0.85,
            page_start=ps, page_end=pe,
            notes=f"Found a 'References' heading followed by {len(items)} items.",
        )

    # Tier C
    items = _tier_c_visual_cluster(texts)
    if items:
        ps, pe = _page_range(items)
        log.info("references via Tier C (visual cluster): %d items pp.%s-%s", len(items), ps, pe)
        return ReferencesLayout(
            items=items, method="visual_cluster", confidence=0.7,
            page_start=ps, page_end=pe,
            notes=f"Visual clustering detected {len(items)} similarly-shaped elements in the back third (pp.{ps}-{pe}).",
        )

    return ReferencesLayout(
        items=[], method="none", confidence=0.0,
        notes="No references detected by any tier.",
    )


def references_text_list(layout: ReferencesLayout) -> list[str]:
    """Flatten to citation strings for downstream enrichment."""
    return [item.text for item in layout.items if item.text]
