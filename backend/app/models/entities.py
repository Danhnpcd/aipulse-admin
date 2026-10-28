import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="operator", nullable=False)
    # Google OAuth 2.0 identity ONLY — never treated as an AI quota/token source
    google_sub: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    auth_method: Mapped[str] = mapped_column(String(32), default="LOCAL_PASSWORD", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="singleton")
    routing_mode: Mapped[str] = mapped_column(String(32), default="FREE_FIRST", nullable=False)
    manual_provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manual_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_priority: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allow_paid_fallback: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    daily_budget_usd: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    monthly_budget_usd: Mapped[float] = mapped_column(Float, default=50.0, nullable=False)
    approval_threshold_usd: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    automation_level: Mapped[str] = mapped_column(String(32), default="LEVEL_2_SANDBOX", nullable=False)
    system_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    demo_mode: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)  # gemini | openai_compatible | mock
    base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    health_status: Mapped[str] = mapped_column(String(32), default="HEALTHY", nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requires_credential: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AIModel(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(140), nullable=False)
    supported_tasks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supports_tool_calling: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_json_mode: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    context_window: Mapped[int] = mapped_column(Integer, default=32768, nullable=False)
    input_cost_per_1k: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    output_cost_per_1k: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tier: Mapped[str] = mapped_column(String(16), default="free", nullable=False)  # free | paid | unknown
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_score: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    quota_status: Mapped[str] = mapped_column(String(32), default="UNKNOWN", nullable=False)
    quota_confidence: Mapped[str] = mapped_column(String(32), default="UNKNOWN", nullable=False)
    quota_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_after_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Only used when provider_type == "mock" to simulate faults deterministically
    mock_behavior: Mapped[str] = mapped_column(String(32), default="NORMAL", nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), default="API_KEY", nullable=False)  # API_KEY | OAUTH_SERVICE_TOKEN
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    masked_preview: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    auth_status: Mapped[str] = mapped_column(String(32), default="UNVERIFIED", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(120), nullable=False)
    room_id: Mapped[str] = mapped_column(String(64), nullable=False)
    avatar_color: Mapped[str] = mapped_column(String(32), default="#38bdf8", nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    preferred_provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preferred_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="IDLE", nullable=False)
    current_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    token_limit: Mapped[int] = mapped_column(Integer, default=32000, nullable=False)
    cost_limit_usd: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    max_loops: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    memory_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tile_x: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    tile_y: Mapped[float] = mapped_column(Float, default=2.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="RESEARCH", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    opportunity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sandbox_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    qa_passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(32), default="AI_GENERATED", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("agents.id"), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    task_type: Mapped[str] = mapped_column(String(64), default="general", nullable=False)
    required_capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True, nullable=False)

    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, index=True, nullable=True)
    has_side_effects: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    side_effect_committed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    tried_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    current_provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    planned_steps: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    completed_steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_steps: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_log: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approval_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(32), default="AI_GENERATED", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Checkpoint(Base):
    __tablename__ = "checkpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(160), nullable=False)
    neutral_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    step_outputs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    side_effect_keys_committed: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TaskEvent(Base):
    __tablename__ = "task_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0", nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("agents.id"), index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    context_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sender_type: Mapped[str] = mapped_column(String(16), nullable=False)  # USER | AGENT | SYSTEM
    content: Mapped[str] = mapped_column(Text, nullable=False)
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(32), default="SOURCE", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    provider_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_category: Mapped[str] = mapped_column(String(32), default="NONE", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CostRecord(Base):
    __tablename__ = "cost_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    usage_record_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    category: Mapped[str] = mapped_column(String(32), default="AI_API", nullable=False)  # AI_API | INFRASTRUCTURE
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_confidence: Mapped[str] = mapped_column(String(32), default="EXACT", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)  # PENDING | APPROVED | REJECTED
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BusinessExperiment(Base):
    __tablename__ = "business_experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    market_segment: Mapped[str] = mapped_column(String(120), nullable=False)
    opportunity_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    validation_status: Mapped[str] = mapped_column(
        String(32), default="RESEARCHED", nullable=False
    )  # RESEARCHED | VALIDATED | BUILDING | LIVE
    visitors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    signups: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    paying_customers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(32), default="AI_GENERATED", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class RevenueRecord(Base):
    __tablename__ = "revenue_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), nullable=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    gross_amount_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    net_received_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    refund_amount_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_test_payment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(32), default="SOURCE", nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ==================== AUTONOMOUS BUSINESS ENGINE ENTITIES ====================


class BusinessOpportunity(Base):
    __tablename__ = "business_opportunities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    direction_category: Mapped[str] = mapped_column(String(64), default="MICRO_SAAS", nullable=False)
    target_customer: Mapped[str] = mapped_column(String(200), nullable=False)
    problem_statement: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    proposed_solution: Mapped[str] = mapped_column(Text, nullable=False)
    mvp_scope: Mapped[str] = mapped_column(Text, nullable=False)
    competitors_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    acquisition_channels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    pricing_hypothesis: Mapped[str] = mapped_column(String(200), nullable=False)
    estimated_dev_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    estimated_monthly_ops_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    risks_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    low_cost_validation_plan: Mapped[str] = mapped_column(Text, nullable=False)
    go_kill_criteria: Mapped[str] = mapped_column(Text, nullable=False)

    # 100-Point 6-Criteria Scoring Rubric + Per-Criterion Explanation & Confidence
    rubric_scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    total_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    low_data_warning: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    selection_status: Mapped[str] = mapped_column(String(32), default="CANDIDATE", nullable=False)
    data_origin: Mapped[str] = mapped_column(String(32), default="SOURCE", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OrchestratorDecision(Base):
    __tablename__ = "orchestrator_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=False)
    stage_from: Mapped[str] = mapped_column(String(48), nullable=False)
    stage_to: Mapped[str] = mapped_column(String(48), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    cycle_index: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    input_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    options_considered_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    success_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    execution_outcome: Mapped[str] = mapped_column(Text, default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProductArtifact(Base):
    __tablename__ = "product_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=False)
    opportunity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(32), default="0.1.0", nullable=False)
    workspace_path: Mapped[str] = mapped_column(String(255), nullable=False)
    spec_markdown: Mapped[str] = mapped_column(Text, default="", nullable=False)
    user_stories_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    acceptance_criteria_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    architecture_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    files_manifest_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    version_history_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    build_status: Mapped[str] = mapped_column(String(32), default="PLANNED", nullable=False)
    test_passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    test_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    test_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    test_output: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    landing_page_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ExperimentSpec(Base):
    __tablename__ = "experiment_specs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    experiment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("business_experiments.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=True)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    target_segment: Mapped[str] = mapped_column(String(160), nullable=False)
    validation_method: Mapped[str] = mapped_column(String(160), nullable=False)
    max_budget_usd: Mapped[float] = mapped_column(Float, default=5.0, nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    success_metric_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    stop_metric_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    criteria_locked: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    result_summary: Mapped[str] = mapped_column(Text, default="PENDING_SIGNALS", nullable=False)
    next_decision: Mapped[str] = mapped_column(String(32), default="COLLECT_DATA", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomerSignal(Base):
    __tablename__ = "customer_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    signal_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    visitor_anon_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    consent_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    feedback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel_source: Mapped[str] = mapped_column(String(64), default="direct", nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DeploymentRecord(Base):
    __tablename__ = "deployment_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_artifacts.id"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), default="staging", nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    checks_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    artifact_dir: Mapped[str] = mapped_column(String(255), nullable=False)
    rollback_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approval_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketingCampaignItem(Base):
    __tablename__ = "marketing_campaign_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    channel: Mapped[str] = mapped_column(String(48), nullable=False)
    target_audience: Mapped[str] = mapped_column(String(180), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    quality_check_passed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    publish_status: Mapped[str] = mapped_column(String(32), default="PENDING_APPROVAL", nullable=False)
    approval_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    marketing_spend_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProductCatalogItem(Base):
    __tablename__ = "product_catalog_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    product_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_artifacts.id"), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=False)
    sku: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    billing_model: Mapped[str] = mapped_column(String(48), default="ONE_TIME", nullable=False)
    price_usd: Mapped[float] = mapped_column(Float, default=9.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    sandbox_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    live_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    catalog_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_catalog_items.id"), index=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), index=True, nullable=False)
    customer_email: Mapped[str] = mapped_column(String(200), nullable=False)
    gateway_mode: Mapped[str] = mapped_column(String(16), default="SANDBOX", nullable=False)
    billing_model: Mapped[str] = mapped_column(String(48), default="ONE_TIME", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING_PAYMENT", index=True, nullable=False)
    gross_amount_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    gateway_fee_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    net_amount_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    refund_amount_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    checkout_session_id: Mapped[str] = mapped_column(String(96), unique=True, index=True, nullable=False)
    verified_via_webhook: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentWebhookLog(Base):
    __tablename__ = "payment_webhook_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    webhook_event_id: Mapped[str] = mapped_column(String(96), unique=True, index=True, nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

