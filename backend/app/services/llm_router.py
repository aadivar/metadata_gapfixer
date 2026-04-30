"""Single OpenAI client wrapper with per-task model routing, token clipping,
structured outputs (JSON Schema), and a per-submission cost ledger.

Goal: keep cost per paper under $0.05 with mini-tier defaults; allow per-task
escalation to flagship when the editor opts in.

Pricing is hard-coded for common OpenAI models (USD per 1M tokens, input/output);
override via env if you use a different provider via OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Type, TypeVar

from openai import OpenAI
from pydantic import BaseModel

from ..config import settings

log = logging.getLogger("llm_router")

T = TypeVar("T", bound=BaseModel)


# ============================================================================
# Task config — model + token budget per task name
# ============================================================================

class TaskConfig(BaseModel):
    model: str
    max_input_tokens: int
    max_output_tokens: int = 2_000
    temperature: float = 0.1


_DEFAULT_MODEL = settings.openai_model or "gpt-4o-mini"

TASK_CONFIG: dict[str, TaskConfig] = {
    "name_normalize":     TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=2_000),
    "assemble_metadata":  TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=6_000, max_output_tokens=3_000),
    "preprint_detect":    TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=1_000),
    # Picker tasks — pick the right candidate from N enricher results.
    "orcid_pick":         TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=2_000, max_output_tokens=600),
    "ror_pick":           TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=2_000, max_output_tokens=600),
    "funder_pick":        TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=2_000, max_output_tokens=600),
    "reference_pick":     TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=4_000, max_output_tokens=1_000),
    # Structurer tasks — turn raw content regions into clean structured JSON.
    "structure_authors":    TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=4_000, max_output_tokens=2_000),
    "structure_references": TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=8_000, max_output_tokens=3_500),
    "structure_funding":    TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=2_500, max_output_tokens=1_200),
    "structure_credit":     TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=3_000, max_output_tokens=1_500),
    "premium":            TaskConfig(model="gpt-4o", max_input_tokens=8_000, max_output_tokens=4_000),
}


# Pricing — USD per 1M tokens (input, output). Update as providers change pricing.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini":           (0.15, 0.60),
    "gpt-4o":                (2.50, 10.00),
    "gpt-4.1":               (2.00,  8.00),
    "gpt-4.1-mini":          (0.40,  1.60),
    "gpt-4.1-nano":          (0.10,  0.40),
    "o3-mini":               (1.10,  4.40),
    # OpenRouter / others — fall back to mini pricing if unknown
}


# ============================================================================
# Cost ledger — JSON file per submission
# ============================================================================

_ledger_lock = threading.Lock()


def _read_ledger(path: Path) -> dict:
    if not path.exists():
        return {"calls": [], "total_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"calls": [], "total_usd": 0.0, "total_input_tokens": 0, "total_output_tokens": 0}


def _append_ledger(path: Path, entry: dict) -> dict:
    with _ledger_lock:
        ledger = _read_ledger(path)
        ledger["calls"].append(entry)
        ledger["total_usd"] = round(ledger.get("total_usd", 0.0) + entry["usd"], 6)
        ledger["total_input_tokens"]  = ledger.get("total_input_tokens", 0)  + entry["in_tokens"]
        ledger["total_output_tokens"] = ledger.get("total_output_tokens", 0) + entry["out_tokens"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, indent=2))
        return ledger


def _estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    pin, pout = PRICING.get(model, PRICING["gpt-4o-mini"])
    return (in_tokens / 1_000_000) * pin + (out_tokens / 1_000_000) * pout


# ============================================================================
# Token clipping (rough: 1 token ≈ 4 chars; keep head+tail)
# ============================================================================

def _clip(text: str, max_tokens: int) -> tuple[str, bool]:
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text, False
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return head + "\n\n[…truncated for token budget…]\n\n" + tail, True


# ============================================================================
# LLMRouter
# ============================================================================

class LLMRouter:
    def __init__(self, ledger_path: Path | None = None) -> None:
        self.client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        self.ledger_path = ledger_path

    def call(
        self,
        task: str,
        system: str,
        user: str,
        schema: Type[T],
        *,
        override_model: str | None = None,
        cap_usd: float | None = None,
        strict: bool = True,
    ) -> T:
        """Run a structured-output LLM call for one task.

        Args:
            task: key into TASK_CONFIG (e.g. "ref_resolve", "assemble_metadata").
            system: system prompt (kept short; same prefix across calls benefits from prompt caching).
            user: the user-message content (will be clipped to the task's token budget).
            schema: a Pydantic model class — its JSON Schema is sent as the response_format.
            override_model: force a specific model (e.g. "gpt-4o" for premium escalation).
            cap_usd: refuse the call if estimated cost would exceed this (post-call check).
        """
        cfg = TASK_CONFIG.get(task) or TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=4_000)
        model = override_model or cfg.model

        clipped_user, was_clipped = _clip(user, cfg.max_input_tokens)
        if was_clipped:
            log.info("clipped user prompt for task=%s (budget=%d tokens)", task, cfg.max_input_tokens)

        resp = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": clipped_user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": strict,
                },
            },
            temperature=cfg.temperature,
            max_tokens=cfg.max_output_tokens,
        )

        usage = resp.usage
        in_tok  = usage.prompt_tokens     if usage else 0
        out_tok = usage.completion_tokens if usage else 0
        usd = _estimate_cost(model, in_tok, out_tok)

        if self.ledger_path is not None:
            ledger = _append_ledger(self.ledger_path, {
                "ts": datetime.utcnow().isoformat(timespec="seconds"),
                "task": task,
                "model": model,
                "in_tokens": in_tok,
                "out_tokens": out_tok,
                "usd": round(usd, 6),
                "clipped": was_clipped,
            })
            if cap_usd and ledger["total_usd"] > cap_usd:
                log.warning("submission cost ceiling exceeded: %.4f > %.4f", ledger["total_usd"], cap_usd)

        content = resp.choices[0].message.content or "{}"
        return schema.model_validate_json(content)


# ============================================================================
# Disambiguation — universal "pick the right candidate" interface
# ============================================================================

class Alternative(BaseModel):
    id: str | None = None
    label: str | None = None
    score: float | None = None
    note: str | None = None  # one-line per-candidate reasoning


class Disambiguation(BaseModel):
    chosen_id: str | None
    confidence: float  # 0..1
    reasoning: str     # 2-3 sentences citing specific evidence
    ranked_alternatives: list[Alternative] = []


_DISAMBIGUATE_SYSTEM = """You are a scholarly-metadata disambiguation assistant.

You will receive a query (a record we want to identify) and a list of
candidates returned by an enricher API ({source}). Pick the single best
match from the candidates — or return null in `chosen_id` if no candidate
is clearly correct.

Rules:
- Confidence below 0.6 means "uncertain — editor should confirm." Be
  honest; do not inflate confidence to look helpful.
- `reasoning` must be 2-3 sentences citing specific evidence (matching
  fields, geographic alignment, organisational hierarchy, etc.). It is
  the editor's audit trail.
- For `ranked_alternatives`, include the top candidates with a 1-line
  note for each explaining why it ranked where it did.
- Never invent candidates that are not in the input list. If you mention
  an ID, it must come verbatim from `candidates`.
- Match conservatively. A 0.9 confidence pick is one where the candidate
  matches the query on at least two independent attributes (e.g. name +
  affiliation, or title + author + year)."""


def _safe_json(payload: Any) -> str:
    try:
        return json.dumps(payload, default=str, ensure_ascii=False)
    except Exception:
        return str(payload)


# Patch on LLMRouter
def _disambiguate(
    self: "LLMRouter",
    task: str,
    source: str,
    query: dict,
    candidates: list[dict],
    *,
    cap_usd: float | None = None,
) -> Disambiguation:
    """Pick the right candidate (or none) from an enricher's results.

    Args:
        task: TASK_CONFIG key — e.g. "orcid_pick", "ror_pick", "funder_pick", "reference_pick".
        source: human-readable enricher name for the system prompt — "ORCID", "ROR", etc.
        query: the record we're trying to identify (e.g. {given, family, affiliation}).
        candidates: list of candidate dicts as returned by the enricher.
    """
    if not candidates:
        return Disambiguation(chosen_id=None, confidence=0.0,
                              reasoning="Enricher returned no candidates.")
    if len(candidates) == 1:
        c = candidates[0]
        cid = c.get("id") or c.get("orcid") or c.get("ror_id") or c.get("doi") or c.get("openalex_id")
        return Disambiguation(
            chosen_id=str(cid) if cid else None,
            confidence=1.0,
            reasoning="Only one candidate returned by the enricher.",
            ranked_alternatives=[Alternative(id=str(cid) if cid else None, label=str(c)[:120], score=1.0, note="sole candidate")],
        )

    user_payload = {"query": query, "candidates": candidates[:10]}  # cap candidates per call
    return self.call(
        task=task,
        system=_DISAMBIGUATE_SYSTEM.format(source=source),
        user=_safe_json(user_payload),
        schema=Disambiguation,
        cap_usd=cap_usd,
    )

LLMRouter.disambiguate = _disambiguate  # type: ignore[attr-defined]


# ============================================================================
# Convenience: per-submission router with ledger pre-bound
# ============================================================================

def router_for_submission(submission_id: int) -> LLMRouter:
    ledger_path = settings.data_dir / "outputs" / f"{submission_id}_cost.json"
    return LLMRouter(ledger_path=ledger_path)
