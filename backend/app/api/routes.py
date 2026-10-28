from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.registry import chat_with_agent, set_agent_status
from app.analytics.reporting import (
    get_business_analytics,
    get_cost_analytics,
    get_system_status_report,
    get_usage_analytics,
)
from app.core.config import get_settings
import app.core.database as db_module
from app.core.database import get_db
from app.events.bus import emit_notification, event_bus
from app.models.entities import (
    AIModel,
    Agent,
    ApprovalRequest,
    AuditLog,
    Checkpoint,
    Conversation,
    Credential,
    Message,
    Notification,
    Project,
    Provider,
    SystemSetting,
    Task,
    TaskEvent,
    User,
)
from app.models.enums import (
    AgentStatus,
    AutomationLevel,
    EventType,
    NotificationType,
    QuotaStatus,
    RoutingMode,
    TaskStatus,
)
from app.providers.manager import ModelRouter, ProviderHealthChecker
from app.schemas.api_schemas import (
    AgentMessageCreate,
    AgentTaskCreate,
    BusinessMvpRequest,
    CredentialCreate,
    CredentialOut,
    CredentialUpdate,
    LoginRequest,
    ModelCreate,
    ModelUpdate,
    ProviderCreate,
    ProviderUpdate,
    RouterPolicyUpdate,
    TaskCreate,
    TokenResponse,
    UserOut,
)
from app.security.auth import (
    api_rate_limiter,
    create_access_token,
    get_current_user,
    record_audit_log,
    require_admin,
    require_operator,
    verify_password,
)
from app.security.vault import get_vault
from app.services.seed import run_business_mvp_pipeline
from app.tasks.engine import TaskEngine

router = APIRouter(prefix="/api", dependencies=[Depends(api_rate_limiter)])


def _serialize_task(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "agent_id": t.agent_id,
        "project_id": t.project_id,
        "priority": t.priority,
        "task_type": t.task_type,
        "status": t.status,
        "idempotency_key": t.idempotency_key,
        "has_side_effects": t.has_side_effects,
        "side_effect_committed": t.side_effect_committed,
        "attempt_count": t.attempt_count,
        "tried_models": t.tried_models,
        "current_provider_id": t.current_provider_id,
        "current_model_id": t.current_model_id,
        "planned_steps": t.planned_steps,
        "completed_steps": t.completed_steps,
        "total_steps": t.total_steps,
        "progress_pct": t.progress_pct,
        "result_summary": t.result_summary,
        "result_payload": t.result_payload,
        "error_log": t.error_log,
        "last_error": t.last_error,
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "estimated_cost_usd": t.estimated_cost_usd,
        "actual_cost_usd": t.actual_cost_usd,
        "requires_approval": t.requires_approval,
        "approval_granted": t.approval_granted,
        "data_origin": t.data_origin,
        "is_demo": t.is_demo,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def _serialize_agent(db: Session, a: Agent) -> dict[str, Any]:
    recent_tasks = db.scalars(
        select(Task).where(Task.agent_id == a.id).order_by(Task.created_at.desc()).limit(5)
    ).all()
    current_task = db.scalar(select(Task).where(Task.id == a.current_task_id)) if a.current_task_id else None
    total_in = sum(t.input_tokens for t in recent_tasks)
    total_out = sum(t.output_tokens for t in recent_tasks)
    total_cost = sum(t.actual_cost_usd for t in recent_tasks)
    return {
        "id": a.id,
        "name": a.name,
        "role": a.role,
        "room_id": a.room_id,
        "avatar_color": a.avatar_color,
        "status": a.status,
        "current_task_id": a.current_task_id,
        "current_task": _serialize_task(current_task) if current_task else None,
        "preferred_provider_id": a.preferred_provider_id,
        "preferred_model_id": a.preferred_model_id,
        "allowed_tools": a.allowed_tools,
        "permissions": a.permissions,
        "token_limit": a.token_limit,
        "cost_limit_usd": a.cost_limit_usd,
        "max_loops": a.max_loops,
        "timeout_seconds": a.timeout_seconds,
        "memory_summary": a.memory_summary,
        "tile_x": a.tile_x,
        "tile_y": a.tile_y,
        "tokens_consumed": total_in + total_out,
        "recorded_cost_usd": round(total_cost, 6),
        "recent_tasks": [_serialize_task(t) for t in recent_tasks],
    }


async def _bg_execute_task(task_id: str) -> None:
    with db_module.SessionLocal() as bg_db:
        engine = TaskEngine()
        await engine.execute_task(bg_db, task_id)


# ==================== HEALTH & SYSTEM ====================


@router.get("/health")
def health_check(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings_row = db.scalar(select(SystemSetting).where(SystemSetting.id == "singleton"))
    return {
        "status": "ok",
        "service": "AI Business Simulator 2.5D Backend",
        "demo_mode": bool(settings_row.demo_mode) if settings_row else True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/system/status")
def system_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_system_status_report(db)


# ==================== AUTH & GOOGLE ACCOUNT SEPARATION ====================


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email.strip().lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email hoặc mật khẩu không đúng.")
    token = create_access_token(user)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="USER_LOGIN",
        target_type="user",
        target_id=user.id,
        details={"email": user.email, "auth_method": user.auth_method},
    )
    db.commit()
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.get("/auth/me", response_model=UserOut)
def get_me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/auth/google/config")
def google_oauth_info() -> dict[str, Any]:
    """
    Explicitly communicates Google Account separation rules to the frontend:
    1. Google OAuth 2.0 is ONLY for authenticating the human user's identity.
    2. Signing in with Gmail does NOT grant free AI API quota or extract tokens.
    3. AI provider credentials must be explicitly added in API Control Room.
    """
    cfg = get_settings()
    configured = bool(cfg.GOOGLE_OAUTH_CLIENT_ID)
    return {
        "oauth_configured": configured,
        "client_id_present": configured,
        "redirect_uri": cfg.GOOGLE_OAUTH_REDIRECT_URI,
        "scope": "openid email profile",
        "separation_notice": (
            "Đăng nhập Google OAuth 2.0 chỉ dùng để xác thực danh tính người dùng. "
            "Tài khoản Gmail KHÔNG tự động cấp quyền hay quota gọi API AI. "
            "Hệ thống không đọc hộp thư Gmail, không lưu mật khẩu Gmail và không trích xuất session token."
        ),
    }


# ==================== AGENTS & CONVERSATIONS ====================


@router.get("/agents")
def list_agents(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    agents = db.scalars(select(Agent).order_by(Agent.id.asc())).all()
    return [_serialize_agent(db, a) for a in agents]


@router.get("/agents/{agent_id}")
def get_agent_detail(agent_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _serialize_agent(db, agent)


@router.post("/agents/{agent_id}/tasks")
async def assign_task_to_agent(
    agent_id: str,
    payload: AgentTaskCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    engine = TaskEngine()
    task = engine.create_task(
        db,
        title=payload.title,
        description=payload.description,
        agent_id=agent_id,
        priority=payload.priority,
        task_type=payload.task_type,
        planned_steps=payload.planned_steps,
        has_side_effects=payload.has_side_effects,
        estimated_cost_usd=payload.estimated_cost_usd,
    )
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="ASSIGN_AGENT_TASK",
        target_type="task",
        target_id=task.id,
        details={"agent_id": agent_id, "title": task.title},
    )
    db.commit()

    if payload.auto_execute and task.status == TaskStatus.QUEUED.value:
        background_tasks.add_task(_bg_execute_task, task.id)
    return _serialize_task(task)


@router.post("/agents/{agent_id}/report")
async def request_agent_report(
    agent_id: str,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    conv, _, reply = await chat_with_agent(
        db,
        agent=agent,
        user_id=user.id,
        user_message="Hãy lập báo cáo ngắn gọn về trạng thái hiện tại, tiến độ nhiệm vụ gần nhất và đề xuất tiếp theo.",
    )
    return {
        "agent_id": agent.id,
        "conversation_id": conv.id,
        "report": reply.content,
        "provider_id": reply.provider_id,
        "model_id": reply.model_id,
        "is_demo": reply.is_demo,
    }


@router.get("/agents/{agent_id}/conversations")
def list_agent_conversations(agent_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    convs = db.scalars(
        select(Conversation).where(Conversation.agent_id == agent_id).order_by(Conversation.updated_at.desc()).limit(10)
    ).all()
    result = []
    for c in convs:
        msgs = db.scalars(
            select(Message).where(Message.conversation_id == c.id).order_by(Message.created_at.asc())
        ).all()
        result.append(
            {
                "id": c.id,
                "agent_id": c.agent_id,
                "title": c.title,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                "messages": [
                    {
                        "id": m.id,
                        "sender_type": m.sender_type,
                        "content": m.content,
                        "provider_id": m.provider_id,
                        "model_id": m.model_id,
                        "tokens_used": m.tokens_used,
                        "is_demo": m.is_demo,
                        "created_at": m.created_at.isoformat() if m.created_at else None,
                    }
                    for m in msgs
                ],
            }
        )
    return result


@router.post("/agents/{agent_id}/messages")
async def send_message_to_agent(
    agent_id: str,
    payload: AgentMessageCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    agent = db.scalar(select(Agent).where(Agent.id == agent_id))
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    conv, user_msg, reply = await chat_with_agent(
        db,
        agent=agent,
        user_id=user.id,
        user_message=payload.message,
        conversation_id=payload.conversation_id,
    )
    spawned_task: dict[str, Any] | None = None
    if payload.create_task:
        engine = TaskEngine()
        t = engine.create_task(
            db,
            title=f"Yêu cầu trực tiếp: {payload.message[:60]}",
            description=payload.message,
            agent_id=agent.id,
        )
        if t.status == TaskStatus.QUEUED.value:
            background_tasks.add_task(_bg_execute_task, t.id)
        spawned_task = _serialize_task(t)

    return {
        "conversation_id": conv.id,
        "user_message": {
            "id": user_msg.id,
            "sender_type": user_msg.sender_type,
            "content": user_msg.content,
            "created_at": user_msg.created_at.isoformat(),
        },
        "agent_reply": {
            "id": reply.id,
            "sender_type": reply.sender_type,
            "content": reply.content,
            "provider_id": reply.provider_id,
            "model_id": reply.model_id,
            "tokens_used": reply.tokens_used,
            "is_demo": reply.is_demo,
            "created_at": reply.created_at.isoformat(),
        },
        "spawned_task": spawned_task,
    }


# ==================== TASKS & CHECKPOINTS ====================


@router.get("/tasks")
def list_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(100)
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    rows = db.scalars(stmt).all()
    return [_serialize_task(t) for t in rows]


@router.post("/tasks")
async def create_task_endpoint(
    payload: TaskCreate,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    engine = TaskEngine()
    task = engine.create_task(
        db,
        title=payload.title,
        description=payload.description,
        agent_id=payload.agent_id,
        priority=payload.priority,
        task_type=payload.task_type,
        planned_steps=payload.planned_steps,
        idempotency_key=payload.idempotency_key,
        has_side_effects=payload.has_side_effects,
        requires_approval=payload.requires_approval,
        estimated_cost_usd=payload.estimated_cost_usd,
        required_capabilities=payload.required_capabilities,
    )
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="CREATE_TASK",
        target_type="task",
        target_id=task.id,
        details={"title": task.title, "agent_id": task.agent_id},
    )
    db.commit()

    if payload.auto_execute and task.status == TaskStatus.QUEUED.value:
        background_tasks.add_task(_bg_execute_task, task.id)
    return _serialize_task(task)


@router.get("/tasks/{task_id}")
def get_task_endpoint(task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    task = db.scalar(select(Task).where(Task.id == task_id))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _serialize_task(task)


@router.get("/tasks/{task_id}/checkpoints")
def list_task_checkpoints(task_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    ckpts = db.scalars(
        select(Checkpoint).where(Checkpoint.task_id == task_id).order_by(Checkpoint.step_index.asc())
    ).all()
    return [
        {
            "id": c.id,
            "task_id": c.task_id,
            "step_index": c.step_index,
            "step_name": c.step_name,
            "neutral_state": c.neutral_state_json,
            "step_outputs": c.step_outputs_json,
            "side_effect_keys_committed": c.side_effect_keys_committed,
            "provider_id": c.provider_id,
            "model_id": c.model_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in ckpts
    ]


@router.post("/tasks/{task_id}/pause")
def pause_task_endpoint(
    task_id: str,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    engine = TaskEngine()
    task = engine.pause_task(db, task_id)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="PAUSE_TASK",
        target_type="task",
        target_id=task.id,
    )
    db.commit()
    return _serialize_task(task)


@router.post("/tasks/{task_id}/resume")
async def resume_task_endpoint(
    task_id: str,
    background_tasks: BackgroundTasks,
    sync: bool = Query(default=False),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    task = db.scalar(select(Task).where(Task.id == task_id))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    settings_row = ModelRouter().get_or_create_settings(db)
    if settings_row.emergency_stop:
        raise HTTPException(status_code=409, detail="Không thể tiếp tục khi EMERGENCY STOP đang bật.")

    task.status = TaskStatus.QUEUED.value
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="RESUME_TASK",
        target_type="task",
        target_id=task.id,
        details={"from_completed_steps": task.completed_steps},
    )
    db.commit()

    if sync:
        engine = TaskEngine()
        task = await engine.execute_task(db, task.id)
        return _serialize_task(task)

    background_tasks.add_task(_bg_execute_task, task.id)
    return _serialize_task(task)


@router.post("/tasks/{task_id}/cancel")
def cancel_task_endpoint(
    task_id: str,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    engine = TaskEngine()
    task = engine.cancel_task(db, task_id)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="CANCEL_TASK",
        target_type="task",
        target_id=task.id,
    )
    db.commit()
    return _serialize_task(task)


# ==================== PROVIDERS & MODELS ====================


def _serialize_model(m: AIModel) -> dict[str, Any]:
    return {
        "id": m.id,
        "provider_id": m.provider_id,
        "model_id": m.model_id,
        "display_name": m.display_name,
        "supported_tasks": m.supported_tasks,
        "supports_tool_calling": m.supports_tool_calling,
        "supports_json_mode": m.supports_json_mode,
        "supports_vision": m.supports_vision,
        "context_window": m.context_window,
        "input_cost_per_1k": m.input_cost_per_1k,
        "output_cost_per_1k": m.output_cost_per_1k,
        "tier": m.tier,
        "rate_limit_rpm": m.rate_limit_rpm,
        "quality_score": m.quality_score,
        "enabled": m.enabled,
        "status": m.status,
        "quota_status": m.quota_status,
        "quota_confidence": m.quota_confidence,
        "quota_remaining": m.quota_remaining,
        "retry_after_until": m.retry_after_until.isoformat() if m.retry_after_until else None,
        "mock_behavior": m.mock_behavior,
        "last_checked_at": m.last_checked_at.isoformat() if m.last_checked_at else None,
    }


@router.get("/providers")
def list_providers(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    providers = db.scalars(select(Provider).order_by(Provider.priority.asc())).all()
    out = []
    for p in providers:
        models = db.scalars(select(AIModel).where(AIModel.provider_id == p.id)).all()
        cred_count = len(
            db.scalars(select(Credential).where(Credential.provider_id == p.id, Credential.enabled.is_(True))).all()
        )
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "provider_type": p.provider_type,
                "base_url": p.base_url,
                "enabled": p.enabled,
                "priority": p.priority,
                "health_status": p.health_status,
                "error_count": p.error_count,
                "last_error": p.last_error,
                "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
                "requires_credential": p.requires_credential,
                "active_credential_count": cred_count,
                "is_demo": p.is_demo,
                "models": [_serialize_model(m) for m in models],
            }
        )
    return out


@router.post("/providers")
def create_provider(
    payload: ProviderCreate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if db.scalar(select(Provider).where(Provider.id == payload.id)):
        raise HTTPException(status_code=409, detail=f"Provider '{payload.id}' already exists")
    p = Provider(
        id=payload.id,
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=payload.base_url,
        enabled=payload.enabled,
        priority=payload.priority,
        requires_credential=payload.requires_credential,
        is_demo=payload.is_demo,
        health_status="HEALTHY" if not payload.requires_credential else "MISSING_CREDENTIAL",
    )
    db.add(p)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="CREATE_PROVIDER",
        target_type="provider",
        target_id=p.id,
        details={"provider_type": p.provider_type, "is_demo": p.is_demo},
    )
    db.commit()
    return {"id": p.id, "name": p.name, "provider_type": p.provider_type, "enabled": p.enabled}


@router.get("/providers/{provider_id}/models")
def get_provider_models(provider_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    models = db.scalars(select(AIModel).where(AIModel.provider_id == provider_id)).all()
    return [_serialize_model(m) for m in models]


@router.post("/providers/{provider_id}/models")
def add_provider_model(
    provider_id: str,
    payload: ModelCreate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not db.scalar(select(Provider).where(Provider.id == provider_id)):
        raise HTTPException(status_code=404, detail="Provider not found")
    full_id = f"{provider_id}:{payload.model_id}"
    if db.scalar(select(AIModel).where(AIModel.id == full_id)):
        raise HTTPException(status_code=409, detail="Model already registered on this provider")
    m = AIModel(
        id=full_id,
        provider_id=provider_id,
        model_id=payload.model_id,
        display_name=payload.display_name,
        supported_tasks=payload.supported_tasks,
        supports_tool_calling=payload.supports_tool_calling,
        supports_json_mode=payload.supports_json_mode,
        supports_vision=payload.supports_vision,
        context_window=payload.context_window,
        input_cost_per_1k=payload.input_cost_per_1k,
        output_cost_per_1k=payload.output_cost_per_1k,
        tier=payload.tier,
        rate_limit_rpm=payload.rate_limit_rpm,
        quality_score=payload.quality_score,
        mock_behavior=payload.mock_behavior,
    )
    db.add(m)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="ADD_MODEL",
        target_type="model",
        target_id=m.id,
        details={"tier": m.tier},
    )
    db.commit()
    return _serialize_model(m)


@router.patch("/models/{model_pk:path}")
def update_model(
    model_pk: str,
    payload: ModelUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    m = db.scalar(select(AIModel).where(AIModel.id == model_pk))
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    for field_name in (
        "enabled",
        "tier",
        "input_cost_per_1k",
        "output_cost_per_1k",
        "quota_status",
        "mock_behavior",
        "quality_score",
    ):
        val = getattr(payload, field_name)
        if val is not None:
            setattr(m, field_name, val)
    if payload.quota_status == QuotaStatus.AVAILABLE.value:
        m.retry_after_until = None
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="UPDATE_MODEL",
        target_type="model",
        target_id=m.id,
        details=payload.model_dump(exclude_none=True),
    )
    db.commit()
    return _serialize_model(m)


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    p = db.scalar(select(Provider).where(Provider.id == provider_id))
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    result = await ProviderHealthChecker.check_provider(db, p)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="TEST_PROVIDER_CONNECTION",
        target_type="provider",
        target_id=p.id,
        details={"ok": result.get("ok")},
    )
    db.commit()
    return result


@router.patch("/providers/{provider_id}")
def patch_provider(
    provider_id: str,
    payload: ProviderUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    p = db.scalar(select(Provider).where(Provider.id == provider_id))
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    for field_name in ("name", "base_url", "enabled", "priority", "health_status"):
        val = getattr(payload, field_name)
        if val is not None:
            setattr(p, field_name, val)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="UPDATE_PROVIDER",
        target_type="provider",
        target_id=p.id,
        details=payload.model_dump(exclude_none=True),
    )
    db.commit()
    return {"id": p.id, "name": p.name, "enabled": p.enabled, "priority": p.priority, "health_status": p.health_status}


@router.delete("/providers/{provider_id}")
def delete_provider(
    provider_id: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    p = db.scalar(select(Provider).where(Provider.id == provider_id))
    if not p:
        raise HTTPException(status_code=404, detail="Provider not found")
    db.delete(p)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="DELETE_PROVIDER",
        target_type="provider",
        target_id=provider_id,
    )
    db.commit()
    return {"deleted": True, "provider_id": provider_id}


# ==================== CREDENTIALS (STRICTLY PROTECTED & MASKED) ====================


@router.post("/credentials", response_model=CredentialOut)
def create_credential(
    payload: CredentialCreate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CredentialOut:
    provider = db.scalar(select(Provider).where(Provider.id == payload.provider_id))
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    vault = get_vault()
    encrypted, masked = vault.encrypt(payload.secret)
    cred = Credential(
        provider_id=payload.provider_id,
        model_id=payload.model_id,
        label=payload.label,
        auth_type=payload.auth_type,
        encrypted_secret=encrypted,
        masked_preview=masked,
        enabled=payload.enabled,
        priority=payload.priority,
        auth_status="CONFIGURED",
        created_by=user.id,
    )
    db.add(cred)
    if provider.health_status == "MISSING_CREDENTIAL":
        provider.health_status = "CONFIGURED"
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="ADD_CREDENTIAL",
        target_type="credential",
        target_id=payload.provider_id,
        details={"label": payload.label, "masked_preview": masked, "auth_type": payload.auth_type},
    )
    db.commit()
    db.refresh(cred)
    return CredentialOut.model_validate(cred)


@router.get("/credentials", response_model=list[CredentialOut])
def list_credentials(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[CredentialOut]:
    rows = db.scalars(select(Credential).order_by(Credential.priority.asc(), Credential.created_at.desc())).all()
    return [CredentialOut.model_validate(c) for c in rows]


@router.patch("/credentials/{credential_id}", response_model=CredentialOut)
def update_credential(
    credential_id: str,
    payload: CredentialUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> CredentialOut:
    cred = db.scalar(select(Credential).where(Credential.id == credential_id))
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    if payload.label is not None:
        cred.label = payload.label
    if payload.enabled is not None:
        cred.enabled = payload.enabled
    if payload.priority is not None:
        cred.priority = payload.priority
    if payload.secret:
        encrypted, masked = get_vault().encrypt(payload.secret)
        cred.encrypted_secret = encrypted
        cred.masked_preview = masked
        cred.auth_status = "ROTATED"
        cred.last_error = None

    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="UPDATE_CREDENTIAL",
        target_type="credential",
        target_id=cred.id,
        details={"label": cred.label, "enabled": cred.enabled, "masked_preview": cred.masked_preview},
    )
    db.commit()
    db.refresh(cred)
    return CredentialOut.model_validate(cred)


@router.delete("/credentials/{credential_id}")
def delete_credential(
    credential_id: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    cred = db.scalar(select(Credential).where(Credential.id == credential_id))
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")
    db.delete(cred)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="REVOKE_CREDENTIAL",
        target_type="credential",
        target_id=credential_id,
    )
    db.commit()
    return {"deleted": True, "credential_id": credential_id}


# ==================== ROUTER POLICY & FAILOVER DRILL ====================


@router.get("/router/policy")
def get_router_policy(db: Session = Depends(get_db)) -> dict[str, Any]:
    s = ModelRouter().get_or_create_settings(db)
    db.commit()
    return {
        "routing_mode": s.routing_mode,
        "manual_provider_id": s.manual_provider_id,
        "manual_model_id": s.manual_model_id,
        "provider_priority": s.provider_priority,
        "allow_paid_fallback": s.allow_paid_fallback,
        "daily_budget_usd": s.daily_budget_usd,
        "monthly_budget_usd": s.monthly_budget_usd,
        "approval_threshold_usd": s.approval_threshold_usd,
        "automation_level": s.automation_level,
        "demo_mode": s.demo_mode,
    }


@router.put("/router/policy")
def update_router_policy(
    payload: RouterPolicyUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    s = ModelRouter().get_or_create_settings(db)
    if payload.routing_mode is not None:
        valid_modes = {m.value for m in RoutingMode}
        if payload.routing_mode not in valid_modes:
            raise HTTPException(status_code=400, detail=f"Invalid routing_mode. Choose from {sorted(valid_modes)}")
        s.routing_mode = payload.routing_mode
    if payload.automation_level is not None:
        valid_levels = {lv.value for lv in AutomationLevel}
        if payload.automation_level not in valid_levels:
            raise HTTPException(status_code=400, detail=f"Invalid automation_level. Choose from {sorted(valid_levels)}")
        s.automation_level = payload.automation_level

    for field_name in (
        "manual_provider_id",
        "manual_model_id",
        "provider_priority",
        "allow_paid_fallback",
        "daily_budget_usd",
        "monthly_budget_usd",
        "approval_threshold_usd",
        "demo_mode",
    ):
        val = getattr(payload, field_name)
        if val is not None:
            setattr(s, field_name, val)

    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="UPDATE_ROUTER_POLICY",
        target_type="router_policy",
        target_id="singleton",
        details=payload.model_dump(exclude_none=True),
    )
    db.commit()
    return get_router_policy(db)


@router.get("/router/events")
def get_router_events(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(TaskEvent)
        .where(
            TaskEvent.event_type.in_(
                [
                    EventType.PROVIDER_SWITCHED.value,
                    EventType.QUOTA_EXHAUSTED.value,
                    EventType.PROVIDER_UNAVAILABLE.value,
                    EventType.BUDGET_LIMIT_REACHED.value,
                ]
            )
        )
        .order_by(TaskEvent.seq.desc())
        .limit(50)
    ).all()
    return [event_bus.serialize_event(r) for r in rows]


@router.post("/router/simulate-failover")
async def simulate_quota_failover_drill(
    exhaust_all_free: bool = Query(default=False),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Executes the exact live quota-exhaustion failover drill in Section 2:
    - Agent Business Researcher starts a 3-step task on Free Model A.
    - Step 1 succeeds and saves Checkpoint #1.
    - Before Step 2, Free Model A hits QUOTA_EXHAUSTED (and if exhaust_all_free=True, Free Model B too).
    - Router verifies error, emits QUOTA_EXHAUSTED + PROVIDER_SWITCHED, continues from Checkpoint #1 on Backup Model
      (or pauses safely in WAITING_FOR_PROVIDER if no free model remains under FREE_ONLY!).
    """
    alpha = db.scalar(select(AIModel).where(AIModel.id == "demo-free-alpha:alpha-flash-free"))
    beta = db.scalar(select(AIModel).where(AIModel.id == "demo-free-beta:beta-coder-free"))
    if alpha:
        alpha.quota_status = QuotaStatus.AVAILABLE.value
        alpha.mock_behavior = "NORMAL"
    if beta:
        beta.quota_status = QuotaStatus.AVAILABLE.value
        beta.mock_behavior = "QUOTA_EXHAUSTED" if exhaust_all_free else "NORMAL"
    db.commit()

    engine = TaskEngine()
    drill_task = engine.create_task(
        db,
        title=f"Diễn tập Chuyển API & Khôi phục Checkpoint ({'Hết mọi nguồn Free' if exhaust_all_free else 'Model A → Model B'})",
        description="Kiểm chứng luồng: Model A chạy Bước 1 -> Lưu Checkpoint -> Model A hết Quota ở Bước 2 -> Router tự chuyển sang Model B mà không chạy lại Bước 1.",
        agent_id="business-researcher",
        planned_steps=[
            "Thu thập nguồn dữ liệu gốc (commit trên Model A)",
            "Phân tích cơ hội sau khi chuyển Model dự phòng",
            "Tổng hợp báo cáo kiểm chứng cuối cùng",
        ],
        has_side_effects=True,
    )

    def flip_alpha_quota_after_step_0(_effect_key: str) -> None:
        m_alpha = db.scalar(select(AIModel).where(AIModel.id == "demo-free-alpha:alpha-flash-free"))
        if m_alpha and m_alpha.mock_behavior == "NORMAL":
            m_alpha.mock_behavior = "QUOTA_EXHAUSTED"
            db.flush()

    final_task = await engine.execute_task(
        db,
        drill_task.id,
        side_effect_callback=flip_alpha_quota_after_step_0,
    )
    checkpoints = list_task_checkpoints(final_task.id, db)
    return {
        "task": _serialize_task(final_task),
        "checkpoints": checkpoints,
    }


# ==================== NOTIFICATIONS ====================


@router.get("/notifications")
def list_notifications(
    unread_only: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(Notification).order_by(Notification.created_at.desc()).limit(60)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows = db.scalars(stmt).all()
    return [
        {
            "id": n.id,
            "type": n.type,
            "title": n.title,
            "message": n.message,
            "task_id": n.task_id,
            "agent_id": n.agent_id,
            "metadata": n.metadata_json,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in rows
    ]


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    notif = db.scalar(select(Notification).where(Notification.id == notification_id))
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    notif.is_read = True
    db.commit()
    return {"id": notif.id, "is_read": True}


@router.post("/notifications/read-all")
def mark_all_notifications_read(db: Session = Depends(get_db)) -> dict[str, Any]:
    unread = db.scalars(select(Notification).where(Notification.is_read.is_(False))).all()
    for n in unread:
        n.is_read = True
    db.commit()
    return {"marked_read": len(unread)}


# ==================== ANALYTICS & MEMORY ARCHIVE ====================


@router.get("/analytics/usage")
def analytics_usage(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_usage_analytics(db)


@router.get("/analytics/costs")
def analytics_costs(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_cost_analytics(db)


@router.get("/analytics/business")
def analytics_business(db: Session = Depends(get_db)) -> dict[str, Any]:
    return get_business_analytics(db)


@router.get("/memory/archive")
def get_memory_archive(db: Session = Depends(get_db)) -> dict[str, Any]:
    completed = db.scalars(
        select(Task).where(Task.status == TaskStatus.COMPLETED.value).order_by(Task.completed_at.desc()).limit(25)
    ).all()
    recoverable = db.scalars(
        select(Task)
        .where(
            Task.status.in_(
                [
                    TaskStatus.WAITING_FOR_PROVIDER.value,
                    TaskStatus.PAUSED.value,
                    TaskStatus.WAITING_FOR_APPROVAL.value,
                ]
            )
        )
        .order_by(Task.updated_at.desc())
    ).all()
    projects = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
    recent_audit = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(30)).all()

    return {
        "completed_tasks": [_serialize_task(t) for t in completed],
        "recoverable_tasks": [_serialize_task(t) for t in recoverable],
        "knowledge_documents": [
            {
                "id": p.id,
                "title": p.name,
                "stage": p.stage,
                "summary": p.summary,
                "data_origin": p.data_origin,
                "qa_verified": p.qa_passed,
                "is_demo": p.is_demo,
            }
            for p in projects
        ],
        "audit_trail": [
            {
                "id": a.id,
                "actor_id": a.actor_id,
                "actor_role": a.actor_role,
                "action": a.action,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "details": a.details_json,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in recent_audit
        ],
    }


# ==================== APPROVALS, AUDIT & SAFE CONTROL ====================


@router.get("/approvals")
def list_approvals(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())).all()
    return [
        {
            "id": r.id,
            "task_id": r.task_id,
            "agent_id": r.agent_id,
            "action_type": r.action_type,
            "reason": r.reason,
            "estimated_cost_usd": r.estimated_cost_usd,
            "status": r.status,
            "decided_by": r.decided_by,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/approvals/{approval_id}/approve")
async def approve_request(
    approval_id: str,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))
    if not req:
        raise HTTPException(status_code=404, detail="Approval request not found")
    req.status = "APPROVED"
    req.decided_by = user.email
    req.decided_at = datetime.now(timezone.utc)

    task = db.scalar(select(Task).where(Task.id == req.task_id))
    if task:
        task.approval_granted = True
        if req.action_type == "PUBLISH_REAL_NEWS_ARTICLE" and task.result_payload and "article" in task.result_payload:
            from app.services.real_news_engine import publish_article_to_live_portal
            publish_article_to_live_portal(task.result_payload["article"])
            task.status = TaskStatus.COMPLETED.value
            task.progress_pct = 100
        else:
            task.status = TaskStatus.QUEUED.value
            background_tasks.add_task(_bg_execute_task, task.id)

    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="APPROVE_REQUEST",
        target_type="approval",
        target_id=req.id,
        details={"task_id": req.task_id},
    )
    db.commit()
    return {"id": req.id, "status": req.status, "task_id": req.task_id}


@router.post("/approvals/{approval_id}/reject")
def reject_request(
    approval_id: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))
    if not req:
        raise HTTPException(status_code=404, detail="Approval request not found")
    req.status = "REJECTED"
    req.decided_by = user.email
    req.decided_at = datetime.now(timezone.utc)

    task = db.scalar(select(Task).where(Task.id == req.task_id))
    if task:
        task.status = TaskStatus.CANCELLED.value

    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="REJECT_REQUEST",
        target_type="approval",
        target_id=req.id,
        details={"task_id": req.task_id},
    )
    db.commit()
    return {"id": req.id, "status": req.status, "task_id": req.task_id}


@router.get("/audit-logs")
def list_audit_logs(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100)).all()
    return [
        {
            "id": a.id,
            "actor_id": a.actor_id,
            "actor_role": a.actor_role,
            "action": a.action,
            "target_type": a.target_type,
            "target_id": a.target_id,
            "details": a.details_json,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


@router.get("/control/status")
def control_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    s = ModelRouter().get_or_create_settings(db)
    return {
        "system_paused": s.system_paused,
        "emergency_stop": s.emergency_stop,
        "automation_level": s.automation_level,
        "routing_mode": s.routing_mode,
        "demo_mode": s.demo_mode,
    }


@router.post("/control/pause")
def control_pause(
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    s = ModelRouter().get_or_create_settings(db)
    s.system_paused = True
    event_bus.publish(db, event_type=EventType.SYSTEM_PAUSED.value, entity_id="system", payload={"by": user.email})
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="SYSTEM_PAUSE",
        target_type="system",
        target_id="singleton",
    )
    db.commit()
    return control_status(db)


@router.post("/control/resume")
def control_resume(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    s = ModelRouter().get_or_create_settings(db)
    s.system_paused = False
    s.emergency_stop = False
    event_bus.publish(db, event_type=EventType.SYSTEM_RESUMED.value, entity_id="system", payload={"by": user.email})
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="SYSTEM_RESUME",
        target_type="system",
        target_id="singleton",
    )
    db.commit()
    return control_status(db)


@router.post("/control/emergency-stop")
def control_emergency_stop(
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Halts intake of new tasks, pauses queued/running tasks safely WITHOUT deleting any tasks,
    logs, or checkpoints.
    """
    s = ModelRouter().get_or_create_settings(db)
    s.emergency_stop = True
    s.system_paused = True

    active_tasks = db.scalars(
        select(Task).where(Task.status.in_([TaskStatus.CREATED.value, TaskStatus.QUEUED.value, TaskStatus.RUNNING.value]))
    ).all()
    for t in active_tasks:
        t.status = TaskStatus.PAUSED.value
    agents = db.scalars(select(Agent).where(Agent.status == AgentStatus.WORKING.value)).all()
    for a in agents:
        set_agent_status(db, a, AgentStatus.PAUSED.value, a.current_task_id)

    event_bus.publish(
        db,
        event_type=EventType.EMERGENCY_STOP_TRIGGERED.value,
        entity_id="system",
        payload={"triggered_by": user.email, "paused_tasks": len(active_tasks)},
    )
    emit_notification(
        db,
        notif_type=NotificationType.WARNING.value,
        title="EMERGENCY STOP ĐÃ KÍCH HOẠT",
        message=(
            f"Hệ thống đã chặn nhận task mới và tạm dừng an toàn {len(active_tasks)} tác vụ. "
            "Toàn bộ dữ liệu, nhật ký và checkpoint được bảo toàn nguyên vẹn."
        ),
    )
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="EMERGENCY_STOP",
        target_type="system",
        target_id="singleton",
        details={"paused_tasks_count": len(active_tasks)},
    )
    db.commit()
    return {**control_status(db), "paused_tasks_count": len(active_tasks)}


# ==================== EVENTS REPLAY & BUSINESS MVP WORKFLOW ====================


@router.get("/events")
def replay_events_endpoint(
    since_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=300),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return event_bus.replay_events(db, since_seq=since_seq, limit=limit)


@router.post("/workflows/business-mvp")
async def trigger_business_mvp_workflow(
    payload: BusinessMvpRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="START_BUSINESS_MVP_PIPELINE",
        target_type="workflow",
        target_id="business-mvp",
        details={"niche_topic": payload.niche_topic},
    )
    db.commit()
    return await run_business_mvp_pipeline(db, payload.niche_topic)


@router.get("/workflows/sandbox-preview/{slug}", response_class=HTMLResponse)
def view_sandbox_preview(slug: str, db: Session = Depends(get_db)) -> HTMLResponse:
    proj = db.scalar(select(Project).where(Project.slug == slug))
    if not proj or not proj.sandbox_html:
        raise HTTPException(status_code=404, detail="Sandbox preview not found")
    return HTMLResponse(content=proj.sandbox_html)
