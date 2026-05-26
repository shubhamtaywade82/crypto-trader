"""crypto_trader.ai.providers.ollama_cloud — Adapter for cloud qwen3.5 OpenAI-compatible chat endpoint."""

import requests
from typing import Optional

from crypto_trader.ai.providers.base import LLMProvider


class OllamaCloudProvider:
    def __init__(
        self,
        host: str = "https://api.ollama.com",
        model: str = "qwen3.5:cloud",
        api_key: str = "",
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({"Authorization": f"Bearer {api_key}"})

    def health(self) -> bool:
        # Requires API key existence
        return bool(self.api_key)

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "stream": False,
        }
        try:
            # OpenAI compatible completions path
            url = f"{self.host}/v1/chat/completions"
            resp = self.session.post(url, json=payload, timeout=timeout_s)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception:
            return None
