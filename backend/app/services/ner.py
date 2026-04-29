"""GLiNER2 NER — single-call interface for the section-by-section GUI."""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..config import settings

log = logging.getLogger("ner")

_model_lock = threading.Lock()
_model = None


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from gliner2 import GLiNER2  # type: ignore

        log.info("loading GLiNER2 model %s", settings.gliner_model)
        _model = GLiNER2.from_pretrained(settings.gliner_model)
        return _model


# Curated label presets — descriptions matter, they steer the model.
LABEL_PRESETS: dict[str, dict[str, str]] = {
    "header": {
        "article_title": "The full title of the scholarly article",
        "author_name": "Personal name of an author of the article",
        "affiliation": "Institutional affiliation of an author (university, lab, hospital, company)",
        "email": "Email address belonging to an author",
        "orcid": "An ORCID identifier in the form 0000-0000-0000-0000",
        "doi": "A Digital Object Identifier such as 10.1000/xyz123",
        "journal_title": "Name of the journal in which the article appears",
        "issn": "An ISSN serial number such as 1234-5678",
        "volume": "Journal volume number",
        "issue": "Journal issue number",
        "page_range": "Article page range, e.g. 123-145",
        "publication_date": "Publication date of the article",
    },
    "abstract": {
        "keyword": "A keyword or index term describing the article's topic",
    },
    "funding": {
        "funder_name": "Name of a funding agency or grant-providing organization",
        "grant_number": "A grant or award identifier such as NIH R01-XXXXX or NSF-1234567",
    },
    "references": {
        "reference_doi": "A DOI inside a bibliographic citation",
        "reference_title": "Title of a cited work",
        "reference_author": "An author of a cited work",
        "reference_year": "Publication year of a cited work",
        "journal_title": "A journal name appearing inside a citation",
    },
}


def run_ner(text: str, labels: dict[str, str]) -> list[dict[str, Any]]:
    if not text.strip() or not labels:
        return []
    model = _load_model()
    raw = model.extract_entities(text, labels)
    return _normalize(raw)


def _normalize(raw: Any) -> list[dict[str, Any]]:
    """Flatten GLiNER2 output to a list of {label, text, ...} dicts.

    GLiNER2 returns either:
      - {"entities": {label: [text1, text2, ...]}}                (current)
      - {"entities": {label: [{text, start, end, score}, ...]}}  (with spans)
      - [{label, text, ...}]                                       (legacy/list)
    """
    if raw is None:
        return []
    if isinstance(raw, dict) and "entities" in raw:
        raw = raw["entities"]

    out: list[dict[str, Any]] = []

    if isinstance(raw, dict):
        for label, items in raw.items():
            if not isinstance(items, list):
                items = [items]
            for item in items:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        out.append({"label": label, "text": text, "start": None, "end": None, "score": None})
                elif isinstance(item, dict):
                    text = (item.get("text") or item.get("span") or "").strip()
                    if text:
                        out.append({
                            "label": label,
                            "text": text,
                            "start": item.get("start"),
                            "end": item.get("end"),
                            "score": item.get("score") or item.get("confidence"),
                        })
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or item.get("span") or "").strip()
            if text:
                out.append({
                    "label": item.get("label") or item.get("type") or item.get("entity"),
                    "text": text,
                    "start": item.get("start"),
                    "end": item.get("end"),
                    "score": item.get("score") or item.get("confidence"),
                })

    return out
