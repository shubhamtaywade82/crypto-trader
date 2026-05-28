"""crypto_trader.ai.providers.ollama_cloud — Adapter for cloud qwen3.5 OpenAI-compatible chat endpoint."""

import requests
import logging
from typing import Optional, List

from crypto_trader.ai.providers.base import LLMProvider

logger = logging.getLogger("crypto_trader.ai.providers.ollama_cloud")


class OllamaCloudProvider:
    def __init__(
        self,
        host: str = "https://api.ollama.com",
        model: str = "qwen3.5:cloud",
        api_key: str = "",  # Can be a comma-separated string or a list
    ):
        self.host = host.rstrip("/")
        self.model = model
        
        # Parse multiple keys if provided
        if isinstance(api_key, str):
            self.api_keys = [k.strip() for k in api_key.split(",") if k.strip()]
        else:
            self.api_keys = list(api_key) if api_key else []
            
        self.current_key_idx = 0
        self.session = requests.Session()
        self._update_session_header()

    def _update_session_header(self) -> None:
        """Update Authorization header with the current API key."""
        if self.api_keys:
            key = self.api_keys[self.current_key_idx]
            self.session.headers.update({"Authorization": f"Bearer {key}"})
            # Log only the masked key for security
            masked_key = f"{key[:4]}****{key[-4:]}" if len(key) > 8 else "****"
            logger.info(f"[OllamaCloud] Using API key index {self.current_key_idx} ({masked_key})")

    def _rotate_key(self) -> bool:
        """Rotate to the next available API key. Returns True if a new key was selected."""
        if len(self.api_keys) <= 1:
            return False
            
        self.current_key_idx = (self.current_key_idx + 1) % len(self.api_keys)
        self._update_session_header()
        return True

    def health(self) -> bool:
        # Requires at least one API key
        return len(self.api_keys) > 0

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        if not self.api_keys:
            logger.error("[OllamaCloud] No API keys configured")
            return None

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

        # Try all keys if necessary
        for attempt in range(len(self.api_keys)):
            try:
                url = f"{self.host}/v1/chat/completions"
                resp = self.session.post(url, json=payload, timeout=timeout_s)
                
                # If rate limited (429) or unauthorized (401), rotate and retry
                if resp.status_code in (401, 403, 429):
                    logger.warning(f"[OllamaCloud] Key {self.current_key_idx} failed with {resp.status_code}. Rotating...")
                    if self._rotate_key():
                        continue
                    else:
                        break
                        
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                logger.warning(f"[OllamaCloud] Error with key {self.current_key_idx}: {e}. Rotating...")
                if self._rotate_key():
                    continue
                else:
                    break
                    
        return None
