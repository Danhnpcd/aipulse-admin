import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.entities import PaymentOrder, RevenueRecord, User, VietQRConfig
from app.models.enums import UserRole
from app.security.auth import api_rate_limiter, decode_access_token, require_admin
from app.services.vietqr_payment_service import (
    VIETNAMESE_BANKS,
    create_vietqr_order,
    get_or_create_vietqr_config,
    process_vietqr_reconciliation_webhook,
)

payment_router = APIRouter(prefix="/api/payments", tags=["Real VietQR Payment Gateway"], dependencies=[Depends(api_rate_limiter)])


class VietQRCheckoutRequest(BaseModel):
    catalog_item_id: str | None = None
    project_id: str | None = None
    customer_email: str = Field(..., min_length=3, max_length=150)
    amount_vnd: int | None = Field(default=None, ge=1000)
    product_title: str | None = None


class VietQRConfigUpdateRequest(BaseModel):
    bank_id: str | None = Field(default=None, description="Mã ngân hàng (MB, VCB, TCB, VPB, ACB...)")
    account_no: str | None = Field(default=None, min_length=5, max_length=40)
    account_name: str | None = Field(default=None, min_length=3, max_length=120)
    exchange_rate_usd_vnd: float | None = Field(default=None, ge=1000.0)
    qr_template: str | None = Field(default=None, pattern="^(compact|compact2|qr_only)$")
    enabled: bool | None = None


@payment_router.get("/banks")
def list_supported_banks() -> dict[str, Any]:
    return {"banks": VIETNAMESE_BANKS}


@payment_router.get("/vietqr/config")
def get_vietqr_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    cfg = get_or_create_vietqr_config(db)
    return {
        "enabled": cfg.enabled,
        "bank_id": cfg.bank_id,
        "bank_name": VIETNAMESE_BANKS.get(cfg.bank_id, {}).get("name", cfg.bank_id),
        "account_no": cfg.account_no,
        "account_name": cfg.account_name,
        "exchange_rate_usd_vnd": cfg.exchange_rate_usd_vnd,
        "qr_template": cfg.qr_template,
        "auto_confirm_on_match": cfg.auto_confirm_on_match,
        "webhook_url": "/api/payments/webhook/vietqr",
    }


@payment_router.put("/vietqr/config")
def update_vietqr_settings(
    payload: VietQRConfigUpdateRequest,
    _user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    cfg = get_or_create_vietqr_config(db)
    if payload.bank_id:
        b = payload.bank_id.strip().upper()
        if b in VIETNAMESE_BANKS:
            cfg.bank_id = b
    if payload.account_no:
        cfg.account_no = payload.account_no.strip()
    if payload.account_name:
        cfg.account_name = payload.account_name.strip().upper()
    if payload.exchange_rate_usd_vnd is not None:
        cfg.exchange_rate_usd_vnd = payload.exchange_rate_usd_vnd
    if payload.qr_template:
        cfg.qr_template = payload.qr_template
    if payload.enabled is not None:
        cfg.enabled = payload.enabled
    db.commit()
    return {"ok": True, "config": get_vietqr_settings(db)}


@payment_router.post("/vietqr/checkout")
def create_vietqr_checkout(
    payload: VietQRCheckoutRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return create_vietqr_order(
        db,
        catalog_item_id=payload.catalog_item_id,
        project_id=payload.project_id,
        customer_email=payload.customer_email,
        amount_vnd=payload.amount_vnd,
        product_title=payload.product_title,
    )


@payment_router.get("/orders/{order_code}")
def check_order_payment_status(
    order_code: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    clean_code = order_code.strip().upper()
    order = db.scalar(select(PaymentOrder).where(PaymentOrder.checkout_session_id == clean_code))
    if not order:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn hàng.")
    return {
        "order_code": order.checkout_session_id,
        "status": order.status,
        "paid_verified": bool(order.status == "PAID_VERIFIED"),
        "gross_amount_usd": order.gross_amount_usd,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "verified_at": order.verified_at.isoformat() if order.verified_at else None,
    }


@payment_router.post("/webhook/vietqr")
async def receive_vietqr_bank_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Automated banking webhook: Accepts bank balance changes from Casso, PayOS, SeAPay,
    or bank gateway services, matching order codes and confirming real payments.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Payload JSON không hợp lệ.") from exc

    return await process_vietqr_reconciliation_webhook(db, payload=payload)


@payment_router.get("/revenue-summary")
def get_verified_revenue_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Returns actual reconciled revenue vs sandbox demo revenue."""
    real_gross = db.scalar(
        select(func.coalesce(func.sum(RevenueRecord.gross_amount_usd), 0.0)).where(
            RevenueRecord.is_demo.is_(False), RevenueRecord.verified.is_(True)
        )
    ) or 0.0

    demo_gross = db.scalar(
        select(func.coalesce(func.sum(RevenueRecord.gross_amount_usd), 0.0)).where(
            RevenueRecord.is_demo.is_(True)
        )
    ) or 0.0

    real_orders_count = db.scalar(
        select(func.count(PaymentOrder.id)).where(PaymentOrder.status == "PAID_VERIFIED")
    ) or 0

    cfg = get_or_create_vietqr_config(db)
    real_vnd = round(real_gross * cfg.exchange_rate_usd_vnd)

    return {
        "real_revenue_usd": round(real_gross, 2),
        "real_revenue_vnd": real_vnd,
        "real_paid_orders_count": int(real_orders_count),
        "demo_mock_revenue_usd": round(demo_gross, 2),
        "exchange_rate": cfg.exchange_rate_usd_vnd,
        "currency": "VND / USD",
    }
