"""Generate (and well-formedness-check) a Crossref 5.3.1 deposit XML for a
single journal article."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from lxml import etree

from ..config import settings
from ..models import JournalArticleMetadata

log = logging.getLogger("crossref_xml")

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(disabled_extensions=("j2",), default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _split_date(iso: str | None) -> tuple[str | None, str | None, str | None]:
    if not iso:
        return None, None, None
    parts = iso.split("-")
    year = parts[0] if len(parts) >= 1 and parts[0] else None
    month = parts[1] if len(parts) >= 2 and parts[1] else None
    day = parts[2] if len(parts) >= 3 and parts[2] else None
    return year, month, day


def build_crossref_xml(meta: JournalArticleMetadata, out_path: Path) -> None:
    pub_year, pub_month, pub_day = _split_date(meta.publication_date)
    template = _env.get_template("journal_article.xml.j2")
    xml = template.render(
        m=meta,
        batch_id=f"mgf-{uuid.uuid4()}",
        timestamp=datetime.utcnow().strftime("%Y%m%d%H%M%S"),
        depositor_name="metadata-gapfixer",
        depositor_email=settings.contact_email,
        registrant="metadata-gapfixer",
        pub_year=pub_year,
        pub_month=pub_month,
        pub_day=pub_day,
    )
    parser = etree.XMLParser(remove_blank_text=False)
    try:
        root = etree.fromstring(xml.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError as exc:
        log.error("generated XML is not well-formed: %s", exc)
        out_path.write_text(xml)
        raise

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True))
