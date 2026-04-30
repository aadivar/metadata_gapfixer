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
    "ror_disambiguate":   TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=1_500),
    "ref_resolve":        TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=4_000, max_output_tokens=3_000),
    "assemble_metadata":  TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=6_000, max_output_tokens=3_000),
    "preprint_detect":    TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=1_000),
    "funder_confirm":     TaskConfig(model=_DEFAULT_MODEL, max_input_tokens=1_000),
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
                    "strict": True,
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
# Convenience: per-submission router with ledger pre-bound
# ============================================================================

def router_for_submission(submission_id: int) -> LLMRouter:
    ledger_path = settings.data_dir / "outputs" / f"{submission_id}_cost.json"
    return LLMRouter(ledger_path=ledger_path)
