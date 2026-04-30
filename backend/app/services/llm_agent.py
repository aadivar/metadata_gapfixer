"""LLM agent (OpenAI-compatible) — reconciles Docling + GLiNER2 output into
a Crossref-ready JournalArticleMetadata via tool-calling against the enricher
APIs (ORCID, ROR, OpenAlex, Crossref).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from openai import OpenAI

from ..config import settings
from ..models import JournalArticleMetadata
from .enrichers import CrossrefClient, OpenAlexClient, ORCIDClient, RORClient
from .llm_router import _append_ledger, _estimate_cost

log = logging.getLogger("agent")

MAX_ITERATIONS = 12

SYSTEM_PROMPT = """You are a scholarly metadata reconciliation agent. You receive:
  1. The full text and structure of a journal article (extracted by Docling).
  2. Candidate entities extracted by a NER model (GLiNER2) per zone.

Your job: produce a complete, accurate Crossref `journal_article` metadata record.

Use the provided tools (ORCID, ROR, OpenAlex, Crossref REST) to:
  - Disambiguate authors (resolve to ORCID iDs where possible).
  - Resolve affiliations to ROR IDs.
  - Verify the journal (ISSN, abbreviated title) via Crossref.
  - Resolve cited references to DOIs via Crossref.
  - Normalize funder names to the Crossref Funder Registry.

Rules:
  - Never fabricate ORCIDs, DOIs, ROR IDs, ISSNs, or grant numbers.
  - If a field is unknown after enrichment, leave it null and add a note in `confidence_notes`.
  - Author names: prefer the form used in the article. Split into given_name + surname.
  - publication_date: ISO `YYYY-MM-DD` (or `YYYY-MM` / `YYYY` if day/month unknown).

When done, return ONLY a single JSON object that conforms to the JournalArticleMetadata
schema, with no surrounding prose.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "orcid_search",
            "description": "Search ORCID for a person by name and optional affiliation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "given_name": {"type": "string"},
                    "family_name": {"type": "string"},
                    "affiliation": {"type": "string"},
                },
                "required": ["family_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ror_search",
            "description": "Resolve an affiliation string to ROR organisation candidates.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openalex_author_lookup",
            "description": "Look up an author in OpenAlex by name (+ optional affiliation).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "affiliation": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openalex_work_lookup",
            "description": "Look up a work in OpenAlex by title or DOI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "doi": {"type": "string"},
                    "author": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openalex_funder_lookup",
            "description": "Look up a funder in OpenAlex (returns Crossref Funder Registry DOI).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crossref_doi_lookup",
            "description": "Fetch the Crossref record for a known DOI.",
            "parameters": {
                "type": "object",
                "properties": {"doi": {"type": "string"}},
                "required": ["doi"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crossref_work_search",
            "description": "Search Crossref for a work by title (+ optional author / journal).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "author": {"type": "string"},
                    "container_title": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crossref_journal_search",
            "description": "Look up a journal in Crossref by title or ISSN.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "issn": {"type": "string"},
                },
            },
        },
    },
]


class _ToolBox:
    def __init__(self) -> None:
        self.orcid = ORCIDClient()
        self.ror = RORClient()
        self.openalex = OpenAlexClient()
        self.crossref = CrossrefClient()

    def call(self, name: str, args: dict) -> Any:
        try:
            if name == "orcid_search":
                return self.orcid.search(args.get("given_name"), args.get("family_name"), args.get("affiliation"))
            if name == "ror_search":
                return self.ror.search(args["name"])
            if name == "openalex_author_lookup":
                return self.openalex.search_author(args["name"], args.get("affiliation"))
            if name == "openalex_work_lookup":
                return self.openalex.search_work(args.get("title"), args.get("doi"), args.get("author"))
            if name == "openalex_funder_lookup":
                return self.openalex.search_funder(args["name"])
            if name == "crossref_doi_lookup":
                return self.crossref.by_doi(args["doi"])
            if name == "crossref_work_search":
                return self.crossref.search_work(args["query"], args.get("author"), args.get("container_title"))
            if name == "crossref_journal_search":
                return self.crossref.search_journal(args.get("title"), args.get("issn"))
        except Exception as exc:
            return {"error": str(exc)}
        return {"error": f"unknown tool {name}"}


def _build_user_message(docling_doc: dict, entities: dict) -> str:
    markdown = docling_doc.get("markdown") or ""
    head = markdown[:6000]
    refs = entities.get("zones", {}).get("references", "")[:6000]
    payload = {
        "header_excerpt": head,
        "references_excerpt": refs,
        "ner_header": entities.get("header"),
        "ner_abstract": entities.get("abstract"),
        "ner_acknowledgements": entities.get("acknowledgements"),
        "ner_references": entities.get("references"),
    }
    return (
        "Here is the extracted article. Reconcile and enrich it, then return the "
        "final JournalArticleMetadata JSON.\n\n```json\n"
        + json.dumps(payload, indent=2)
        + "\n```"
    )


def reconcile_metadata(docling_doc: dict, entities: dict, sub_id: int | None = None) -> JournalArticleMetadata:
    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    tools = _ToolBox()

    ledger_path = (settings.data_dir / "outputs" / f"{sub_id}_cost.json") if sub_id is not None else None
    model = settings.openai_model

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(docling_doc, entities)},
    ]

    last_content: str | None = None
    for step in range(MAX_ITERATIONS):
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.1,
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        # Cost tracking — records every iteration of the agent loop.
        if ledger_path is not None and resp.usage is not None:
            in_tok  = resp.usage.prompt_tokens
            out_tok = resp.usage.completion_tokens
            usd = _estimate_cost(model, in_tok, out_tok)
            _append_ledger(ledger_path, {
                "ts": datetime.utcnow().isoformat(timespec="seconds"),
                "task": f"reconcile_iter_{step}",
                "model": model,
                "in_tokens": in_tok,
                "out_tokens": out_tok,
                "usd": round(usd, 6),
                "clipped": False,
            })

        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = tools.call(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, default=str)[:8000],
                })
            continue

        last_content = msg.content or ""
        break
    else:
        log.warning("agent hit MAX_ITERATIONS without final answer")
        last_content = ""

    return _parse_metadata(last_content or "")


def _parse_metadata(text: str) -> JournalArticleMetadata:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return JournalArticleMetadata.model_validate_json(text)
    except Exception:
        # Try to find the first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return JournalArticleMetadata.model_validate_json(text[start : end + 1])
            except Exception as exc:
                log.warning("metadata JSON parse failed: %s", exc)
    return JournalArticleMetadata(confidence_notes={"agent": "no valid JSON returned"})
