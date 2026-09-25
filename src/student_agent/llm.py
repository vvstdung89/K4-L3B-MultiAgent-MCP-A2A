"""Optional LLM orchestrator client (OpenAI-compatible chat completions, e.g. OpenRouter).

Configured through ``ORCHESTRATOR_*`` environment variables. When they are missing the
workflow runs fully deterministic. The client only ever sees structured fact sheets built
from MCP evidence, never the free-text complaint, and must answer with a JSON object.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx2

LLM_TIMEOUT_SECONDS = 90.0
LLM_RETRIES = 1
MAX_TOKENS = 4000
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    fast_model: str
    thinking: bool

    @classmethod
    def from_env(cls) -> LLMSettings | None:
        base_url = os.getenv("ORCHESTRATOR_BASE_URL", "").strip().rstrip("/")
        api_key = os.getenv("ORCHESTRATOR_API_KEY", "").strip()
        model = os.getenv("ORCHESTRATOR_MODEL", "").strip()
        fast_model = os.getenv("ORCHESTRATOR_FAST_MODEL", "").strip() or model
        thinking = os.getenv("ORCHESTRATOR_THINKING", "").strip().lower() in {"1", "true", "yes"}
        if not (base_url.startswith(("http://", "https://")) and api_key and model):
            return None
        return cls(base_url, api_key, model, fast_model, thinking)


class LLMError(RuntimeError):
    pass


class OrchestratorLLM:
    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self._client = httpx2.AsyncClient(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx2.Timeout(LLM_TIMEOUT_SECONDS, connect=20.0),
        )
        self.calls = 0
        self.tokens = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat_json(self, *, fast: bool, system: str, user: str) -> dict[str, Any]:
        model = self.settings.fast_model if fast else self.settings.model
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "reasoning": {"enabled": bool(self.settings.thinking and not fast)},
        }
        last_error: Exception | None = None
        for _ in range(LLM_RETRIES + 1):
            try:
                self.calls += 1
                response = await self._client.post("/chat/completions", json=body)
                if response.status_code != 200:
                    raise LLMError(f"HTTP {response.status_code}")
                payload = response.json()
                self.tokens += int(payload.get("usage", {}).get("total_tokens") or 0)
                content = payload["choices"][0]["message"].get("content") or ""
                return _parse_json(content)
            except (LLMError, KeyError, ValueError, httpx2.HTTPError) as exc:
                last_error = exc
                await asyncio.sleep(1.0)
        raise LLMError(f"{model}: {type(last_error).__name__}: {last_error}")


def _parse_json(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(content)
        if not match:
            raise ValueError("no JSON object in model response") from None
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value
