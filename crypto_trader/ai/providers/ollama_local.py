"""crypto_trader.ai.providers.ollama_local — Adapter for local qwen3.5:4b chat endpoint."""

import requests
from typing import Optional

from crypto_trader.ai.providers.base import LLMProvider


class OllamaLocalProvider:
    def __init__(self, host: str = "http://localhost:11434", model: str = "qwen3.5:4b"):
        self.host = host.rstrip("/")
        self.model = model
        self.session = requests.Session()

    def health(self) -> bool:
        try:
            return self.session.get(f"{self.host}/api/tags", timeout=3).status_code == 200
        except Exception:
            return False

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "think": False,  # disable thinking tokens — 3-5x faster for qwen3.x models
            "options": {"temperature": 0.2, "num_predict": 256},
        }
        try:
            resp = self.session.post(f"{self.host}/api/chat", json=payload, timeout=timeout_s)
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]
        except Exception:
            return None
