"""
Legal & Compliance API Routes for AI Business Simulator.
Governed by the Legal AI Team:
- legal-compliance-officer (Trưởng ban Pháp chế & Tuân thủ)
- ip-rights-guardian (Chuyên viên Bản quyền & SHTT)
- tax-privacy-auditor (Chuyên viên Thuế & Dữ liệu Cá nhân)
"""

from datetime import datetime, timezone
from typing import Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.entities import Agent, Project, Task, User
from app.models.enums import TaskStatus
from app.security.auth import get_current_user, require_operator

compliance_router = APIRouter(prefix="/api/compliance", tags=["Legal & Compliance"])


class LegalAuditRequest(BaseModel):
    project_id: str
    focus_regulations: List[str] = [
        "ND_147_2024_ICP",
        "DIEU_32_BLDS_2015_MODEL_RIGHTS",
        "ND_52_2013_TMDT",
        "TT_40_2021_THUE_ONLINE",
        "ND_13_2023_DATA_PRIVACY",
    ]


class LegalClearanceRequest(BaseModel):
    project_id: str
    action: str  # "PAUSE", "RESUME_SAFE_MODE", "APPROVE_CLEARANCE"
    reason: str


@compliance_router.get("/overview")
def get_compliance_overview(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Returns the legal compliance status of all projects and legal team agents."""
    agents = db.scalars(
        select(Agent).where(Agent.room_id == "compliance-room")
    ).all()

    projects = db.scalars(select(Project)).all()
    project_status_list = []
    for p in projects:
        risk_level = "LOW"
        risk_notes = []
        is_paused = (p.status == "PAUSED_LEGAL_AUDIT" or p.stage == "PAUSED")

        slug = (p.slug or "").lower()
        if "news" in slug or "aipulse" in slug:
            risk_level = "CRITICAL" if not is_paused else "CONTROLLED_PAUSED"
            risk_notes.append("Nghị định 147/2024/NĐ-CP: Bắt buộc Giấy phép ICP trang tin tổng hợp nếu cào báo chí.")
            risk_notes.append("Luật SHTT 2022: Cần văn bản thỏa thuận bản quyền với các cơ quan báo chí.")
        elif "tiktok" in slug or "fashion" in slug:
            risk_level = "HIGH" if not is_paused else "CONTROLLED_PAUSED"
            risk_notes.append("Điều 32 BLDS 2015: Cấm dùng ảnh người mẫu thật khi chưa có Model Release.")
            risk_notes.append("Quy chế TikTok Shop: Bắt buộc tuân thủ Creator Marketplace & gắn nhãn Paid Partnership.")
        elif "invoice" in slug:
            risk_level = "LOW"
            risk_notes.append("Dữ liệu tài chính: Cần bảo mật theo NĐ 13/2023 về dữ liệu cá nhân.")

        project_status_list.append({
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "status": p.status,
            "stage": p.stage,
            "is_paused_for_legal": is_paused,
            "risk_level": risk_level,
            "risk_notes": risk_notes,
            "summary": p.summary,
        })

    tasks = db.scalars(
        select(Task).where(
            Task.agent_id.in_([
                "legal-compliance-officer",
                "ip-rights-guardian",
                "tax-privacy-auditor",
            ])
        )
    ).all()

    return {
        "status": "HEALTHY",
        "compliance_gate_enforced": True,
        "legal_team": [
            {
                "id": a.id,
                "name": a.name,
                "role": a.role,
                "room_id": a.room_id,
                "status": a.status,
                "avatar_color": a.avatar_color,
            }
            for a in agents
        ],
        "projects_compliance": project_status_list,
        "active_compliance_tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "agent_id": t.agent_id,
                "priority": t.priority,
                "result_summary": t.result_summary,
            }
            for t in tasks
        ],
        "vietnam_legal_regulations": [
            {
                "code": "ND_147_2024_ND_CP",
                "name": "Nghị định 147/2024/NĐ-CP về Quản lý, Cung cấp và Sử dụng Internet & Thông tin trên Mạng",
                "scope": "Cấm vận hành trang thông tin điện tử tổng hợp không phép; mức phạt 20 - 30 triệu VNĐ.",
                "remedy": "Chuyển thành Blog công nghệ cá nhân hoặc nộp hồ sơ xin cấp phép ICP.",
            },
            {
                "code": "DIEU_32_BLDS_2015",
                "name": "Điều 32 Bộ luật Dân sự 2015 về Quyền của Cá nhân đối với Hình ảnh",
                "scope": "Cấm sử dụng hình ảnh người khác vào mục đích thương mại khi chưa có sự đồng ý.",
                "remedy": "Chuyển đổi 100% sang người mẫu ảo AI tổng hợp (Synthetic AI Faces), cấm dùng ảnh người thật.",
            },
            {
                "code": "ND_52_2013_ND_85_2021",
                "name": "Nghị định 52/2013/NĐ-CP & Nghị định 85/2021/NĐ-CP về Thương mại Điện tử",
                "scope": "Bắt buộc thông báo website bán hàng/dịch vụ số lên Cổng online.gov.vn của Bộ Công Thương.",
                "remedy": "Duy trì VietQR ở chế độ Sandbox nghiệm thu; nộp hồ sơ thông báo trước khi thu phí công khai.",
            },
            {
                "code": "TT_40_2021_TT_BTC",
                "name": "Thông tư 40/2021/TT-BTC & Luật Quản lý Thuế số 38/2019/QH14",
                "scope": "Nghĩa vụ kê khai thuế GTGT (5%) & TNCN (2%) đối với doanh thu kinh doanh số > 100 triệu/năm.",
                "remedy": "Tự động kết xuất sao kê doanh thu thực nhận để phục vụ quyết toán thuế đúng kỳ hạn.",
            },
            {
                "code": "ND_13_2023_ND_CP",
                "name": "Nghị định 13/2023/NĐ-CP về Bảo vệ Dữ liệu Cá nhân",
                "scope": "Quy định thu thập email, IP, số điện thoại phải có chính sách bảo mật và chấp thuận (Consent).",
                "remedy": "Bổ sung Điều khoản Bảo mật và Banner đồng ý xử lý dữ liệu trước khi thu thập lead magnet.",
            },
        ],
    }


@compliance_router.post("/audit-project")
def audit_project_compliance(
    req: LegalAuditRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Runs a simulated legal audit report for a specific project."""
    proj = db.scalar(select(Project).where(Project.id == req.project_id))
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    slug = (proj.slug or "").lower()
    findings = []
    action_items = []
    clearance_status = "HOLD"

    if "news" in slug or "aipulse" in slug:
        findings.append({
            "regulation": "Nghị định 147/2024/NĐ-CP",
            "violation_risk": "CỰC KỲ CAO",
            "detail": "Thu thập và phát hành tin tức từ các báo (VNExpress, Thanh Niên) không có Giấy phép ICP.",
            "penalty_range": "20.000.000 - 30.000.000 VNĐ",
        })
        findings.append({
            "regulation": "Luật Sở hữu trí tuệ 2005 (sửa đổi 2022) Điều 28",
            "violation_risk": "CAO",
            "detail": "Sao chép tác phẩm báo chí của bên thứ ba mà không có văn bản thỏa thuận bản quyền.",
            "penalty_range": "Bồi thường thiệt hại dân sự & gỡ bỏ nội dung",
        })
        action_items.append("Đình chỉ mọi cron crawl tự động từ các nguồn báo chí chính thống.")
        action_items.append("Chuyển định vị website thành 'Blog Cá Nhân Chia Sẻ Kiến Thức AI' (không thuộc diện cấp phép ICP).")

    elif "tiktok" in slug or "fashion" in slug:
        findings.append({
            "regulation": "Điều 32 Bộ luật Dân sự 2015",
            "violation_risk": "CỰC KỲ CAO",
            "detail": "Sử dụng hình ảnh người mẫu lấy trên mạng để thương mại hóa mà không có hợp đồng Model Release.",
            "penalty_range": "Bồi thường danh dự, nhân phẩm & xử phạt vi phạm hành chính NĐ 15/2020 (10-20 triệu VNĐ)",
        })
        action_items.append("Cấm tuyệt đối sử dụng ảnh người thật chưa ký thỏa thuận bản quyền.")
        action_items.append("Kích hoạt 100% người mẫu ảo AI (Diffusion Model) để sở hữu bản quyền độc quyền.")

    else:
        clearance_status = "PASSED"
        findings.append({
            "regulation": "Nghị định 52/2013/NĐ-CP & Nghị định 13/2023/NĐ-CP",
            "violation_risk": "THẤP",
            "detail": "Dự án phần mềm nội bộ, cần thông báo TMĐT khi gắn cổng thanh toán công khai.",
            "penalty_range": "0 VNĐ",
        })

    return {
        "project_id": proj.id,
        "project_name": proj.name,
        "compliance_status": clearance_status,
        "audited_by": "legal-compliance-officer",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "findings": findings,
        "mandatory_action_items": action_items,
    }


@compliance_router.post("/set-project-status")
def set_project_legal_status(
    req: LegalClearanceRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Updates legal clearance or suspension status of a project."""
    proj = db.scalar(select(Project).where(Project.id == req.project_id))
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")

    if req.action == "PAUSE":
        proj.status = "PAUSED_LEGAL_AUDIT"
        proj.stage = "PAUSED"
        proj.summary = f"[LEGAL HOLD] {req.reason}"
    elif req.action == "RESUME_SAFE_MODE":
        proj.status = "SAFE_MODE"
        proj.summary = f"[SAFE COMPLIANCE MODE] {req.reason}"
    elif req.action == "APPROVE_CLEARANCE":
        proj.status = "ACTIVE"
        proj.summary = f"[LEGAL CLEARANCE APPROVED] {req.reason}"

    db.commit()
    db.refresh(proj)
    return {
        "project_id": proj.id,
        "name": proj.name,
        "status": proj.status,
        "summary": proj.summary,
        "updated_by": user.email,
        "action_taken": req.action,
    }
