from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.events.bus import emit_notification, event_bus
from app.models.entities import (
    AIModel,
    CostRecord,
    Credential,
    Provider,
    SystemSetting,
    UsageRecord,
)
from app.models.enums import (
    ErrorCategory,
    EventType,
    ModelTier,
    NotificationType,
    QuotaConfidence,
    QuotaStatus,
    RoutingMode,
)
from app.providers.adapters import (
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    build_provider_adapter,
)
from app.security.vault import CredentialVault, get_vault


class NoAvailableModelError(Exception):
    def __init__(self, reason: str, budget_blocked: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.budget_blocked = budget_blocked


@dataclass
class CandidateModel:
    provider: Provider
    model: AIModel
    credential: Credential | None


class ModelRegistry:
    """Queries configured models and filters by task capabilities & context limits."""

    @staticmethod
    def list_compatible_models(
        db: Session,
        *,
        task_type: str = "general",
        require_tools: bool = False,
        require_json: bool = False,
        require_vision: bool = False,
        min_context: int = 1024,
        exclude_model_keys: set[str] | None = None,
    ) -> list[tuple[Provider, AIModel]]:
        excluded = exclude_model_keys or set()
        rows = db.execute(
            select(Provider, AIModel)
            .join(AIModel, AIModel.provider_id == Provider.id)
            .where(Provider.enabled.is_(True), AIModel.enabled.is_(True))
        ).all()

        now = datetime.now(timezone.utc)
        compatible: list[tuple[Provider, AIModel]] = []
        for provider, model in rows:
            full_key = f"{provider.id}:{model.model_id}"
            if full_key in excluded:
                continue
            if model.quota_status == QuotaStatus.QUOTA_EXHAUSTED.value:
                continue
            if model.quota_status == QuotaStatus.RATE_LIMITED.value and model.retry_after_until:
                retry_until = model.retry_after_until
                if retry_until.tzinfo is None:
                    retry_until = retry_until.replace(tzinfo=timezone.utc)
                if retry_until > now:
                    continue
            if model.context_window < min_context:
                continue
            if require_tools and not model.supports_tool_calling:
                continue
            if require_json and not model.supports_json_mode:
                continue
            if require_vision and not model.supports_vision:
                continue
            if model.supported_tasks and task_type not in model.supported_tasks and "general" not in model.supported_tasks:
                continue
            compatible.append((provider, model))
        return compatible


class QuotaTracker:
    """Tracks rate limits vs quota exhaustion without fabricating unknown quota numbers."""

    @staticmethod
    def record_success(db: Session, model: AIModel, response: ProviderResponse) -> None:
        now = datetime.now(timezone.utc)
        model.last_checked_at = now
        model.retry_after_until = None
        if response.quota_remaining is not None:
            model.quota_remaining = response.quota_remaining
            model.quota_confidence = response.quota_confidence
            model.quota_status = (
                QuotaStatus.AVAILABLE.value if response.quota_remaining > 0 else QuotaStatus.QUOTA_EXHAUSTED.value
            )
        else:
            model.quota_remaining = None
            model.quota_confidence = QuotaConfidence.UNKNOWN.value
            model.quota_status = QuotaStatus.AVAILABLE.value
        db.flush()

    @staticmethod
    def record_failure(db: Session, provider: Provider, model: AIModel, error: ProviderError) -> None:
        now = datetime.now(timezone.utc)
        model.last_checked_at = now
        provider.last_checked_at = now
        provider.error_count = (provider.error_count or 0) + 1
        provider.last_error = error.message

        if error.category == ErrorCategory.RATE_LIMIT_429:
            # Temporary 429 is NOT permanent quota exhaustion
            cooldown = error.retry_after_seconds or 30
            model.quota_status = QuotaStatus.RATE_LIMITED.value
            model.retry_after_until = now + timedelta(seconds=cooldown)
            provider.health_status = "DEGRADED"
        elif error.category in (ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.BILLING_ERROR):
            model.quota_status = QuotaStatus.QUOTA_EXHAUSTED.value
            model.quota_remaining = 0
            model.quota_confidence = QuotaConfidence.ESTIMATED.value
            provider.health_status = "DEGRADED"
        elif error.category in (ErrorCategory.PROVIDER_DOWN, ErrorCategory.TIMEOUT):
            provider.health_status = "UNAVAILABLE"
        elif error.category in (ErrorCategory.INVALID_API_KEY, ErrorCategory.PERMISSION_DENIED):
            provider.health_status = "AUTH_ERROR"
        db.flush()


class ProviderHealthChecker:
    """Executes active connectivity diagnostics against real or sandbox providers."""

    @staticmethod
    async def check_provider(
        db: Session,
        provider: Provider,
        vault: CredentialVault | None = None,
    ) -> dict[str, Any]:
        vault = vault or get_vault()
        model = db.scalar(
            select(AIModel).where(AIModel.provider_id == provider.id, AIModel.enabled.is_(True))
        )
        if not model:
            return {"ok": False, "error": "No enabled models configured for this provider"}

        api_key: str | None = None
        cred = db.scalar(
            select(Credential)
            .where(Credential.provider_id == provider.id, Credential.enabled.is_(True))
            .order_by(Credential.priority.asc())
        )
        if cred:
            api_key = vault.decrypt(cred.encrypted_secret)
        elif provider.requires_credential and not provider.is_demo:
            provider.health_status = "MISSING_CREDENTIAL"
            provider.last_checked_at = datetime.now(timezone.utc)
            db.flush()
            return {"ok": False, "error": "No active API credential configured"}

        adapter = build_provider_adapter(provider.provider_type, provider.id, provider.base_url)
        if provider.provider_type == "mock":
            adapter.behavior_map[model.model_id] = model.mock_behavior  # type: ignore[attr-defined]
        try:
            res = await adapter.test_connection(model.model_id, api_key=api_key)
            provider.health_status = "HEALTHY"
            provider.last_error = None
            provider.last_checked_at = datetime.now(timezone.utc)
            if cred:
                cred.auth_status = "VERIFIED"
                cred.last_error = None
            db.flush()
            return res
        except ProviderError as exc:
            QuotaTracker.record_failure(db, provider, model, exc)
            if cred and exc.category in (ErrorCategory.INVALID_API_KEY, ErrorCategory.PERMISSION_DENIED):
                cred.auth_status = "INVALID"
                cred.last_error = exc.message
            db.flush()
            return {"ok": False, "error": exc.message, "category": exc.category.value}


class UsageRecorder:
    """Persists token usage and transparent cost records."""

    @staticmethod
    def record(
        db: Session,
        *,
        task_id: str | None,
        agent_id: str | None,
        provider: Provider,
        model: AIModel,
        credential_id: str | None,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        success: bool,
        error_category: ErrorCategory = ErrorCategory.NONE,
        is_demo: bool = False,
    ) -> tuple[UsageRecord, CostRecord]:
        usage = UsageRecord(
            task_id=task_id,
            agent_id=agent_id,
            provider_id=provider.id,
            model_id=model.model_id,
            credential_id=credential_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            success=success,
            error_category=error_category.value,
            is_demo=is_demo or provider.is_demo,
        )
        db.add(usage)
        db.flush()

        if model.tier == ModelTier.FREE.value:
            amount = 0.0
            confidence = QuotaConfidence.EXACT.value
        elif model.tier == ModelTier.UNKNOWN.value:
            amount = 0.0
            confidence = QuotaConfidence.UNKNOWN.value
        else:
            amount = round(
                (input_tokens / 1000.0) * model.input_cost_per_1k
                + (output_tokens / 1000.0) * model.output_cost_per_1k,
                6,
            )
            confidence = QuotaConfidence.ESTIMATED.value

        cost = CostRecord(
            usage_record_id=usage.id,
            task_id=task_id,
            category="AI_API",
            provider_id=provider.id,
            model_id=model.model_id,
            amount_usd=amount if success else 0.0,
            cost_confidence=confidence,
            is_demo=is_demo or provider.is_demo,
        )
        db.add(cost)
        db.flush()

        if cost.amount_usd > 0 and success and not (is_demo or provider.is_demo):
            from app.events.bus import emit_notification
            from app.models.entities import SystemSetting
            settings_row = db.scalar(select(SystemSetting).where(SystemSetting.id == "singleton"))
            daily_limit = settings_row.daily_budget_usd if settings_row else 5.0
            spent_today = CostTracker.get_spent_today_usd(db)
            emit_notification(
                db,
                notif_type=NotificationType.WARNING.value,
                title=f"Cảnh báo Chi phí: Phát sinh ${cost.amount_usd:.5f} ({provider.name})",
                message=(
                    f"Tác vụ đã tiêu thụ {input_tokens + output_tokens} tokens trên model {model.model_id}, "
                    f"phát sinh chi phí ${cost.amount_usd:.5f} USD. "
                    f"Tổng chi tiêu thực tế hôm nay: ${spent_today:.4f} / ${daily_limit:.2f} USD."
                ),
                task_id=task_id,
            )

        return usage, cost

    @staticmethod
    def get_spent_today_usd(db: Session) -> float:
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        total = db.scalar(
            select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(
                CostRecord.created_at >= day_start,
                CostRecord.is_demo.is_(False),
            )
        )
        # Also include non-free simulated paid model spend in budget guard tests
        paid_total = db.scalar(
            select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(
                CostRecord.created_at >= day_start
            )
        )
        return float(max(total or 0.0, paid_total or 0.0))


class FallbackPolicy:
    """Orders candidate models according to FREE_FIRST, FREE_ONLY, COST_OPTIMIZED, QUALITY_FIRST, or MANUAL."""

    @staticmethod
    def rank_candidates(
        db: Session,
        settings_row: SystemSetting,
        raw_pairs: list[tuple[Provider, AIModel]],
        *,
        preferred_provider_id: str | None = None,
        preferred_model_id: str | None = None,
        estimated_tokens: int = 1000,
    ) -> tuple[list[CandidateModel], bool]:
        mode = settings_row.routing_mode
        priority_order = settings_row.provider_priority or []
        spent_today = UsageRecorder.get_spent_today_usd(db)
        budget_blocked = False

        def provider_rank(pid: str, default_prio: int) -> int:
            return priority_order.index(pid) if pid in priority_order else (100 + default_prio)

        eligible: list[CandidateModel] = []
        for provider, model in raw_pairs:
            # Verify credential availability if required
            cred = db.scalar(
                select(Credential)
                .where(
                    Credential.provider_id == provider.id,
                    Credential.enabled.is_(True),
                    (Credential.model_id.is_(None)) | (Credential.model_id == model.model_id),
                )
                .order_by(Credential.priority.asc())
            )
            if provider.requires_credential and not cred:
                continue
            # If real Mode (demo_mode == False), skip demo mock providers unless explicitly in MANUAL mode for testing
            if not settings_row.demo_mode and provider.is_demo and mode != RoutingMode.MANUAL.value:
                continue

            # Policy filtering
            if mode == RoutingMode.FREE_ONLY.value and model.tier != ModelTier.FREE.value:
                continue
            if mode == RoutingMode.FREE_FIRST.value and model.tier != ModelTier.FREE.value:
                if not settings_row.allow_paid_fallback:
                    continue

            # Budget check for paid models
            if model.tier == ModelTier.PAID.value:
                est_cost = (estimated_tokens / 1000.0) * (model.input_cost_per_1k + model.output_cost_per_1k)
                if spent_today + est_cost > settings_row.daily_budget_usd:
                    budget_blocked = True
                    continue

            eligible.append(CandidateModel(provider=provider, model=model, credential=cred))

        if mode == RoutingMode.MANUAL.value:
            target_p = settings_row.manual_provider_id or preferred_provider_id
            target_m = settings_row.manual_model_id or preferred_model_id
            manual_matches = [
                c
                for c in eligible
                if (not target_p or c.provider.id == target_p)
                and (not target_m or c.model.model_id == target_m)
            ]
            if manual_matches:
                return manual_matches, budget_blocked
            if not settings_row.allow_paid_fallback:
                return [], budget_blocked

        def sort_key(c: CandidateModel) -> tuple[Any, ...]:
            is_agent_pref = (
                0
                if (
                    preferred_provider_id == c.provider.id
                    and preferred_model_id == c.model.model_id
                    and (mode != RoutingMode.FREE_FIRST.value or c.model.tier == ModelTier.FREE.value)
                )
                else 1
            )
            p_rank = provider_rank(c.provider.id, c.provider.priority)
            total_unit_cost = c.model.input_cost_per_1k + c.model.output_cost_per_1k
            tier_rank = 0 if c.model.tier == ModelTier.FREE.value else (1 if c.model.tier == ModelTier.PAID.value else 2)
            # Prefer real verified credentials over demo mocks when both are free!
            demo_rank = 1 if c.provider.is_demo else 0

            if mode in (RoutingMode.FREE_FIRST.value, RoutingMode.FREE_ONLY.value):
                return (tier_rank, is_agent_pref, demo_rank, p_rank, total_unit_cost, -c.model.quality_score)
            if mode == RoutingMode.COST_OPTIMIZED.value:
                return (total_unit_cost, tier_rank, demo_rank, p_rank, -c.model.quality_score)
            if mode == RoutingMode.QUALITY_FIRST.value:
                return (-c.model.quality_score, demo_rank, tier_rank, p_rank)
            return (is_agent_pref, tier_rank, p_rank)

        eligible.sort(key=sort_key)
        return eligible, budget_blocked


class ModelRouter:
    """
    Central model selection and controlled failover engine.
    Logs every error, switches safely across compatible models, and emits real-time game events
    without ever exposing API keys or secrets.
    """

    def __init__(self, vault: CredentialVault | None = None):
        self.vault = vault or get_vault()

    def get_or_create_settings(self, db: Session) -> SystemSetting:
        row = db.scalar(select(SystemSetting).where(SystemSetting.id == "singleton"))
        if not row:
            row = SystemSetting(id="singleton")
            db.add(row)
            db.flush()
        return row

    def select_candidates(
        self,
        db: Session,
        *,
        task_type: str = "general",
        require_tools: bool = False,
        require_json: bool = False,
        require_vision: bool = False,
        min_context: int = 1024,
        preferred_provider_id: str | None = None,
        preferred_model_id: str | None = None,
        exclude_model_keys: set[str] | None = None,
        estimated_tokens: int = 1000,
    ) -> tuple[list[CandidateModel], bool]:
        settings_row = self.get_or_create_settings(db)
        pairs = ModelRegistry.list_compatible_models(
            db,
            task_type=task_type,
            require_tools=require_tools,
            require_json=require_json,
            require_vision=require_vision,
            min_context=min_context,
            exclude_model_keys=exclude_model_keys,
        )
        return FallbackPolicy.rank_candidates(
            db,
            settings_row,
            pairs,
            preferred_provider_id=preferred_provider_id,
            preferred_model_id=preferred_model_id,
            estimated_tokens=estimated_tokens,
        )

    async def execute_with_fallback(
        self,
        db: Session,
        *,
        request: ProviderRequest,
        task_id: str | None = None,
        agent_id: str | None = None,
        agent_name: str = "AI Agent",
        task_type: str = "general",
        require_tools: bool = False,
        require_vision: bool = False,
        min_context: int = 1024,
        preferred_provider_id: str | None = None,
        preferred_model_id: str | None = None,
        already_tried: list[str] | None = None,
    ) -> tuple[ProviderResponse, list[str]]:
        tried_keys = set(already_tried or [])
        candidates, budget_blocked = self.select_candidates(
            db,
            task_type=task_type,
            require_tools=require_tools,
            require_json=request.require_json,
            require_vision=require_vision,
            min_context=min_context,
            preferred_provider_id=preferred_provider_id,
            preferred_model_id=preferred_model_id,
            exclude_model_keys=tried_keys,
            estimated_tokens=max(500, request.max_tokens),
        )

        if not candidates:
            if budget_blocked:
                event_bus.publish(
                    db,
                    event_type=EventType.BUDGET_LIMIT_REACHED.value,
                    entity_id=task_id or "router",
                    task_id=task_id,
                    agent_id=agent_id,
                    payload={"reason": "Daily budget threshold reached; paid models blocked."},
                )
                emit_notification(
                    db,
                    notif_type=NotificationType.BUDGET_LIMIT_REACHED.value,
                    title="Chạm ngưỡng ngân sách ngày",
                    message=f"{agent_name}: Tác vụ bị chặn do chạm giới hạn ngân sách chi tiêu hàng ngày.",
                    task_id=task_id,
                    agent_id=agent_id,
                )
                raise NoAvailableModelError(
                    "Ngân sách ngày đã chạm ngưỡng cho phép; không thể gọi model trả phí.",
                    budget_blocked=True,
                )
            raise NoAvailableModelError(
                "Không còn model phù hợp trong danh sách được phép. Vui lòng bổ sung provider hoặc chờ quota khôi phục."
            )

        previous_failed: CandidateModel | None = None
        previous_error: ProviderError | None = None
        ordered_tried = list(already_tried or [])

        for candidate in candidates:
            full_key = f"{candidate.provider.id}:{candidate.model.model_id}"
            if previous_failed is not None and previous_error is not None:
                # Emit official AI_PROVIDER_SWITCHED / PROVIDER_SWITCHED event & notification BEFORE running backup
                switch_payload = {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "old_provider": previous_failed.provider.id,
                    "old_model": previous_failed.model.model_id,
                    "new_provider": candidate.provider.id,
                    "new_model": candidate.model.model_id,
                    "reason": previous_error.category.value,
                    "reason_detail": previous_error.message,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "execution_state": "SWITCHING_AND_RESUMING",
                }
                event_bus.publish(
                    db,
                    event_type=EventType.PROVIDER_SWITCHED.value,
                    entity_id=task_id or candidate.provider.id,
                    task_id=task_id,
                    agent_id=agent_id,
                    payload=switch_payload,
                )
                emit_notification(
                    db,
                    notif_type=NotificationType.PROVIDER_SWITCHED.value,
                    title=f"Chuyển model tự động: {previous_failed.model.model_id} → {candidate.model.model_id}",
                    message=(
                        f"{agent_name}: model {previous_failed.provider.name} ({previous_failed.model.model_id}) "
                        f"gặp lỗi {previous_error.category.value}. Hệ thống đang chuyển sang "
                        f"{candidate.provider.name}, model {candidate.model.model_id}. Nhiệm vụ giữ nguyên checkpoint."
                    ),
                    task_id=task_id,
                    agent_id=agent_id,
                    metadata=switch_payload,
                )

            api_key: str | None = None
            if candidate.credential:
                api_key = self.vault.decrypt(candidate.credential.encrypted_secret)

            adapter = build_provider_adapter(
                candidate.provider.provider_type,
                candidate.provider.id,
                candidate.provider.base_url,
            )
            req_for_model = ProviderRequest(
                system_prompt=request.system_prompt,
                user_prompt=request.user_prompt,
                model_id=candidate.model.model_id,
                require_json=request.require_json,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                timeout_seconds=request.timeout_seconds,
                metadata={
                    **request.metadata,
                    "mock_behavior": request.metadata.get("override_mock_behavior") or candidate.model.mock_behavior,
                },
            )

            ordered_tried.append(full_key)
            try:
                response = await adapter.generate(req_for_model, api_key=api_key)
                QuotaTracker.record_success(db, candidate.model, response)
                candidate.provider.health_status = "HEALTHY"
                candidate.provider.last_checked_at = datetime.now(timezone.utc)
                if candidate.credential:
                    candidate.credential.auth_status = "VERIFIED"
                    candidate.credential.last_used_at = datetime.now(timezone.utc)

                UsageRecorder.record(
                    db,
                    task_id=task_id,
                    agent_id=agent_id,
                    provider=candidate.provider,
                    model=candidate.model,
                    credential_id=candidate.credential.id if candidate.credential else None,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    latency_ms=response.latency_ms,
                    success=True,
                    is_demo=response.is_demo,
                )
                db.commit()
                return response, ordered_tried

            except ProviderError as err:
                QuotaTracker.record_failure(db, candidate.provider, candidate.model, err)
                if candidate.credential and err.category in (
                    ErrorCategory.INVALID_API_KEY,
                    ErrorCategory.PERMISSION_DENIED,
                ):
                    candidate.credential.auth_status = "INVALID"
                    candidate.credential.last_error = err.message

                UsageRecorder.record(
                    db,
                    task_id=task_id,
                    agent_id=agent_id,
                    provider=candidate.provider,
                    model=candidate.model,
                    credential_id=candidate.credential.id if candidate.credential else None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                    success=False,
                    error_category=err.category,
                    is_demo=candidate.provider.is_demo,
                )

                if err.category in (ErrorCategory.QUOTA_EXHAUSTED, ErrorCategory.BILLING_ERROR):
                    event_bus.publish(
                        db,
                        event_type=EventType.QUOTA_EXHAUSTED.value,
                        entity_id=candidate.provider.id,
                        task_id=task_id,
                        agent_id=agent_id,
                        payload={
                            "provider_id": candidate.provider.id,
                            "model_id": candidate.model.model_id,
                            "category": err.category.value,
                            "message": err.message,
                        },
                    )
                    emit_notification(
                        db,
                        notif_type=NotificationType.QUOTA_EXHAUSTED.value,
                        title=f"Hết quota: {candidate.provider.name} / {candidate.model.model_id}",
                        message=err.message,
                        task_id=task_id,
                        agent_id=agent_id,
                    )
                else:
                    event_bus.publish(
                        db,
                        event_type=EventType.PROVIDER_UNAVAILABLE.value,
                        entity_id=candidate.provider.id,
                        task_id=task_id,
                        agent_id=agent_id,
                        payload={
                            "provider_id": candidate.provider.id,
                            "model_id": candidate.model.model_id,
                            "category": err.category.value,
                            "message": err.message,
                        },
                    )

                db.commit()
                # Do NOT fallback on malformed client request
                if err.category == ErrorCategory.BAD_REQUEST:
                    raise
                previous_failed = candidate
                previous_error = err
                continue

        if budget_blocked:
            event_bus.publish(
                db,
                event_type=EventType.BUDGET_LIMIT_REACHED.value,
                entity_id=task_id or "router",
                task_id=task_id,
                agent_id=agent_id,
                payload={"reason": "Free models unavailable and paid fallback blocked by daily budget limit."},
            )
            emit_notification(
                db,
                notif_type=NotificationType.BUDGET_LIMIT_REACHED.value,
                title="Chạm ngưỡng ngân sách ngày",
                message=(
                    f"{agent_name}: Các nguồn miễn phí đã hết khả dụng và model trả phí bị chặn do vượt hạn mức ngân sách."
                ),
                task_id=task_id,
                agent_id=agent_id,
            )
            db.commit()
            raise NoAvailableModelError(
                "Các model miễn phí đã hết quota và model trả phí bị chặn do chạm ngưỡng ngân sách.",
                budget_blocked=True,
            )

        raise NoAvailableModelError(
            "Không còn model phù hợp trong danh sách sau khi thử tất cả nguồn dự phòng. "
            "Nhiệm vụ đã được lưu checkpoint và tạm dừng."
        )
