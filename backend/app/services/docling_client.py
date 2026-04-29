import logging
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("docling")

# docling-serve exposes /v1/convert/file (multipart) and /v1/convert/source (URL/json).
# Response shape: { "document": { "json_content": {...}, "md_content": "...", "text_content": "..." }, "status": "...", ... }
CONVERT_FILE_PATH = "/v1/convert/file"

DEFAULT_OPTIONS: dict[str, Any] = {
    # httpx-multipart-friendly: list values get sent as repeated form fields.
    "to_formats": ["json", "md", "text"],
    "image_export_mode": "placeholder",
    "do_ocr": "true",
    "table_mode": "accurate",
    "abort_on_error": "false",
    "return_as_file": "false",
}


class DoclingError(RuntimeError):
    pass


class DoclingClient:
    def __init__(self, base_url: str | None = None, timeout: float = 600.0) -> None:
        self.base_url = (base_url or settings.docling_base_url).rstrip("/")
        self.timeout = timeout

    def convert(self, path: Path, options: dict[str, Any] | None = None) -> dict:
        url = f"{self.base_url}{CONVERT_FILE_PATH}"
        opts: dict[str, Any] = {**DEFAULT_OPTIONS, **(options or {})}

        # httpx requires `data` to be a Mapping. Multi-valued fields go in as
        # list values and httpx will emit them as repeated form fields.
        data: dict[str, Any] = {}
        for key, value in opts.items():
            if isinstance(value, bool):
                data[key] = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                data[key] = [str(v) for v in value]
            else:
                data[key] = str(value)

        with path.open("rb") as fh:
            files = {"files": (path.name, fh, _guess_mime(path))}
            log.info("docling convert %s -> %s", path.name, url)
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, data=data, files=files)
            except httpx.HTTPError as exc:
                raise DoclingError(f"docling request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise DoclingError(f"docling returned {resp.status_code}: {resp.text[:500]}")

        payload = resp.json()
        if payload.get("status") not in (None, "success", "partial_success"):
            raise DoclingError(f"docling status={payload.get('status')} errors={payload.get('errors')}")

        document = payload.get("document") or {}
        return {
            "filename": document.get("filename") or path.name,
            "doc": document.get("json_content") or {},
            "markdown": document.get("md_content") or "",
            "text": document.get("text_content") or "",
            "timings": payload.get("timings"),
            "processing_time": payload.get("processing_time"),
        }


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
    }.get(suffix, "application/octet-stream")
