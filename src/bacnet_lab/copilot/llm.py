"""Thin async client for the Open-WebUI / Ollama OpenAI-compatible chat API."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, timeout_s: float = 60.0) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout_s

    @property
    def model(self) -> str:
        return self._model

    async def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        response_json: bool = False,
    ) -> str:
        """Single-turn chat completion. Returns the assistant text (or '' on error)."""
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": False,
        }
        if response_json:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{self._base}/api/chat/completions", json=payload, headers=headers
                )
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error("LLM chat failed: %s", e)
            return ""

    async def health(self) -> bool:
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{self._base}/api/models", headers=headers)
                return r.status_code == 200
        except Exception:
            return False
