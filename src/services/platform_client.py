import logging
import asyncio
from uuid import uuid4
from pydantic import SecretStr
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import httpx

logger = logging.getLogger(__name__)


class PlatformClient:
    """LLM proxy through llm-agent-platform.

    Registers as an agent on startup, gets a Bearer token,
    routes all LLM calls through the platform for unified
    monitoring, circuit breaker, and guardrails.
    """

    def __init__(self, platform_url: str, master_token: str, agent_name: str = "eventai-agent"):
        self.platform_url = platform_url.rstrip("/")
        self.master_token = master_token
        self.agent_name = agent_name
        self._token: SecretStr | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10))
        self._register_attempts = 0
        self._last_register_time = 0.0
        self.current_session_id: str | None = None

    @property
    def token(self) -> str:
        if self._token is None:
            raise RuntimeError("PlatformClient not registered. Call register() first.")
        return self._token.get_secret_value()

    async def register(self) -> str:
        """Register agent with platform, get Bearer token."""
        import time
        now = time.monotonic()
        if now - self._last_register_time < 300 and self._register_attempts >= 3:
            raise RuntimeError("Too many registration attempts (3/5min)")

        self._register_attempts += 1
        self._last_register_time = now

        resp = await self._client.post(
            f"{self.platform_url}/agents",
            json={
                "name": self.agent_name,
                "description": "EventAI Demo Day curator agent",
                "methods": ["chat_completion", "embedding"],
                "endpoint_url": "http://bot:8080",
            },
            headers={"Authorization": f"Bearer {self.master_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = SecretStr(data["token"])
        self._register_attempts = 0
        logger.info("Registered with platform as %s (id=%s)", self.agent_name, data["id"])
        return data["id"]

    async def _request(self, method: str, path: str, session_id: str | None = None, **kwargs) -> httpx.Response:
        """Make authenticated request. Auto-reregister on 401."""
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        sid = session_id or self.current_session_id
        if sid:
            headers["X-Session-Id"] = sid

        resp = await self._client.request(method, f"{self.platform_url}{path}", headers=headers, **kwargs)

        if resp.status_code == 401:
            logger.warning("Platform returned 401, re-registering...")
            await self.register()
            headers["Authorization"] = f"Bearer {self.token}"
            resp = await self._client.request(method, f"{self.platform_url}{path}", headers=headers, **kwargs)

        resp.raise_for_status()
        return resp

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
    )
    async def chat_completion(
        self,
        messages: list[dict],
        model: str | None = None,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        session_id: str | None = None,
    ) -> dict:
        """POST /v1/chat/completions through platform."""
        from src.core.config import settings

        payload = {
            "model": model or settings.llm_model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format

        resp = await self._request("POST", "/v1/chat/completions", session_id=session_id, json=payload)
        return resp.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
    )
    async def embedding(self, text: str, model: str | None = None, session_id: str | None = None) -> list[float]:
        """POST /v1/embeddings through platform."""
        from src.core.config import settings

        payload = {
            "model": model or settings.embedding_model,
            "input": text,
        }
        resp = await self._request("POST", "/v1/embeddings", session_id=session_id, json=payload)
        data = resp.json()
        return data["data"][0]["embedding"]

    async def close(self):
        await self._client.aclose()

    def __repr__(self) -> str:
        return f"PlatformClient(url={self.platform_url}, registered={self._token is not None})"
