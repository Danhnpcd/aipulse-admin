import hmac
import random
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import SecretRedactingFilter
from app.core.database import get_db
from app.models.entities import User, ZaloGuardianConfig, ZaloMessageLog
from app.models.enums import UserRole
from app.security.auth import api_rate_limiter, decode_access_token, record_audit_log
from app.security.vault import get_vault
from app.services.zalo_guardian_service import (
    ZaloBotClient,
    _mask_secret,
    apply_windows_anti_sleep,
    collect_live_system_diagnostics,
    discover_and_sync_tunnels,
    format_status_message_vi,
    get_or_create_zalo_config,
    resolve_decrypted_bot_token,
    zalo_guardian_service,
)

zalo_router = APIRouter(prefix="/api/zalo", tags=["Zalo AI Guardian"], dependencies=[Depends(api_rate_limiter)])


def require_local_or_operator(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    """
    Requires valid Admin/Operator Bearer JWT when accessed over Cloudflare Tunnel / external network,
    while allowing genuine localhost (127.0.0.1 without proxy headers) for local administrative scripts.
    """
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        claims = decode_access_token(token)
        user = db.scalar(select(User).where(User.id == claims.get("sub")))
        if not user or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tài khoản không hợp lệ")
        if user.role not in (UserRole.ADMIN.value, UserRole.OPERATOR.value):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cần quyền Quản trị viên (Admin/Operator)")
        return user

    # Check if genuinely local (no Cloudflare/proxy headers)
    fwd = request.headers.get("x-forwarded-for") or request.headers.get("cf-connecting-ip")
    client_host = request.client.host if request.client else ""
    if not fwd and client_host in ("127.0.0.1", "::1", "localhost"):
        return db.scalar(select(User).where(User.email == "danhnpcd@gmail.com"))

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Vui lòng đăng nhập tài khoản Quản trị viên trên thanh trên cùng để thay đổi cấu hình Zalo.",
    )


class ZaloConfigUpdate(BaseModel):
    bot_token: str | None = Field(default=None, description="Zalo Bot Platform token (stored encrypted)")
    enabled: bool | None = None
    receive_mode: str | None = Field(default=None, pattern="^(POLLING|WEBHOOK)$")
    owner_chat_id: str | None = None
    unpair_owner: bool = False
    regenerate_pin: bool = False
    anti_sleep_enabled: bool | None = None
    auto_heal_tunnels: bool | None = None
    notify_on_critical_error: bool | None = None
    notify_on_tunnel_change: bool | None = None
    notify_on_task_attention: bool | None = None
    notify_on_wakeup_recovery: bool | None = None
    periodic_report_minutes: int | None = Field(default=None, ge=0, le=1440)


class ZaloChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    also_send_to_zalo: bool = False


def _serialize_zalo_config(cfg: ZaloGuardianConfig, tunnels: dict[str, Any]) -> dict[str, Any]:
    admin_url = (tunnels.get("admin_url") or "http://127.0.0.1:8000").rstrip("/")
    return {
        "enabled": cfg.enabled,
        "receive_mode": cfg.receive_mode,
        "has_bot_token": bool(cfg.encrypted_bot_token),
        "masked_bot_token": cfg.masked_bot_token,
        "bot_id": cfg.bot_id,
        "bot_name": cfg.bot_name,
        "bot_username": cfg.bot_username,
        "connection_status": cfg.connection_status,
        "last_error": cfg.last_error,
        "owner_chat_id": cfg.owner_chat_id,
        "owner_display_name": cfg.owner_display_name,
        "pairing_pin": cfg.pairing_pin,
        "webhook_url": f"{admin_url}/api/zalo/webhook",
        "anti_sleep_enabled": cfg.anti_sleep_enabled,
        "auto_heal_tunnels": cfg.auto_heal_tunnels,
        "notify_on_critical_error": cfg.notify_on_critical_error,
        "notify_on_tunnel_change": cfg.notify_on_tunnel_change,
        "notify_on_task_attention": cfg.notify_on_task_attention,
        "notify_on_wakeup_recovery": cfg.notify_on_wakeup_recovery,
        "periodic_report_minutes": cfg.periodic_report_minutes,
        "last_heartbeat_at": cfg.last_heartbeat_at.isoformat() if cfg.last_heartbeat_at else None,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
    }


def _serialize_msg(m: ZaloMessageLog) -> dict[str, Any]:
    return {
        "id": m.id,
        "direction": m.direction,
        "channel": m.channel,
        "chat_id": m.chat_id,
        "sender_name": m.sender_name,
        "content": m.content,
        "category": m.category,
        "ai_model_used": m.ai_model_used,
        "delivered": m.delivered,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@zalo_router.get("/status")
def get_zalo_guardian_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = get_or_create_zalo_config(db)
    diag = collect_live_system_diagnostics(db)
    recent_msgs = list(
        db.scalars(
            select(ZaloMessageLog).order_by(ZaloMessageLog.created_at.desc()).limit(40)
        ).all()
    )
    recent_msgs.reverse()
    db.commit()
    return {
        "config": _serialize_zalo_config(cfg, diag["network"]),
        "diagnostics": diag,
        "messages": [_serialize_msg(m) for m in recent_msgs],
    }


@zalo_router.put("/config")
async def update_zalo_guardian_config(
    payload: ZaloConfigUpdate,
    user: User | None = Depends(require_local_or_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    cfg = get_or_create_zalo_config(db)
    tunnels = discover_and_sync_tunnels()

    if payload.enabled is not None:
        cfg.enabled = payload.enabled
    if payload.receive_mode is not None:
        cfg.receive_mode = payload.receive_mode
    if payload.unpair_owner:
        cfg.owner_chat_id = None
        cfg.owner_display_name = None
        cfg.pairing_pin = f"{random.randint(100000, 999999)}"
        if cfg.encrypted_bot_token:
            cfg.connection_status = "CONNECTED_WAITING_PAIR"
    elif payload.owner_chat_id is not None and payload.owner_chat_id.strip():
        cfg.owner_chat_id = payload.owner_chat_id.strip()
        if cfg.encrypted_bot_token:
            cfg.connection_status = "PAIRED_ACTIVE"

    if payload.regenerate_pin:
        cfg.pairing_pin = f"{random.randint(100000, 999999)}"

    if payload.anti_sleep_enabled is not None:
        cfg.anti_sleep_enabled = payload.anti_sleep_enabled
        apply_windows_anti_sleep(payload.anti_sleep_enabled)
    if payload.auto_heal_tunnels is not None:
        cfg.auto_heal_tunnels = payload.auto_heal_tunnels
    if payload.notify_on_critical_error is not None:
        cfg.notify_on_critical_error = payload.notify_on_critical_error
    if payload.notify_on_tunnel_change is not None:
        cfg.notify_on_tunnel_change = payload.notify_on_tunnel_change
    if payload.notify_on_task_attention is not None:
        cfg.notify_on_task_attention = payload.notify_on_task_attention
    if payload.notify_on_wakeup_recovery is not None:
        cfg.notify_on_wakeup_recovery = payload.notify_on_wakeup_recovery
    if payload.periodic_report_minutes is not None:
        cfg.periodic_report_minutes = payload.periodic_report_minutes

    verification_info: dict[str, Any] | None = None
    if payload.bot_token is not None and payload.bot_token.strip():
        raw_token = payload.bot_token.strip()
        # Strip optional leading "bot" prefix if user copied full URL segment
        if raw_token.startswith("bot") and ":" in raw_token and raw_token[3:4].isdigit():
            raw_token = raw_token[3:]

        vault = get_vault()
        ciphertext, masked = vault.encrypt(raw_token)
        cfg.encrypted_bot_token = ciphertext
        cfg.masked_bot_token = masked
        zalo_guardian_service._polling_webhook_cleared = False

        try:
            client = ZaloBotClient(raw_token)
            me = await client.get_me()
            cfg.bot_id = str(me.get("id") or "")
            cfg.bot_name = me.get("first_name") or me.get("account_name") or "Zalo AI Guardian"
            cfg.bot_username = me.get("username") or ""
            cfg.last_error = None
            cfg.connection_status = "PAIRED_ACTIVE" if cfg.owner_chat_id else "CONNECTED_WAITING_PAIR"

            if cfg.receive_mode == "WEBHOOK" and tunnels.get("admin_url"):
                wh_url = f"{tunnels['admin_url'].rstrip('/')}/api/zalo/webhook"
                await client.set_webhook(wh_url, cfg.webhook_secret)
            else:
                await client.delete_webhook()

            verification_info = {"ok": True, "bot_name": cfg.bot_name, "bot_username": cfg.bot_username}
        except Exception as exc:
            safe_err = SecretRedactingFilter.sanitize_text(str(exc))
            cfg.connection_status = "AUTH_ERROR"
            cfg.last_error = safe_err[:250]
            verification_info = {"ok": False, "error": cfg.last_error}

    elif payload.receive_mode is not None and cfg.encrypted_bot_token:
        raw_token = resolve_decrypted_bot_token(cfg)
        if raw_token:
            client = ZaloBotClient(raw_token)
            try:
                if cfg.receive_mode == "WEBHOOK" and tunnels.get("admin_url"):
                    wh_url = f"{tunnels['admin_url'].rstrip('/')}/api/zalo/webhook"
                    await client.set_webhook(wh_url, cfg.webhook_secret)
                else:
                    await client.delete_webhook()
                    zalo_guardian_service._polling_webhook_cleared = False
            except Exception as exc:
                cfg.last_error = SecretRedactingFilter.sanitize_text(str(exc))[:250]

    record_audit_log(
        db,
        user=user,
        action="UPDATE_ZALO_GUARDIAN_CONFIG",
        target_type="zalo_guardian",
        target_id="singleton",
        details={"receive_mode": cfg.receive_mode, "has_token": bool(cfg.encrypted_bot_token)},
    )
    db.commit()
    return {
        "ok": True,
        "verification": verification_info,
        "config": _serialize_zalo_config(cfg, tunnels),
    }


@zalo_router.post("/test-notify")
async def trigger_test_zalo_notification(
    _user: User | None = Depends(require_local_or_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    cfg = get_or_create_zalo_config(db)
    token = resolve_decrypted_bot_token(cfg)
    diag = collect_live_system_diagnostics(db)
    db.commit()

    if not token:
        return {
            "ok": False,
            "delivered_to_zalo": False,
            "message": "Chưa cấu hình Zalo Bot Token. Vui lòng tạo Bot trên Zalo Bot Manager và dán mã Token vào ô cấu hình.",
        }
    if not cfg.owner_chat_id:
        return {
            "ok": False,
            "delivered_to_zalo": False,
            "message": f"Bot '{cfg.bot_name or 'Zalo AI'}' đã kết nối thành công! Hãy mở ứng dụng Zalo và nhắn chữ 'Xin chào' vào Bot để hệ thống tự động nhận diện tài khoản Zalo của bạn.",
        }

    sent = await zalo_guardian_service.send_proactive_zalo_alert(
        alert_key="manual_test_ping",
        title="🔔 KIỂM TRA KẾT NỐI TRỢ LÝ AI ZALO THÀNH CÔNG",
        body=format_status_message_vi(diag),
        category="STATUS_REPORT",
        force=True,
    )
    return {
        "ok": sent,
        "delivered_to_zalo": sent,
        "message": (
            f"Đã gửi báo cáo trực tiếp tới Zalo của {cfg.owner_display_name or cfg.owner_chat_id}!"
            if sent
            else "Gửi tin nhắn Zalo thất bại. Vui lòng kiểm tra lại kết nối mạng hoặc Bot Token."
        ),
    }


@zalo_router.post("/chat")
async def chat_with_zalo_guardian_from_web(
    payload: ZaloChatRequest,
    user: User | None = Depends(require_local_or_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    cfg = get_or_create_zalo_config(db)
    sender = user.display_name if user else (cfg.owner_display_name or "Quản trị viên")
    owner_chat = cfg.owner_chat_id
    db.commit()

    res = await zalo_guardian_service.handle_incoming_message(
        chat_id=owner_chat or "WEB_ADMIN_OWNER",
        sender_name=sender,
        text=payload.message,
        channel="WEB_CONSOLE",
    )

    if payload.also_send_to_zalo and owner_chat and res.get("reply"):
        await zalo_guardian_service.send_proactive_zalo_alert(
            alert_key=f"web_sync_{random.randint(1000, 9999)}",
            title="💬 [Đồng bộ từ Web Admin]",
            body=res["reply"],
            category="CHAT",
            force=True,
        )

    return res


@zalo_router.post("/self-heal")
async def trigger_manual_self_heal(
    _user: User | None = Depends(require_local_or_operator),
) -> dict[str, Any]:
    return await zalo_guardian_service.execute_self_healing(trigger_reason="web_admin_button")


@zalo_router.post("/webhook")
async def receive_zalo_bot_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_bot_api_secret_token: str | None = Header(default=None, alias="X-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Official Zalo Bot Platform Webhook endpoint.
    Validates X-Bot-Api-Secret-Token and acknowledges within <20ms (under Zalo's 2s limit).
    """
    cfg = get_or_create_zalo_config(db)
    expected_secret = cfg.webhook_secret or ""
    if not x_bot_api_secret_token or not hmac.compare_digest(x_bot_api_secret_token, expected_secret):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid webhook secret token")

    try:
        body = await request.json()
    except Exception:
        return {"ok": True}

    msg_obj = body.get("message") or body.get("data") or {}
    text = (msg_obj.get("text") or "").strip()
    if text:
        chat_obj = msg_obj.get("chat") or {}
        from_obj = msg_obj.get("from") or msg_obj.get("sender") or {}
        chat_id = str(chat_obj.get("id") or from_obj.get("id") or "")
        sender_name = (
            from_obj.get("first_name")
            or from_obj.get("display_name")
            or from_obj.get("username")
            or "Zalo User"
        )
        ext_id = str(msg_obj.get("message_id") or body.get("update_id") or "")
        if chat_id:
            background_tasks.add_task(
                zalo_guardian_service.handle_incoming_message,
                chat_id=chat_id,
                sender_name=sender_name,
                text=text,
                channel="ZALO",
                external_message_id=ext_id or None,
            )

    return {"ok": True}
