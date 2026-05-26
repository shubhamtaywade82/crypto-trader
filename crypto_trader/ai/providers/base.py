"""crypto_trader.ai.providers.base — Normalized LLMProvider interface."""

from typing import Optional, Protocol

from crypto_trader.ai.schemas import LLMDecision


class LLMProvider(Protocol):
    def health(self) -> bool:
        """Verify the service endpoint is reachable and healthy."""
        ...

    def chat(self, system_prompt: str, user_prompt: str, timeout_s: int) -> Optional[str]:
        """Send message exchange to the provider, returning raw text or JSON."""
        ...
