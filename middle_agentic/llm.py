"""Local model server helpers (role: llm). Model management is implemented for Ollama;
other runtimes (llama.cpp, vLLM, LM Studio) manage their models themselves."""
from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from middle_agentic.config import Settings
from middle_agentic.providers.registry import RUNTIMES


class LLMError(RuntimeError):
    pass


def api_base(settings: Settings) -> str:
    spec = RUNTIMES.get(settings.llm.runtime)
    if spec is None or not spec.local:
        raise LLMError(f"llm.runtime '{settings.llm.runtime}' is not a local runtime")
    base = settings.llm.api_base or spec.default_api_base
    if not base:
        raise LLMError(f"no api_base configured for runtime '{settings.llm.runtime}'")
    return base.rstrip("/")


def _require_ollama(settings: Settings) -> str:
    if settings.llm.runtime != "ollama":
        raise LLMError(f"model management is handled by {settings.llm.runtime} itself; only ollama is automated")
    return api_base(settings)


def status(settings: Settings, timeout: float = 2.0) -> dict[str, object]:
    base = api_base(settings)
    spec = RUNTIMES[settings.llm.runtime]
    try:
        response = httpx.get(f"{base}{spec.health_path or ''}", timeout=timeout)
        reachable = response.status_code < 400
    except httpx.HTTPError:
        reachable = False
    return {"runtime": settings.llm.runtime, "api_base": base, "model": settings.llm.model, "reachable": reachable}


def list_models(settings: Settings) -> list[dict[str, object]]:
    base = _require_ollama(settings)
    try:
        response = httpx.get(f"{base}/api/tags", timeout=5)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMError(f"ollama is not reachable at {base}: {exc}") from exc
    return [
        {"name": item.get("name"), "size_gb": round((item.get("size") or 0) / 1e9, 2), "modified": item.get("modified_at")}
        for item in response.json().get("models", [])
    ]


def pull(settings: Settings, model: str, on_progress: Callable[[str], None] | None = None) -> None:
    """Pull a model through the Ollama HTTP API (works even when the ollama CLI is not on PATH)."""
    base = _require_ollama(settings)
    last_status = ""
    try:
        with httpx.stream("POST", f"{base}/api/pull", json={"model": model, "stream": True}, timeout=None) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                if "error" in event:
                    raise LLMError(f"ollama pull failed: {event['error']}")
                status_text = event.get("status", "")
                total, completed = event.get("total"), event.get("completed")
                if total and completed:
                    status_text = f"{status_text} {completed * 100 // total}%"
                if on_progress and status_text != last_status:
                    on_progress(status_text)
                    last_status = status_text
    except httpx.HTTPError as exc:
        raise LLMError(f"ollama is not reachable at {base}: {exc}") from exc
