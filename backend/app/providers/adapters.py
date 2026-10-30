import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import SecretRedactingFilter
from app.models.enums import ErrorCategory, QuotaConfidence


@dataclass
class ProviderRequest:
    system_prompt: str
    user_prompt: str
    model_id: str
    require_json: bool = False
    max_tokens: int = 1024
    temperature: float = 0.3
    timeout_seconds: float = 30.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    content: str
    provider_id: str
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    quota_remaining: int | None = None
    quota_confidence: str = QuotaConfidence.UNKNOWN.value
    is_demo: bool = False


class ProviderError(Exception):
    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory,
        provider_id: str,
        model_id: str,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
    ):
        safe_msg = SecretRedactingFilter.sanitize_text(message)
        super().__init__(safe_msg)
        self.message = safe_msg
        self.category = category
        self.provider_id = provider_id
        self.model_id = model_id
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds


class AIProvider(ABC):
    """Unified adapter contract for every upstream AI provider."""

    def __init__(self, provider_id: str, base_url: str | None = None):
        self.provider_id = provider_id
        self.base_url = base_url

    @abstractmethod
    async def generate(self, request: ProviderRequest, api_key: str | None = None) -> ProviderResponse:
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self, model_id: str, api_key: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


def classify_http_failure(
    *,
    provider_id: str,
    model_id: str,
    status_code: int,
    body_text: str,
    headers: httpx.Headers | dict[str, str],
) -> ProviderError:
    lower = (body_text or "").lower()
    retry_after: int | None = None
    ra_header = headers.get("retry-after") or headers.get("Retry-After")
    if ra_header and str(ra_header).isdigit():
        retry_after = int(ra_header)

    if status_code == 401 or "api_key_invalid" in lower or "invalid api key" in lower or "incorrect api key" in lower:
        return ProviderError(
            f"Invalid API credential rejected by {provider_id}",
            category=ErrorCategory.INVALID_API_KEY,
            provider_id=provider_id,
            model_id=model_id,
            http_status=status_code,
        )
    if status_code == 402 or "billing" in lower or "payment required" in lower or "credit balance" in lower:
        return ProviderError(
            f"Billing or balance exhausted on {provider_id}",
            category=ErrorCategory.BILLING_ERROR,
            provider_id=provider_id,
            model_id=model_id,
            http_status=status_code,
        )
    if status_code == 403 or "permission" in lower or "not authorized" in lower or "access denied" in lower:
        return ProviderError(
            f"Insufficient permission to access {model_id} on {provider_id}",
            category=ErrorCategory.PERMISSION_DENIED,
            provider_id=provider_id,
            model_id=model_id,
            http_status=status_code,
        )
    if status_code == 429:
        if any(
            phrase in lower
            for phrase in ("insufficient_quota", "quota exceeded", "exceeded your current quota", "daily limit")
        ):
            return ProviderError(
                f"Quota exhausted for {model_id} on {provider_id}",
                category=ErrorCategory.QUOTA_EXHAUSTED,
                provider_id=provider_id,
                model_id=model_id,
                http_status=429,
                retry_after_seconds=retry_after,
            )
        return ProviderError(
            f"Temporary rate limit (HTTP 429) on {provider_id}/{model_id}",
            category=ErrorCategory.RATE_LIMIT_429,
            provider_id=provider_id,
            model_id=model_id,
            http_status=429,
            retry_after_seconds=retry_after or 30,
        )
    if status_code == 400:
        if "context" in lower and ("length" in lower or "token" in lower or "maximum" in lower):
            return ProviderError(
                f"Context window limit exceeded for {model_id}",
                category=ErrorCategory.CONTEXT_LENGTH_EXCEEDED,
                provider_id=provider_id,
                model_id=model_id,
                http_status=400,
            )
        return ProviderError(
            f"Malformed request to {provider_id}: {body_text[:180]}",
            category=ErrorCategory.BAD_REQUEST,
            provider_id=provider_id,
            model_id=model_id,
            http_status=400,
        )
    if status_code == 404:
        return ProviderError(
            f"Model {model_id} not found or deprecated on {provider_id}: {body_text[:160]}",
            category=ErrorCategory.PROVIDER_DOWN,
            provider_id=provider_id,
            model_id=model_id,
            http_status=404,
        )
    if status_code == 503 and ("high demand" in lower or "overloaded" in lower or "temporarily" in lower):
        return ProviderError(
            f"Model {model_id} experiencing temporary high demand (HTTP 503) on {provider_id}",
            category=ErrorCategory.RATE_LIMIT_429,
            provider_id=provider_id,
            model_id=model_id,
            http_status=503,
            retry_after_seconds=retry_after or 20,
        )
    if status_code >= 500:
        return ProviderError(
            f"Upstream provider {provider_id} unavailable (HTTP {status_code})",
            category=ErrorCategory.PROVIDER_DOWN,
            provider_id=provider_id,
            model_id=model_id,
            http_status=status_code,
        )
    return ProviderError(
        f"Provider {provider_id} returned HTTP {status_code}",
        category=ErrorCategory.BAD_REQUEST,
        provider_id=provider_id,
        model_id=model_id,
        http_status=status_code,
    )


class GeminiProvider(AIProvider):
    """Real Google Gemini REST API Adapter (generativelanguage.googleapis.com)."""

    DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"

    async def generate(self, request: ProviderRequest, api_key: str | None = None) -> ProviderResponse:
        if not api_key or not api_key.strip():
            raise ProviderError(
                "Missing API key for Google Gemini provider",
                category=ErrorCategory.INVALID_API_KEY,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )
        api_key = api_key.strip()
        base = (self.base_url or self.DEFAULT_BASE).rstrip("/")
        url = f"{base}/models/{request.model_id}:generateContent"
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": request.system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": request.user_prompt}]}],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
                "thinkingConfig": {"thinkingBudget": request.metadata.get("thinking_budget", 0)},
            },
        }
        if request.require_json:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(
                    url,
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"Connection timed out calling Gemini model {request.model_id}",
                category=ErrorCategory.TIMEOUT,
                provider_id=self.provider_id,
                model_id=request.model_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Network error calling Gemini: {exc}",
                category=ErrorCategory.PROVIDER_DOWN,
                provider_id=self.provider_id,
                model_id=request.model_id,
            ) from exc

        latency_ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code >= 400:
            raise classify_http_failure(
                provider_id=self.provider_id,
                model_id=request.model_id,
                status_code=resp.status_code,
                body_text=resp.text,
                headers=resp.headers,
            )

        data = resp.json()
        candidates = data.get("candidates") or []
        parts = (((candidates[0] if candidates else {}).get("content") or {}).get("parts")) or []
        content = "\n".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought")).strip()
        if not content:
            content = "\n".join(p.get("text", "") for p in parts if "text" in p).strip()
        usage = data.get("usageMetadata") or {}
        return ProviderResponse(
            content=content or "(Empty response from Gemini)",
            provider_id=self.provider_id,
            model_id=request.model_id,
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
            latency_ms=latency_ms,
            quota_remaining=None,
            quota_confidence=QuotaConfidence.UNKNOWN.value,
            is_demo=False,
        )

    async def test_connection(self, model_id: str, api_key: str | None = None) -> dict[str, Any]:
        req = ProviderRequest(
            system_prompt="Reply concisely.",
            user_prompt="Respond with OK.",
            model_id=model_id,
            max_tokens=16,
            timeout_seconds=12.0,
        )
        res = await self.generate(req, api_key=api_key)
        return {"ok": True, "latency_ms": res.latency_ms, "sample": res.content[:60], "is_demo": False}


class OpenAICompatibleProvider(AIProvider):
    """Real adapter for OpenRouter, Groq, OpenAI, Together, or local Ollama (/v1/chat/completions)."""

    DEFAULT_BASE = "https://openrouter.ai/api/v1"

    async def generate(self, request: ProviderRequest, api_key: str | None = None) -> ProviderResponse:
        base = (self.base_url or self.DEFAULT_BASE).rstrip("/")
        is_local = "localhost" in base or "127.0.0.1" in base
        if not api_key and not is_local:
            raise ProviderError(
                f"Missing API key for provider {self.provider_id}",
                category=ErrorCategory.INVALID_API_KEY,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.require_json:
            payload["response_format"] = {"type": "json_object"}

        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                resp = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"Timeout calling {self.provider_id}/{request.model_id}",
                category=ErrorCategory.TIMEOUT,
                provider_id=self.provider_id,
                model_id=request.model_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Network error calling {self.provider_id}: {exc}",
                category=ErrorCategory.PROVIDER_DOWN,
                provider_id=self.provider_id,
                model_id=request.model_id,
            ) from exc

        latency_ms = int((time.perf_counter() - t0) * 1000)
        if resp.status_code >= 400:
            raise classify_http_failure(
                provider_id=self.provider_id,
                model_id=request.model_id,
                status_code=resp.status_code,
                body_text=resp.text,
                headers=resp.headers,
            )

        data = resp.json()
        choices = data.get("choices") or []
        msg = (choices[0].get("message") if choices else {}) or {}
        content = msg.get("content") or ""
        usage = data.get("usage") or {}

        rem_header = resp.headers.get("x-ratelimit-remaining-requests")
        quota_rem = int(rem_header) if rem_header and rem_header.isdigit() else None
        confidence = QuotaConfidence.EXACT.value if quota_rem is not None else QuotaConfidence.UNKNOWN.value

        return ProviderResponse(
            content=content,
            provider_id=self.provider_id,
            model_id=request.model_id,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            quota_remaining=quota_rem,
            quota_confidence=confidence,
            is_demo=False,
        )

    async def test_connection(self, model_id: str, api_key: str | None = None) -> dict[str, Any]:
        req = ProviderRequest(
            system_prompt="Reply concisely.",
            user_prompt="Respond with OK.",
            model_id=model_id,
            max_tokens=16,
            timeout_seconds=12.0,
        )
        res = await self.generate(req, api_key=api_key)
        return {"ok": True, "latency_ms": res.latency_ms, "sample": res.content[:60], "is_demo": False}


class MockConfigurableProvider(AIProvider):
    """
    Deterministic Sandbox/Demo adapter that never consumes external tokens and never
    impersonates a real provider (all outputs explicitly set is_demo=True).
    """

    def __init__(self, provider_id: str, behavior_map: dict[str, str] | None = None):
        super().__init__(provider_id=provider_id, base_url="mock://sandbox")
        self.behavior_map = behavior_map or {}

    async def generate(self, request: ProviderRequest, api_key: str | None = None) -> ProviderResponse:
        behavior = request.metadata.get("mock_behavior") or self.behavior_map.get(request.model_id, "NORMAL")

        if behavior == "RATE_LIMIT_429":
            raise ProviderError(
                f"[Mock] Rate limit 429 on {self.provider_id}/{request.model_id}",
                category=ErrorCategory.RATE_LIMIT_429,
                provider_id=self.provider_id,
                model_id=request.model_id,
                http_status=429,
                retry_after_seconds=25,
            )
        if behavior == "QUOTA_EXHAUSTED":
            raise ProviderError(
                f"[Mock] Free quota exhausted on {self.provider_id}/{request.model_id}",
                category=ErrorCategory.QUOTA_EXHAUSTED,
                provider_id=self.provider_id,
                model_id=request.model_id,
                http_status=429,
            )
        if behavior == "INVALID_API_KEY":
            raise ProviderError(
                f"[Mock] Invalid API key for {self.provider_id}",
                category=ErrorCategory.INVALID_API_KEY,
                provider_id=self.provider_id,
                model_id=request.model_id,
                http_status=401,
            )
        if behavior == "PERMISSION_DENIED":
            raise ProviderError(
                f"[Mock] Permission denied on model {request.model_id}",
                category=ErrorCategory.PERMISSION_DENIED,
                provider_id=self.provider_id,
                model_id=request.model_id,
                http_status=403,
            )
        if behavior == "PROVIDER_DOWN":
            raise ProviderError(
                f"[Mock] Provider {self.provider_id} service outage (503)",
                category=ErrorCategory.PROVIDER_DOWN,
                provider_id=self.provider_id,
                model_id=request.model_id,
                http_status=503,
            )
        if behavior == "TIMEOUT":
            raise ProviderError(
                f"[Mock] Request timed out on {self.provider_id}/{request.model_id}",
                category=ErrorCategory.TIMEOUT,
                provider_id=self.provider_id,
                model_id=request.model_id,
            )
        if behavior == "CONTEXT_LENGTH_EXCEEDED":
            raise ProviderError(
                f"[Mock] Prompt exceeds context window of {request.model_id}",
                category=ErrorCategory.CONTEXT_LENGTH_EXCEEDED,
                provider_id=self.provider_id,
                model_id=request.model_id,
                http_status=400,
            )
        if behavior == "BILLING_ERROR":
            raise ProviderError(
                f"[Mock] Paid balance required on {self.provider_id}",
                category=ErrorCategory.BILLING_ERROR,
                provider_id=self.provider_id,
                model_id=request.model_id,
                http_status=402,
            )

        step_name = request.metadata.get("step_name", "analysis")
        agent_role = request.metadata.get("agent_role", "AI Agent")
        in_tok = max(32, len(request.system_prompt + request.user_prompt) // 4)
        if request.require_json:
            body = json.dumps(
                {
                    "mode": "DEMO_SANDBOX",
                    "agent_role": agent_role,
                    "step": step_name,
                    "provider": self.provider_id,
                    "model": request.model_id,
                    "summary": f"[DEMO] Hoàn thành bước '{step_name}' cho yêu cầu: {request.user_prompt[:120]}",
                },
                ensure_ascii=False,
            )
        else:
            body = (
                f"[DEMO SANDBOX — {self.provider_id}/{request.model_id}] "
                f"{agent_role} đã xử lý bước '{step_name}': "
                f"Phân tích dữ liệu, đối chiếu ràng buộc ngân sách và lưu kết quả trung lập cho yêu cầu "
                f"\"{request.user_prompt[:140]}\"."
            )
        out_tok = max(24, len(body) // 4)
        return ProviderResponse(
            content=body,
            provider_id=self.provider_id,
            model_id=request.model_id,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_ms=42,
            quota_remaining=50,
            quota_confidence=QuotaConfidence.ESTIMATED.value,
            is_demo=True,
        )

    async def test_connection(self, model_id: str, api_key: str | None = None) -> dict[str, Any]:
        req = ProviderRequest(
            system_prompt="Sandbox test",
            user_prompt="Ping",
            model_id=model_id,
            metadata={"step_name": "health_check"},
        )
        res = await self.generate(req, api_key=api_key)
        return {"ok": True, "latency_ms": res.latency_ms, "sample": res.content[:80], "is_demo": True}


def build_provider_adapter(provider_type: str, provider_id: str, base_url: str | None = None) -> AIProvider:
    ptype = (provider_type or "").lower()
    if ptype == "gemini":
        return GeminiProvider(provider_id=provider_id, base_url=base_url)
    if ptype in ("openai_compatible", "openrouter", "groq", "openai", "ollama"):
        return OpenAICompatibleProvider(provider_id=provider_id, base_url=base_url)
    return MockConfigurableProvider(provider_id=provider_id)
