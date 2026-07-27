from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

from pydantic import ValidationError

from .catalog import public_catalog
from .models import StrategyDocument, StrategyProposal


class AssistantUnavailableError(RuntimeError):
    pass


class AssistantResponseError(ValueError):
    pass


class OllamaAssistant:
    def __init__(self) -> None:
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        self.model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
        self.timeout = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))

    async def propose(
        self,
        *,
        prompt: str,
        current_strategy: StrategyDocument,
        requested_name: str | None,
    ) -> StrategyProposal:
        return await asyncio.to_thread(
            self._propose_sync,
            prompt,
            current_strategy,
            requested_name,
        )

    def _propose_sync(
        self,
        prompt: str,
        current_strategy: StrategyDocument,
        requested_name: str | None,
    ) -> StrategyProposal:
        schema = StrategyProposal.model_json_schema()
        system_message = (
            "You edit StarCraft II bot strategies. Return only a StrategyProposal matching "
            "the supplied JSON schema. Never generate Python or unsupported action types. "
            "Preserve existing behavior unless the user asks to change it. Use only values "
            "from the supported catalog. Explain uncertain interpretations in assumptions "
            "and potentially weak or impossible behavior in warnings."
        )
        user_message = json.dumps(
            {
                "request": prompt,
                "requestedName": requested_name,
                "supportedCatalog": public_catalog(),
                "currentStrategy": current_strategy.model_dump(mode="json"),
                "responseSchema": schema,
            }
        )
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": schema,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise AssistantUnavailableError(
                f"Could not reach Ollama at {self.base_url}. Start Ollama and pull {self.model}."
            ) from exc
        if "error" in body:
            raise AssistantUnavailableError(body["error"])
        content = body.get("message", {}).get("content")
        if not content:
            raise AssistantResponseError("Ollama returned an empty proposal.")
        try:
            return StrategyProposal.model_validate_json(content)
        except ValidationError as exc:
            raise AssistantResponseError(
                f"Ollama returned a proposal that failed validation: {exc}"
            ) from exc

    async def health(self) -> dict[str, object]:
        def request_tags() -> dict[str, object]:
            request = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.loads(response.read())

        try:
            result = await asyncio.to_thread(request_tags)
        except Exception:
            return {
                "available": False,
                "model": self.model,
                "modelInstalled": False,
                "baseUrl": self.base_url,
            }
        installed = {
            model.get("name") or model.get("model") for model in result.get("models", [])
        }
        return {
            "available": True,
            "model": self.model,
            "modelInstalled": self.model in installed,
            "baseUrl": self.base_url,
        }
