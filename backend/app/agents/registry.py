from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.events.bus import event_bus
from app.models.entities import Agent, Conversation, Message, Task
from app.models.enums import AgentStatus, DataOrigin, EventType
from app.providers.adapters import ProviderRequest
from app.providers.manager import ModelRouter, NoAvailableModelError

INITIAL_AGENTS: list[dict[str, Any]] = [
    {
        "id": "ceo-orchestrator",
        "name": "CEO Orchestrator",
        "role": "Điều phối Chiến lược & Quản trị Doanh nghiệp",
        "room_id": "command-center",
        "avatar_color": "#f59e0b",
        "tile_x": 1.95,
        "tile_y": 1.95,
        "system_prompt": (
            "Bạn là CEO Orchestrator của AI Business Simulator. Điều phối mục tiêu kinh doanh, "
            "phân rã kế hoạch có giới hạn độ sâu, tuân thủ ngân sách và không tự cấp thêm quyền."
        ),
        "allowed_tools": ["plan_workflow", "assign_agent_task", "review_kpi"],
        "permissions": ["read:company", "orchestrate:bounded_tasks"],
        "token_limit": 32000,
        "cost_limit_usd": 2.0,
        "max_loops": 4,
    },
    {
        "id": "business-researcher",
        "name": "Business Researcher",
        "role": "Nghiên cứu Thị trường & Cơ hội Số",
        "room_id": "research-lab",
        "avatar_color": "#38bdf8",
        "tile_x": 5.45,
        "tile_y": 2.25,
        "system_prompt": (
            "Bạn là Business Researcher. Phân tích nhu cầu thị trường ngách, nguồn dữ liệu hợp pháp, "
            "đau điểm khách hàng và đề xuất sản phẩm số khả thi."
        ),
        "allowed_tools": ["market_scan", "competitor_matrix", "save_research_note"],
        "permissions": ["read:market", "write:research_experiment"],
        "token_limit": 24000,
        "cost_limit_usd": 1.5,
        "max_loops": 4,
    },
    {
        "id": "business-evaluator",
        "name": "Business Evaluator",
        "role": "Thẩm định Mô hình Kinh doanh & Rủi ro",
        "room_id": "research-lab",
        "avatar_color": "#818cf8",
        "tile_x": 7.05,
        "tile_y": 1.25,
        "system_prompt": (
            "Bạn là Business Evaluator. Chấm điểm cơ hội kinh doanh (0-100) dựa trên chi phí triển khai, "
            "khả năng thu phí, rủi ro pháp lý và biên lợi nhuận."
        ),
        "allowed_tools": ["score_opportunity", "unit_economics_check"],
        "permissions": ["read:research_experiment", "write:evaluation_score"],
        "token_limit": 20000,
        "cost_limit_usd": 1.0,
        "max_loops": 3,
    },
    {
        "id": "product-builder",
        "name": "Product Builder",
        "role": "Kỹ sư Phát triển Sản phẩm Số & Ứng dụng AI",
        "room_id": "dev-lab",
        "avatar_color": "#10b981",
        "tile_x": 1.05,
        "tile_y": 6.95,
        "system_prompt": (
            "Bạn là Product Builder. Thiết kế kiến trúc, viết mã sạch và dựng bản xem trước sản phẩm số "
            "trong môi trường Sandbox được cấp quyền."
        ),
        "allowed_tools": ["generate_sandbox_app", "compile_preview", "save_artifact"],
        "permissions": ["read:specs", "write:sandbox_code"],
        "token_limit": 48000,
        "cost_limit_usd": 2.5,
        "max_loops": 5,
    },
    {
        "id": "qa-engineer",
        "name": "QA Engineer",
        "role": "Kiểm thử Tự động & Đảm bảo Chất lượng",
        "room_id": "dev-lab",
        "avatar_color": "#2dd4bf",
        "tile_x": 2.65,
        "tile_y": 5.75,
        "system_prompt": (
            "Bạn là QA Engineer. Kiểm tra tính toàn vẹn giao diện, hiệu năng, bảo mật cơ bản và xác nhận "
            "điều kiện nghiệm thu trước khi xuất bản."
        ),
        "allowed_tools": ["run_smoke_suite", "accessibility_audit", "report_defect"],
        "permissions": ["read:sandbox_code", "write:qa_report"],
        "token_limit": 24000,
        "cost_limit_usd": 1.0,
        "max_loops": 4,
    },
    {
        "id": "marketing-agent",
        "name": "Marketing Agent",
        "role": "Tiếp thị Nội dung Hợp pháp & Chuyển đổi",
        "room_id": "marketing-studio",
        "avatar_color": "#ec4899",
        "tile_x": 10.85,
        "tile_y": 1.95,
        "system_prompt": (
            "Bạn là Marketing Agent. Xây dựng thông điệp giá trị trung thực, nội dung landing page và "
            "kế hoạch phân phối hợp pháp (không spam, không tạo đánh giá giả)."
        ),
        "allowed_tools": ["draft_copy", "plan_channel_mix", "conversion_funnel"],
        "permissions": ["read:product", "write:marketing_draft"],
        "token_limit": 24000,
        "cost_limit_usd": 1.2,
        "max_loops": 4,
    },
    {
        "id": "finance-analyst",
        "name": "Finance Analyst",
        "role": "Quản trị Chi phí API, Ngân sách & Doanh thu",
        "room_id": "finance-office",
        "avatar_color": "#eab308",
        "tile_x": 1.95,
        "tile_y": 10.95,
        "system_prompt": (
            "Bạn là Finance Analyst. Theo dõi sát chi phí token AI, hạ tầng, doanh thu thực nhận, hoàn tiền "
            "và cảnh báo sớm khi tiệm cận giới hạn ngân sách."
        ),
        "allowed_tools": ["audit_token_spend", "forecast_runway", "reconcile_revenue"],
        "permissions": ["read:finance", "write:finance_report"],
        "token_limit": 20000,
        "cost_limit_usd": 1.0,
        "max_loops": 3,
    },
    {
        "id": "security-auditor",
        "name": "Security Auditor",
        "role": "Kiểm toán Bảo mật, Quyền hạn & Credential",
        "room_id": "api-control-room",
        "avatar_color": "#f43f5e",
        "tile_x": 6.35,
        "tile_y": 6.45,
        "system_prompt": (
            "Bạn là Security Auditor. Rà soát nhật ký kiểm toán, chính sách che khóa API, phân quyền RBAC "
            "và tuân thủ an toàn vận hành."
        ),
        "allowed_tools": ["inspect_audit_logs", "verify_secret_masking", "policy_check"],
        "permissions": ["read:audit_logs", "write:security_report"],
        "token_limit": 20000,
        "cost_limit_usd": 1.0,
        "max_loops": 3,
    },
    {
        "id": "zalo-system-guardian",
        "name": "Zalo AI Guardian",
        "role": "Trợ lý Quản trị Hệ thống & Liên lạc Zalo 24/7",
        "room_id": "command-center",
        "avatar_color": "#0068ff",
        "tile_x": 3.15,
        "tile_y": 1.95,
        "system_prompt": (
            "Bạn là Trợ lý AI Quản lý Hệ thống 24/7 của chủ sở hữu qua kênh Zalo. "
            "Giám sát sức khỏe máy chủ, Cloudflare Tunnel, tác vụ AI, chống Sleep, tự động phục hồi lỗi "
            "và báo cáo trung thực 100% không bịa số liệu."
        ),
        "allowed_tools": ["system_diagnostics", "tunnel_heal", "zalo_notify", "task_control"],
        "permissions": ["read:company", "control:guardian", "notify:zalo"],
        "token_limit": 32000,
        "cost_limit_usd": 1.0,
        "max_loops": 4,
    },
    {
        "id": "legal-compliance-officer",
        "name": "Trưởng ban Pháp chế & Tuân thủ",
        "role": "Thẩm định Giấy phép ICP, TMĐT Bộ Công Thương & Tuân thủ Luật Số",
        "room_id": "compliance-room",
        "avatar_color": "#f43f5e",
        "tile_x": 5.45,
        "tile_y": 10.05,
        "system_prompt": (
            "Bạn là Trưởng ban Pháp chế & Tuân thủ (Chief Legal & Compliance Officer). "
            "Rà soát điều kiện pháp lý toàn diện: Giấy phép trang thông tin điện tử tổng hợp (Nghị định 147/2024/NĐ-CP), "
            "thông báo website thương mại điện tử với Bộ Công Thương (Nghị định 52/2013/NĐ-CP & 85/2021/NĐ-CP), "
            "và cổng thanh toán VietQR. Bạn có quyền Veto (phủ quyết) mọi dự án chưa đạt chuẩn an toàn pháp lý."
        ),
        "allowed_tools": ["audit_icp_license", "verify_ecommerce_registration", "veto_lifecycle_stage", "issue_compliance_certificate"],
        "permissions": ["read:compliance", "write:legal_audit", "veto:lifecycle_gate"],
        "token_limit": 32000,
        "cost_limit_usd": 2.0,
        "max_loops": 4,
    },
    {
        "id": "ip-rights-guardian",
        "name": "Chuyên viên Sở hữu Trí tuệ & Bản quyền",
        "role": "Kiểm định Bản quyền Tác giả & Hợp đồng Hình ảnh Người mẫu",
        "room_id": "compliance-room",
        "avatar_color": "#a855f7",
        "tile_x": 4.85,
        "tile_y": 11.25,
        "system_prompt": (
            "Bạn là Chuyên viên Sở hữu Trí tuệ & Bản quyền (IP & Rights Guardian). "
            "Giám sát nghiêm ngặt việc tuân thủ Luật Sở hữu trí tuệ 2005 (sửa đổi 2022) và Điều 32 Bộ luật Dân sự 2015 "
            "(Quyền của cá nhân đối với hình ảnh). Bắt buộc 100% hình ảnh người mẫu thương mại phải có Model Release "
            "hoặc là hình ảnh AI tổng hợp độc quyền; cấm sao chép nội dung báo chí trái phép."
        ),
        "allowed_tools": ["audit_model_release", "check_copyright_infringement", "scan_trademark_risk"],
        "permissions": ["read:media_assets", "write:ip_clearance", "audit:image_rights"],
        "token_limit": 28000,
        "cost_limit_usd": 1.5,
        "max_loops": 4,
    },
    {
        "id": "tax-privacy-auditor",
        "name": "Chuyên viên Thuế & Bảo vệ Dữ liệu Cá nhân",
        "role": "Quản trị Thuế Kinh doanh Số & Bảo vệ Dữ liệu NĐ 13/2023",
        "room_id": "compliance-room",
        "avatar_color": "#06b6d4",
        "tile_x": 6.05,
        "tile_y": 11.25,
        "system_prompt": (
            "Bạn là Chuyên viên Thuế & Bảo vệ Dữ liệu (Tax & Data Privacy Auditor). "
            "Giám sát việc kê khai thuế thu nhập cá nhân / kinh doanh trực tuyến theo Thông tư 40/2021/TT-BTC khi phát sinh dòng tiền, "
            "và tuân thủ Nghị định 13/2023/NĐ-CP về Bảo vệ Dữ liệu Cá nhân (chính sách bảo mật, thu thập consent độc giả)."
        ),
        "allowed_tools": ["audit_tax_threshold", "review_privacy_policy", "check_data_consent"],
        "permissions": ["read:finance", "write:tax_report", "audit:data_privacy"],
        "token_limit": 24000,
        "cost_limit_usd": 1.5,
        "max_loops": 3,
    },
]


def set_agent_status(db: Session, agent: Agent, new_status: str, current_task_id: str | None = None) -> None:
    agent.status = new_status
    agent.current_task_id = current_task_id
    db.flush()
    event_bus.publish(
        db,
        event_type=EventType.AGENT_STATUS_CHANGED.value,
        entity_id=agent.id,
        agent_id=agent.id,
        task_id=current_task_id,
        payload={
            "agent_id": agent.id,
            "name": agent.name,
            "status": new_status,
            "current_task_id": current_task_id,
            "room_id": agent.room_id,
        },
    )


async def chat_with_agent(
    db: Session,
    *,
    agent: Agent,
    user_id: str | None,
    user_message: str,
    conversation_id: str | None = None,
    router: ModelRouter | None = None,
) -> tuple[Conversation, Message, Message]:
    """
    Persists user message in a durable Conversation, retrieves ONLY concise relevant context
    (last 6 messages + agent memory summary), invokes ModelRouter with fallback, and persists reply.
    """
    router = router or ModelRouter()
    conv: Conversation | None = None
    if conversation_id:
        conv = db.scalar(
            select(Conversation).where(Conversation.id == conversation_id, Conversation.agent_id == agent.id)
        )
    if not conv:
        conv = Conversation(
            agent_id=agent.id,
            user_id=user_id,
            title=user_message[:80],
            context_summary=f"Hội thoại điều hành với {agent.name} ({agent.role})",
        )
        db.add(conv)
        db.flush()

    user_msg = Message(
        conversation_id=conv.id,
        sender_type="USER",
        content=user_message,
        data_origin=DataOrigin.SOURCE.value,
        is_demo=False,
    )
    db.add(user_msg)
    db.flush()

    # Retrieve strictly bounded recent context instead of dumping unbounded conversation history
    recent_msgs = db.scalars(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc())
        .limit(6)
    ).all()
    recent_chronological = list(reversed(recent_msgs))
    compact_history = "\n".join(f"[{m.sender_type}]: {m.content[:300]}" for m in recent_chronological)

    active_task_info = "Không có task đang chạy."
    if agent.current_task_id:
        t = db.scalar(select(Task).where(Task.id == agent.current_task_id))
        if t:
            active_task_info = f"Task hiện tại: {t.title} ({t.status}, {t.progress_pct}%)."

    prompt = (
        f"Ngữ cảnh tóm tắt của Agent: {agent.memory_summary or 'Sẵn sàng nhận lệnh.'}\n"
        f"{active_task_info}\n"
        f"Quyền hạn được cấp: {', '.join(agent.permissions)}\n"
        f"Lịch sử gần nhất:\n{compact_history}\n\n"
        f"Hãy phản hồi trực tiếp, rõ ràng bằng tiếng Việt cho yêu cầu mới nhất của người vận hành."
    )

    prev_status = agent.status
    set_agent_status(db, agent, AgentStatus.WORKING.value, agent.current_task_id)
    try:
        resp, _ = await router.execute_with_fallback(
            db,
            request=ProviderRequest(
                system_prompt=agent.system_prompt,
                user_prompt=prompt,
                model_id=agent.preferred_model_id or "auto",
                max_tokens=min(800, agent.token_limit),
                metadata={"step_name": "agent_dialogue", "agent_role": agent.name},
            ),
            agent_id=agent.id,
            agent_name=agent.name,
            preferred_provider_id=agent.preferred_provider_id,
            preferred_model_id=agent.preferred_model_id,
        )
        reply = Message(
            conversation_id=conv.id,
            sender_type="AGENT",
            content=resp.content,
            provider_id=resp.provider_id,
            model_id=resp.model_id,
            tokens_used=resp.input_tokens + resp.output_tokens,
            data_origin=DataOrigin.AI_GENERATED.value,
            is_demo=resp.is_demo,
        )
        db.add(reply)
        agent.memory_summary = f"Trao đổi gần nhất: {user_message[:90]} -> Đã phản hồi qua {resp.model_id}."
        set_agent_status(
            db,
            agent,
            prev_status if prev_status == AgentStatus.WORKING.value and agent.current_task_id else AgentStatus.IDLE.value,
            agent.current_task_id,
        )
        db.commit()
        return conv, user_msg, reply
    except NoAvailableModelError as exc:
        sys_reply = Message(
            conversation_id=conv.id,
            sender_type="SYSTEM",
            content=f"[TẠM DỪNG DO HẾT NGUỒN MODEL]: {exc.reason}",
            data_origin=DataOrigin.SOURCE.value,
            is_demo=False,
        )
        db.add(sys_reply)
        set_agent_status(db, agent, AgentStatus.WAITING.value, agent.current_task_id)
        db.commit()
        return conv, user_msg, sys_reply
