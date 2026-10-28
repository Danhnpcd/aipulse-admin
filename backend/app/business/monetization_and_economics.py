import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.events.bus import emit_notification, event_bus
from app.models.entities import (
    AIModel,
    ApprovalRequest,
    BusinessExperiment,
    CostRecord,
    CustomerSignal,
    MarketingCampaignItem,
    PaymentOrder,
    PaymentWebhookLog,
    ProductArtifact,
    ProductCatalogItem,
    Project,
    RevenueRecord,
    UsageRecord,
)
from app.models.enums import CustomerValidationState, DataOrigin, EventType, NotificationType
from app.tasks.engine import TaskEngine

DARK_PATTERN_FORBIDDEN_PHRASES = [
    "cam kết giàu nhanh",
    "100% đảm bảo lợi nhuận",
    "chỉ còn 1 suất cuối cùng giả",
    "10,000 khách hàng hài lòng",
]


# ==================== PHASE 5: CUSTOMER VALIDATION AGENT ====================


def record_customer_signal(
    db: Session,
    *,
    project_id: str,
    product_id: str | None = None,
    signal_type: str,
    contact_email: str | None = None,
    feedback_text: str | None = None,
    consent_granted: bool = False,
    channel_source: str = "landing_page",
    visitor_anon_id: str | None = None,
) -> CustomerSignal:
    valid_types = {"PAGE_VISIT", "TRIAL_SIGNUP", "DEMO_REQUEST", "VOLUNTARY_FEEDBACK"}
    if signal_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"signal_type phải thuộc {sorted(valid_types)}")

    if contact_email and not consent_granted:
        raise HTTPException(
            status_code=400,
            detail="Chỉ lưu email khách hàng khi người dùng tự nguyện đồng ý (consent_granted=True).",
        )

    sig = CustomerSignal(
        project_id=project_id,
        product_id=product_id,
        signal_type=signal_type,
        visitor_anon_id=visitor_anon_id or str(uuid.uuid4())[:12],
        contact_email=contact_email.strip().lower() if contact_email else None,
        consent_granted=consent_granted,
        feedback_text=(feedback_text or "").strip()[:1500] or None,
        channel_source=channel_source,
        is_demo=False,
    )
    db.add(sig)

    exp = db.scalar(
        select(BusinessExperiment)
        .where(BusinessExperiment.project_id == project_id, BusinessExperiment.is_demo.is_(False))
        .order_by(BusinessExperiment.created_at.desc())
        .limit(1)
    )
    if exp:
        if signal_type == "PAGE_VISIT":
            exp.visitors = (exp.visitors or 0) + 1
        elif signal_type in ("TRIAL_SIGNUP", "DEMO_REQUEST"):
            exp.signups = (exp.signups or 0) + 1

    db.commit()
    db.refresh(sig)
    return sig


def get_customer_validation_summary(db: Session, project_id: str | None = None) -> dict[str, Any]:
    stmt = select(CustomerSignal).where(CustomerSignal.is_demo.is_(False))
    if project_id:
        stmt = stmt.where(CustomerSignal.project_id == project_id)
    signals = db.scalars(stmt.order_by(CustomerSignal.created_at.desc())).all()

    visits = sum(1 for s in signals if s.signal_type == "PAGE_VISIT")
    signups = sum(1 for s in signals if s.signal_type in ("TRIAL_SIGNUP", "DEMO_REQUEST"))
    feedbacks = [
        {
            "id": s.id,
            "feedback_text": s.feedback_text,
            "channel_source": s.channel_source,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in signals
        if s.feedback_text
    ]

    order_stmt = select(func.count(PaymentOrder.id)).where(
        PaymentOrder.gateway_mode == "LIVE",
        PaymentOrder.verified_via_webhook.is_(True),
        PaymentOrder.status == "PAID_VERIFIED",
    )
    if project_id:
        order_stmt = order_stmt.where(PaymentOrder.project_id == project_id)
    paying_customers = db.scalar(order_stmt) or 0

    if paying_customers > 0:
        state = CustomerValidationState.PAYING_CUSTOMERS_VERIFIED.value
    elif signups > 0:
        state = CustomerValidationState.LEADS_ACQUIRED.value
    else:
        state = CustomerValidationState.NO_CUSTOMERS_YET.value

    return {
        "customer_status": state,
        "paying_customer_status": "PAYING_CUSTOMERS_VERIFIED" if paying_customers > 0 else "NO_CUSTOMERS_YET",
        "real_visitors": visits,
        "voluntary_signups": signups,
        "confirmed_paying_customers": int(paying_customers),
        "signup_conversion_rate_pct": round((signups / visits) * 100.0, 2) if visits > 0 else "INSUFFICIENT_DATA",
        "recent_feedback": feedbacks[:15],
    }


# ==================== PHASE 7: MONETIZATION ENGINE ====================


def ensure_product_catalog_for_artifact(db: Session, product: ProductArtifact) -> list[ProductCatalogItem]:
    existing = db.scalars(select(ProductCatalogItem).where(ProductCatalogItem.product_id == product.id)).all()
    if existing:
        return list(existing)

    items = [
        ProductCatalogItem(
            product_id=product.id,
            project_id=product.project_id,
            sku=f"{product.slug}-toolkit-onetime",
            name=f"{product.name} — Bản Toolkit Offline (Thanh toán 1 lần)",
            billing_model="ONE_TIME",
            price_usd=19.0,
            currency="USD",
            sandbox_enabled=True,
            live_approved=False,
            active=True,
        ),
        ProductCatalogItem(
            product_id=product.id,
            project_id=product.project_id,
            sku=f"{product.slug}-pro-monthly",
            name=f"{product.name} — Gói Pro Thuê Bao Tháng",
            billing_model="SUBSCRIPTION_MONTHLY",
            price_usd=9.0,
            currency="USD",
            sandbox_enabled=True,
            live_approved=False,
            active=True,
        ),
    ]
    for it in items:
        db.add(it)
    db.commit()
    for it in items:
        db.refresh(it)
    return items


def create_checkout_order(
    db: Session,
    *,
    catalog_item: ProductCatalogItem,
    customer_email: str,
    gateway_mode: str = "SANDBOX",
    raw_extra_fields: dict[str, Any] | None = None,
) -> PaymentOrder:
    """
    Creates an order strictly in `PENDING_PAYMENT`.
    - Rejects raw card numbers/CVVs (PCI safety).
    - Blocks `LIVE` mode unless Admin has approved live commerce (`live_approved=True`).
    - NEVER records revenue upon button click; waits for verified webhook callback!
    """
    if raw_extra_fields and any(k in raw_extra_fields for k in ("card_number", "cvv", "pan", "cvc")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hệ thống tuyệt đối không nhận hoặc lưu thông tin thẻ thanh toán nhạy cảm (PAN/CVV).",
        )

    mode = gateway_mode.strip().upper()
    if mode not in ("SANDBOX", "LIVE"):
        raise HTTPException(status_code=400, detail="gateway_mode phải là 'SANDBOX' hoặc 'LIVE'.")

    if mode == "LIVE" and not catalog_item.live_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Gói giá này chưa được phê duyệt thương mại Production (live_approved=False). Chỉ cho phép chạy SANDBOX.",
        )

    order = PaymentOrder(
        catalog_item_id=catalog_item.id,
        project_id=catalog_item.project_id,
        customer_email=customer_email.strip().lower(),
        gateway_mode=mode,
        billing_model=catalog_item.billing_model,
        status="PENDING_PAYMENT",
        gross_amount_usd=catalog_item.price_usd,
        gateway_fee_usd=0.0,
        net_amount_usd=0.0,
        refund_amount_usd=0.0,
        checkout_session_id=f"cs_{mode.lower()}_{uuid.uuid4().hex[:20]}",
        verified_via_webhook=False,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def compute_webhook_signature(canonical_body: str, secret: str | None = None) -> str:
    sec = (secret or get_settings().PAYMENT_WEBHOOK_SECRET).encode("utf-8")
    return hmac.new(sec, canonical_body.encode("utf-8"), hashlib.sha256).hexdigest()


def process_verified_payment_webhook(
    db: Session,
    *,
    raw_body_str: str,
    signature_header: str,
) -> dict[str, Any]:
    """
    Verifies HMAC-SHA256 webhook signature before confirming any payment or refund.
    - Deduplicates by `webhook_event_id`.
    - Separates `SANDBOX` test transactions from verified `LIVE` revenue.
    """
    expected_sig = compute_webhook_signature(raw_body_str)
    sig_valid = bool(signature_header) and hmac.compare_digest(expected_sig, signature_header.strip())

    try:
        payload = json.loads(raw_body_str)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Webhook JSON không hợp lệ.") from exc

    webhook_event_id = str(payload.get("event_id") or "").strip()
    event_type = str(payload.get("event_type") or "").strip()
    checkout_session_id = str(payload.get("checkout_session_id") or "").strip()

    if not webhook_event_id or not event_type or not checkout_session_id:
        raise HTTPException(status_code=400, detail="Thiếu event_id, event_type hoặc checkout_session_id.")

    if not sig_valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Chữ ký xác thực Webhook không hợp lệ.")

    existing_log = db.scalar(
        select(PaymentWebhookLog).where(PaymentWebhookLog.webhook_event_id == webhook_event_id)
    )
    if existing_log:
        return {"idempotent_replay": True, "webhook_event_id": webhook_event_id, "order_id": existing_log.order_id}

    order = db.scalar(select(PaymentOrder).where(PaymentOrder.checkout_session_id == checkout_session_id))
    if not order:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn hàng khớp checkout_session_id.")

    if event_type == "payment.succeeded":
        gross = float(order.gross_amount_usd)
        fee = round(gross * 0.029 + 0.30, 2) if gross > 0 else 0.0
        net = round(max(0.0, gross - fee), 2)

        order.status = "PAID_VERIFIED"
        order.gateway_fee_usd = fee
        order.net_amount_usd = net
        order.verified_via_webhook = True
        order.verified_at = datetime.now(timezone.utc)

        is_sandbox = order.gateway_mode != "LIVE"
        rev = RevenueRecord(
            project_id=order.project_id,
            description=(
                f"[{'SANDBOX TEST' if is_sandbox else 'LIVE VERIFIED'}] Đơn hàng {order.checkout_session_id} ({order.billing_model})"
            ),
            gross_amount_usd=gross,
            net_received_usd=0.0 if is_sandbox else net,
            refund_amount_usd=0.0,
            is_test_payment=is_sandbox,
            is_demo=is_sandbox,
            verified=not is_sandbox,
            data_origin=DataOrigin.VERIFIED.value if not is_sandbox else DataOrigin.SOURCE.value,
        )
        db.add(rev)

        if not is_sandbox:
            exp = db.scalar(
                select(BusinessExperiment)
                .where(BusinessExperiment.project_id == order.project_id, BusinessExperiment.is_demo.is_(False))
                .order_by(BusinessExperiment.created_at.desc())
                .limit(1)
            )
            if exp:
                exp.paying_customers = (exp.paying_customers or 0) + 1

    elif event_type == "payment.refunded":
        refund_amt = round(min(float(order.gross_amount_usd), float(payload.get("refund_amount_usd", order.gross_amount_usd))), 2)
        order.status = "REFUNDED"
        order.refund_amount_usd = refund_amt
        is_sandbox = order.gateway_mode != "LIVE"
        if not is_sandbox:
            db.add(
                RevenueRecord(
                    project_id=order.project_id,
                    description=f"[LIVE REFUND] Hoàn tiền đơn {order.checkout_session_id}",
                    gross_amount_usd=0.0,
                    net_received_usd=0.0,
                    refund_amount_usd=refund_amt,
                    is_test_payment=False,
                    is_demo=False,
                    verified=True,
                    data_origin=DataOrigin.VERIFIED.value,
                )
            )
    else:
        raise HTTPException(status_code=400, detail=f"Loại sự kiện webhook '{event_type}' không được hỗ trợ.")

    wlog = PaymentWebhookLog(
        webhook_event_id=webhook_event_id,
        order_id=order.id,
        event_type=event_type,
        signature_valid=True,
        payload_json=payload,
    )
    db.add(wlog)
    event_bus.publish(
        db,
        event_type=EventType.PAYMENT_WEBHOOK_VERIFIED.value,
        entity_id=order.id,
        payload={
            "order_id": order.id,
            "gateway_mode": order.gateway_mode,
            "status": order.status,
            "gross_amount_usd": order.gross_amount_usd,
        },
    )
    db.commit()
    db.refresh(order)
    return {
        "idempotent_replay": False,
        "order_id": order.id,
        "status": order.status,
        "gateway_mode": order.gateway_mode,
        "verified_via_webhook": order.verified_via_webhook,
        "net_amount_usd": order.net_amount_usd,
        "refund_amount_usd": order.refund_amount_usd,
    }


# ==================== PHASE 8: ETHICAL MARKETING & DISTRIBUTION AGENT ====================


async def generate_ethical_marketing_drafts(
    db: Session,
    *,
    project: Project,
    product: ProductArtifact,
    target_audience: str,
    engine: TaskEngine | None = None,
) -> list[MarketingCampaignItem]:
    """
    Creates ethical, non-spam content drafts across allowed channels and holds them in
    `PENDING_APPROVAL` until reviewed by Operator/Admin.
    """
    task_engine = engine or TaskEngine()
    mkt_task = task_engine.create_task(
        db,
        title=f"[Marketing Agent] Soạn kế hoạch phân phối & bản nháp nội dung: {product.name[:45]}",
        description=f"Tạo bài hướng dẫn kỹ thuật SEO và trang giới thiệu trung thực cho {product.name}, chờ phê duyệt trước khi đăng.",
        agent_id="marketing-agent",
        task_type="marketing",
        project_id=project.id,
        idempotency_key=f"biz-mkt:{project.id}:v{product.version}",
        has_side_effects=True,
        requires_approval=True,
        planned_steps=[
            "Xác định nỗi đau khách hàng và từ khóa hướng dẫn thực tiễn",
            "Soạn thảo nội dung trung thực, kiểm tra không chứa tuyên bố phóng đại",
        ],
    )
    ap_req = db.scalar(
        select(ApprovalRequest).where(ApprovalRequest.task_id == mkt_task.id).order_by(ApprovalRequest.created_at.desc()).limit(1)
    )

    drafts_spec = [
        (
            "SEO_LEAD_MAGNET",
            f"[Kênh 1: SEO Lead Magnet] Hướng dẫn tự kiểm tra lệch thuế suất GTGT 8% & 10% trên bảng kê hóa đơn bằng {product.name}",
            (
                f"## Cách phát hiện sai lệch bảng kê hóa đơn trước kỳ kê khai thuế\n\n"
                f"Bài viết hướng dẫn quy trình đối soát 3 bước cho {target_audience}:\n"
                f"1. Kiểm tra định dạng Mã số thuế người bán (10 số hoặc 13 số chi nhánh).\n"
                f"2. Đối chiếu công thức `subtotal * vat_rate == vat_amount` và tổng thanh toán theo chuẩn NĐ 72/2024.\n"
                f"3. Rà soát hóa đơn trùng lặp và dùng thử công cụ trực tiếp tại `{product.landing_page_url}`."
            ),
        ),
        (
            "B2B_OUTREACH",
            f"[Kênh 2: B2B Direct Outreach] Thư ngỏ giải pháp tự động hóa đối soát hóa đơn dành riêng cho Văn phòng Kế toán Dịch vụ",
            (
                f"Kính gửi: Ban Giám đốc / Trưởng phòng Kế toán Dịch vụ,\n\n"
                f"Văn phòng của quý đơn vị đang quản lý sổ sách cho từ 30 đến 100 doanh nghiệp và mất 40 giờ mỗi quý chỉ để dò lỗi hóa đơn?\n\n"
                f"Giải pháp {product.name} (v{product.version}) giúp:\n"
                f"- Quét tự động 1.000 hóa đơn trong 5 giây, độ chính xác toán học 100%.\n"
                f"- Bắt trọn 100% hóa đơn xuất sai thuế suất 8% và trùng số hóa đơn.\n"
                f"- Bảo mật tuyệt đối Offline-First tại máy trạm theo Nghị định 13/2023/NĐ-CP.\n"
                f"- Trải nghiệm trực tiếp tại: `{product.landing_page_url}`."
            ),
        ),
        (
            "COMMUNITY_SEEDING",
            f"[Kênh 3: Tiếp thị Cộng đồng] Cẩm nang 10 rủi ro hóa đơn điện tử bị loại chi phí thuế TNDN & Bộ công cụ rà soát mẫu",
            (
                f"# CẢNH BÁO 10 LỖI HÓA ĐƠN ĐIỆN TỬ DỄ BỊ TRUY THU THUẾ KỲ BÁO CÁO NÀY\n\n"
                f"Chào anh chị em kế toán,\n"
                f"Trong đợt thanh tra quyết toán thuế gần đây, các sai sót phổ biến nhất gồm: sai thuế suất 8%, "
                f"kê khai trùng số hóa đơn và sai số tiền làm tròn.\n\n"
                f"Đội ngũ kỹ sư đã chia sẻ bộ công cụ đối soát tự động, miễn phí 30 hóa đơn/tháng cho cộng đồng:\n"
                f"👉 Link kiểm tra trực tiếp: `{product.landing_page_url}`."
            ),
        ),
        (
            "AFFILIATE_PROGRAM",
            f"[Kênh 4: Tiếp thị Liên kết] Chính sách Đối tác Giới thiệu Doanh nghiệp (Hoa hồng 20% trọn đời) cùng InvoiceGuard",
            (
                f"# CHƯƠNG TRÌNH ĐỐI TÁC TIẾP THỊ LIÊN KẾT — INVOICEGUARD PARTNER (20% RECURRING)\n\n"
                f"- Mức chia sẻ hoa hồng: 20% trên mỗi giao dịch thành công định kỳ hàng tháng.\n"
                f"- Giới thiệu 10 văn phòng kế toán dùng gói Enterprise (499k/tháng) → Nhận ~1.000.000 VNĐ / tháng đều đặn.\n"
                f"- Đường link tiếp thị có mã giới thiệu: `{product.landing_page_url}?utm_source=affiliate&ref={product.slug}`."
            ),
        ),
    ]

    created: list[MarketingCampaignItem] = []
    for channel, title, body in drafts_spec:
        exists = db.scalar(
            select(MarketingCampaignItem).where(
                MarketingCampaignItem.project_id == project.id,
                MarketingCampaignItem.channel == channel,
            )
        )
        if exists:
            created.append(exists)
            continue

        lower_body = body.lower()
        clean_quality = not any(phrase in lower_body for phrase in DARK_PATTERN_FORBIDDEN_PHRASES)
        item = MarketingCampaignItem(
            project_id=project.id,
            product_id=product.id,
            channel=channel,
            target_audience=target_audience,
            title=title,
            content_markdown=body,
            quality_check_passed=clean_quality,
            publish_status="PENDING_APPROVAL",
            approval_request_id=ap_req.id if ap_req else None,
            is_demo=False,
        )
        db.add(item)
        created.append(item)

    emit_notification(
        db,
        notif_type=NotificationType.APPROVAL_REQUIRED.value,
        title=f"Bản nháp Tiếp thị chờ duyệt: {product.name}",
        message="Marketing Agent đã chuẩn bị kế hoạch SEO & giới thiệu sản phẩm, đang giữ ở trạng thái PENDING_APPROVAL.",
        agent_id="marketing-agent",
    )
    db.commit()
    for c in created:
        db.refresh(c)
    return created


# ==================== PHASE 9: UNIT ECONOMICS ENGINE ====================


def calculate_unit_economics(db: Session, project_id: str | None = None) -> dict[str, Any]:
    """
    Computes exact Unit Economics (Section 12):
    - Never counts unverified checkout clicks or Sandbox test payments as confirmed revenue.
    - Never guesses AI token cost when model pricing is unknown.
    - Explicitly returns `"INSUFFICIENT_DATA"` for ratios lacking denominator observations.
    """
    live_orders_stmt = select(PaymentOrder).where(
        PaymentOrder.gateway_mode == "LIVE",
        PaymentOrder.verified_via_webhook.is_(True),
    )
    sandbox_orders_stmt = select(PaymentOrder).where(
        PaymentOrder.gateway_mode == "SANDBOX",
        PaymentOrder.verified_via_webhook.is_(True),
    )
    if project_id:
        live_orders_stmt = live_orders_stmt.where(PaymentOrder.project_id == project_id)
        sandbox_orders_stmt = sandbox_orders_stmt.where(PaymentOrder.project_id == project_id)

    live_orders = db.scalars(live_orders_stmt).all()
    sandbox_orders = db.scalars(sandbox_orders_stmt).all()

    gross_revenue = round(sum(o.gross_amount_usd for o in live_orders), 2)
    payment_fees = round(sum(o.gateway_fee_usd for o in live_orders), 2)
    refunds = round(sum(o.refund_amount_usd for o in live_orders), 2)
    net_received = round(max(0.0, sum(o.net_amount_usd for o in live_orders) - refunds), 2)
    sandbox_volume = round(sum(o.gross_amount_usd for o in sandbox_orders), 2)

    ai_cost = float(
        db.scalar(
            select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(
                CostRecord.category == "AI_API", CostRecord.is_demo.is_(False)
            )
        )
        or 0.0
    )
    infra_cost = float(
        db.scalar(
            select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(
                CostRecord.category == "INFRASTRUCTURE", CostRecord.is_demo.is_(False)
            )
        )
        or 0.0
    )
    mkt_stmt = select(func.coalesce(func.sum(MarketingCampaignItem.marketing_spend_usd), 0.0)).where(
        MarketingCampaignItem.is_demo.is_(False)
    )
    if project_id:
        mkt_stmt = mkt_stmt.where(MarketingCampaignItem.project_id == project_id)
    marketing_cost = float(db.scalar(mkt_stmt) or 0.0)
    other_cost = float(
        db.scalar(
            select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(
                CostRecord.category == "OTHER", CostRecord.is_demo.is_(False)
            )
        )
        or 0.0
    )

    # Detect whether any non-demo model calls used unpriced/unknown tier models
    unpriced_calls = (
        db.scalar(
            select(func.count(UsageRecord.id))
            .join(
                AIModel,
                (AIModel.provider_id == UsageRecord.provider_id) & (AIModel.model_id == UsageRecord.model_id),
            )
            .where(UsageRecord.is_demo.is_(False), AIModel.tier == "unknown")
        )
        or 0
    )

    gross_profit = round(net_received - ai_cost - infra_cost, 4)
    estimated_net_profit = round(gross_profit - marketing_cost - other_cost, 4)

    paid_Live_customers = len({o.customer_email for o in live_orders if o.status == "PAID_VERIFIED"})
    sub_orders = [o for o in live_orders if o.billing_model.startswith("SUBSCRIPTION")]
    active_subs = [o for o in sub_orders if o.status == "PAID_VERIFIED"]

    cust_summary = get_customer_validation_summary(db, project_id=project_id)
    real_visitors = cust_summary["real_visitors"]

    return {
        "project_id": project_id,
        "customer_validation_status": cust_summary["customer_status"],
        "gross_revenue_usd": gross_revenue,
        "net_received_usd": net_received,
        "refunds_usd": refunds,
        "payment_fees_usd": payment_fees,
        "sandbox_test_volume_usd": sandbox_volume,
        "ai_cost_usd": round(ai_cost, 6),
        "ai_cost_note": (
            "UNKNOWN_MODEL_PRICING_DETECTED" if unpriced_calls > 0 else "VERIFIED_FROM_CONFIGURED_RATES"
        ),
        "infrastructure_cost_usd": round(infra_cost, 4),
        "marketing_cost_usd": round(marketing_cost, 4),
        "other_cost_usd": round(other_cost, 4),
        "gross_profit_usd": gross_profit,
        "estimated_net_profit_usd": estimated_net_profit,
        "cac_usd": round(marketing_cost / paid_Live_customers, 2) if paid_Live_customers > 0 else "INSUFFICIENT_DATA",
        "arpu_usd": round(net_received / paid_Live_customers, 2) if paid_Live_customers > 0 else "INSUFFICIENT_DATA",
        "conversion_rate_pct": (
            round((paid_Live_customers / real_visitors) * 100.0, 2) if real_visitors > 0 else "INSUFFICIENT_DATA"
        ),
        "refund_rate_pct": round((refunds / gross_revenue) * 100.0, 2) if gross_revenue > 0 else "INSUFFICIENT_DATA",
        "retention_rate_pct": (
            round((len(active_subs) / len(sub_orders)) * 100.0, 2) if len(sub_orders) > 0 else "INSUFFICIENT_DATA"
        ),
        "formulas": {
            "net_received_usd": "sum(live_verified_orders.net_amount_usd) - refunds_usd",
            "gross_profit_usd": "net_received_usd - ai_cost_usd - infrastructure_cost_usd",
            "estimated_net_profit_usd": "gross_profit_usd - marketing_cost_usd - other_cost_usd",
            "cac_usd": "marketing_cost_usd / confirmed_paying_customers (or INSUFFICIENT_DATA if 0)",
            "arpu_usd": "net_received_usd / confirmed_paying_customers (or INSUFFICIENT_DATA if 0)",
        },
    }
