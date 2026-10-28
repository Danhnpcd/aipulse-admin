from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    display_name: str
    role: str
    auth_method: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ProviderCreate(BaseModel):
    id: str = Field(..., min_length=2, max_length=64)
    name: str
    provider_type: str = "openai_compatible"
    base_url: str | None = None
    enabled: bool = True
    priority: int = 10
    requires_credential: bool = True
    is_demo: bool = False


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    health_status: str | None = None


class ModelCreate(BaseModel):
    model_id: str
    display_name: str
    supported_tasks: list[str] = ["general"]
    supports_tool_calling: bool = True
    supports_json_mode: bool = True
    supports_vision: bool = False
    context_window: int = 32768
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    tier: str = "free"
    rate_limit_rpm: int | None = 30
    quality_score: int = 80
    mock_behavior: str = "NORMAL"


class ModelUpdate(BaseModel):
    enabled: bool | None = None
    tier: str | None = None
    input_cost_per_1k: float | None = None
    output_cost_per_1k: float | None = None
    quota_status: str | None = None
    mock_behavior: str | None = None
    quality_score: int | None = None


class CredentialCreate(BaseModel):
    provider_id: str
    model_id: str | None = None
    label: str = Field(..., min_length=2, max_length=120)
    auth_type: str = "API_KEY"
    secret: str = Field(..., min_length=4)
    enabled: bool = True
    priority: int = 1


class CredentialUpdate(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    secret: str | None = None


class CredentialOut(BaseModel):
    """Strictly omits encrypted_secret and plaintext secret."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    provider_id: str
    model_id: str | None
    label: str
    auth_type: str
    masked_preview: str
    enabled: bool
    priority: int
    auth_status: str
    last_error: str | None
    last_used_at: datetime | None
    created_at: datetime


class RouterPolicyUpdate(BaseModel):
    routing_mode: str | None = None
    manual_provider_id: str | None = None
    manual_model_id: str | None = None
    provider_priority: list[str] | None = None
    allow_paid_fallback: bool | None = None
    daily_budget_usd: float | None = None
    monthly_budget_usd: float | None = None
    approval_threshold_usd: float | None = None
    automation_level: str | None = None
    demo_mode: bool | None = None


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    description: str = Field(..., min_length=2)
    agent_id: str = "ceo-orchestrator"
    priority: int = 5
    task_type: str = "general"
    planned_steps: list[str] | None = None
    idempotency_key: str | None = None
    has_side_effects: bool = False
    requires_approval: bool = False
    estimated_cost_usd: float = 0.0
    auto_execute: bool = True
    required_capabilities: dict[str, Any] = Field(default_factory=dict)


class AgentTaskCreate(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    description: str = Field(..., min_length=2)
    priority: int = 5
    task_type: str = "general"
    planned_steps: list[str] | None = None
    has_side_effects: bool = False
    estimated_cost_usd: float = 0.0
    auto_execute: bool = True


class AgentMessageCreate(BaseModel):
    message: str = Field(..., min_length=1)
    conversation_id: str | None = None
    create_task: bool = False


class BusinessMvpRequest(BaseModel):
    niche_topic: str = Field(..., min_length=4, max_length=200)
