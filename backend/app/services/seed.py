import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.registry import INITIAL_AGENTS, set_agent_status
from app.core.config import get_settings
from app.events.bus import emit_notification, event_bus
from app.models.entities import (
    AIModel,
    Agent,
    BusinessExperiment,
    Project,
    Provider,
    RevenueRecord,
    SystemSetting,
    Task,
    User,
)
from app.models.enums import (
    AgentStatus,
    DataOrigin,
    EventType,
    ModelTier,
    NotificationType,
    QuotaConfidence,
    QuotaStatus,
    TaskStatus,
    UserRole,
)
from app.security.auth import hash_password
from app.tasks.engine import TaskEngine


def seed_initial_data(db: Session) -> None:
    """Idempotent bootstrap of RBAC users, settings, 8 AI agents, real + sandbox providers, and transparent demo state."""
    cfg = get_settings()

    # 1. System settings singleton
    settings_row = db.scalar(select(SystemSetting).where(SystemSetting.id == "singleton"))
    if not settings_row:
        settings_row = SystemSetting(
            id="singleton",
            routing_mode=cfg.DEFAULT_ROUTING_MODE,
            provider_priority=[
                "google-gemini",
                "groq-cloud",
                "openrouter-free",
                "demo-free-alpha",
                "demo-free-beta",
                "demo-paid-pro",
            ],
            allow_paid_fallback=True,
            daily_budget_usd=cfg.DAILY_BUDGET_LIMIT_USD,
            monthly_budget_usd=cfg.MONTHLY_BUDGET_LIMIT_USD,
            approval_threshold_usd=cfg.APPROVAL_COST_THRESHOLD_USD,
            automation_level=cfg.DEFAULT_AUTOMATION_LEVEL,
            demo_mode=cfg.DEMO_MODE,
        )
        db.add(settings_row)

    # 2. Bootstrap Users (including owner Google identity danhnpcd@gmail.com + RBAC test accounts)
    bootstrap_users = [
        (cfg.PRIMARY_OPERATOR_EMAIL, cfg.BOOTSTRAP_ADMIN_PASSWORD, "Danh Nguyen (danhnpcd@gmail.com)", UserRole.ADMIN.value),
        (cfg.BOOTSTRAP_ADMIN_EMAIL, cfg.BOOTSTRAP_ADMIN_PASSWORD, "Chief Operator (Admin)", UserRole.ADMIN.value),
        (cfg.BOOTSTRAP_OPERATOR_EMAIL, cfg.BOOTSTRAP_OPERATOR_PASSWORD, "Shift Operator", UserRole.OPERATOR.value),
        (cfg.BOOTSTRAP_VIEWER_EMAIL, cfg.BOOTSTRAP_VIEWER_PASSWORD, "Observer (Read-Only)", UserRole.VIEWER.value),
    ]
    for email, pwd, name, role in bootstrap_users:
        if email and not db.scalar(select(User).where(User.email == email.lower())):
            db.add(
                User(
                    email=email.lower(),
                    display_name=name,
                    password_hash=hash_password(pwd),
                    role=role,
                    auth_method="GOOGLE_ACCOUNT_OWNER" if email.endswith("@gmail.com") else "LOCAL_PASSWORD",
                )
            )

    # 3. 8 Specialized AI Agents
    for ag_spec in INITIAL_AGENTS:
        if not db.scalar(select(Agent).where(Agent.id == ag_spec["id"])):
            db.add(Agent(**ag_spec))

    # 4. Real Providers (require real API credential) + Demo Sandbox Providers (clearly marked is_demo=True)
    providers_seed = [
        Provider(
            id="google-gemini",
            name="Google Gemini API (Real)",
            provider_type="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            enabled=True,
            priority=1,
            health_status="MISSING_CREDENTIAL",
            requires_credential=True,
            is_demo=False,
        ),
        Provider(
            id="groq-cloud",
            name="Groq OpenAI-Compatible (Real)",
            provider_type="openai_compatible",
            base_url="https://api.groq.com/openai/v1",
            enabled=True,
            priority=2,
            health_status="MISSING_CREDENTIAL",
            requires_credential=True,
            is_demo=False,
        ),
        Provider(
            id="openrouter-free",
            name="OpenRouter Free Hub (Real)",
            provider_type="openai_compatible",
            base_url="https://openrouter.ai/api/v1",
            enabled=True,
            priority=3,
            health_status="MISSING_CREDENTIAL",
            requires_credential=True,
            is_demo=False,
        ),
        Provider(
            id="demo-free-alpha",
            name="Sandbox Free Provider A (Demo)",
            provider_type="mock",
            base_url="mock://alpha",
            enabled=True,
            priority=10,
            health_status="HEALTHY",
            requires_credential=False,
            is_demo=True,
        ),
        Provider(
            id="demo-free-beta",
            name="Sandbox Free Provider B (Demo Backup)",
            provider_type="mock",
            base_url="mock://beta",
            enabled=True,
            priority=11,
            health_status="HEALTHY",
            requires_credential=False,
            is_demo=True,
        ),
        Provider(
            id="demo-paid-pro",
            name="Sandbox Paid Reserve C (Demo Paid)",
            provider_type="mock",
            base_url="mock://paid",
            enabled=True,
            priority=20,
            health_status="HEALTHY",
            requires_credential=False,
            is_demo=True,
        ),
    ]
    for p in providers_seed:
        if not db.scalar(select(Provider).where(Provider.id == p.id)):
            db.add(p)
    db.flush()

    models_seed = [
        AIModel(
            id="google-gemini:gemini-2.5-flash",
            provider_id="google-gemini",
            model_id="gemini-2.5-flash",
            display_name="Gemini 2.5 Flash (Free Tier Eligible)",
            supported_tasks=["general", "research", "coding", "marketing", "finance"],
            supports_tool_calling=True,
            supports_json_mode=True,
            supports_vision=True,
            context_window=1000000,
            input_cost_per_1k=0.0,
            output_cost_per_1k=0.0,
            tier=ModelTier.FREE.value,
            rate_limit_rpm=15,
            quality_score=92,
            quota_status=QuotaStatus.UNKNOWN.value,
            quota_confidence=QuotaConfidence.UNKNOWN.value,
        ),
        AIModel(
            id="groq-cloud:llama-3.3-70b-versatile",
            provider_id="groq-cloud",
            model_id="llama-3.3-70b-versatile",
            display_name="Llama 3.3 70B Versatile (Groq Free Tier)",
            supported_tasks=["general", "research", "coding", "marketing"],
            supports_tool_calling=True,
            supports_json_mode=True,
            supports_vision=False,
            context_window=128000,
            input_cost_per_1k=0.0,
            output_cost_per_1k=0.0,
            tier=ModelTier.FREE.value,
            rate_limit_rpm=30,
            quality_score=89,
            quota_status=QuotaStatus.UNKNOWN.value,
            quota_confidence=QuotaConfidence.UNKNOWN.value,
        ),
        AIModel(
            id="openrouter-free:deepseek/deepseek-chat:free",
            provider_id="openrouter-free",
            model_id="deepseek/deepseek-chat:free",
            display_name="DeepSeek V3 (:free via OpenRouter)",
            supported_tasks=["general", "research", "coding"],
            supports_tool_calling=False,
            supports_json_mode=True,
            supports_vision=False,
            context_window=64000,
            input_cost_per_1k=0.0,
            output_cost_per_1k=0.0,
            tier=ModelTier.FREE.value,
            rate_limit_rpm=20,
            quality_score=88,
            quota_status=QuotaStatus.UNKNOWN.value,
            quota_confidence=QuotaConfidence.UNKNOWN.value,
        ),
        AIModel(
            id="demo-free-alpha:alpha-flash-free",
            provider_id="demo-free-alpha",
            model_id="alpha-flash-free",
            display_name="Alpha Flash Free v1 [DEMO]",
            supported_tasks=["general", "research", "coding", "marketing", "finance", "qa"],
            supports_tool_calling=True,
            supports_json_mode=True,
            supports_vision=True,
            context_window=64000,
            input_cost_per_1k=0.0,
            output_cost_per_1k=0.0,
            tier=ModelTier.FREE.value,
            rate_limit_rpm=60,
            quality_score=84,
            quota_status=QuotaStatus.AVAILABLE.value,
            quota_confidence=QuotaConfidence.ESTIMATED.value,
            quota_remaining=50,
            mock_behavior="NORMAL",
        ),
        AIModel(
            id="demo-free-beta:beta-coder-free",
            provider_id="demo-free-beta",
            model_id="beta-coder-free",
            display_name="Beta Coder Free v1 [DEMO BACKUP]",
            supported_tasks=["general", "research", "coding", "marketing", "finance", "qa"],
            supports_tool_calling=True,
            supports_json_mode=True,
            supports_vision=False,
            context_window=32000,
            input_cost_per_1k=0.0,
            output_cost_per_1k=0.0,
            tier=ModelTier.FREE.value,
            rate_limit_rpm=60,
            quality_score=81,
            quota_status=QuotaStatus.AVAILABLE.value,
            quota_confidence=QuotaConfidence.ESTIMATED.value,
            quota_remaining=50,
            mock_behavior="NORMAL",
        ),
        AIModel(
            id="demo-paid-pro:omega-pro-paid",
            provider_id="demo-paid-pro",
            model_id="omega-pro-paid",
            display_name="Omega Pro Paid Reserve [DEMO PAID]",
            supported_tasks=["general", "research", "coding", "marketing", "finance", "qa"],
            supports_tool_calling=True,
            supports_json_mode=True,
            supports_vision=True,
            context_window=128000,
            input_cost_per_1k=0.01,
            output_cost_per_1k=0.03,
            tier=ModelTier.PAID.value,
            rate_limit_rpm=120,
            quality_score=96,
            quota_status=QuotaStatus.AVAILABLE.value,
            quota_confidence=QuotaConfidence.ESTIMATED.value,
            quota_remaining=500,
            mock_behavior="NORMAL",
        ),
    ]
    for m in models_seed:
        if not db.scalar(select(AIModel).where(AIModel.id == m.id)):
            db.add(m)

    # 5. Transparent Demo Business Experiment & Test Payment Record (explicitly is_demo=True & is_test_payment=True)
    if not db.scalar(select(Project).where(Project.slug == "ai-invoice-extractor-sandbox")):
        proj = Project(
            name="AI Invoice Extractor Micro-SaaS [DEMO SANDBOX]",
            slug="ai-invoice-extractor-sandbox",
            stage="SANDBOX_READY",
            status="ACTIVE",
            summary="Công cụ bóc tách hóa đơn PDF sang CSV cho hộ kinh doanh nhỏ (bản thử nghiệm Sandbox).",
            opportunity_score=82.5,
            qa_passed=True,
            data_origin=DataOrigin.AI_GENERATED.value,
            is_demo=True,
            sandbox_html=(
                "<!doctype html><html lang='vi'><head><meta charset='utf-8'/>"
                "<title>AI Invoice Extractor — Sandbox Preview</title>"
                "<style>body{font-family:system-ui;background:#0f172a;color:#f8fafc;padding:2rem}"
                ".card{max-width:640px;margin:auto;background:#1e293b;padding:2rem;border-radius:1rem;border:1px solid #38bdf8}"
                ".badge{background:#f59e0b;color:#0f172a;padding:4px 10px;border-radius:999px;font-weight:700;font-size:12px}</style>"
                "</head><body><div class='card'><span class='badge'>SANDBOX DEMO PREVIEW</span>"
                "<h1>AI Invoice Extractor</h1><p>Chuyển hóa đơn PDF sang bảng kế toán chuẩn trong 5 giây.</p>"
                "</div></body></html>"
            ),
        )
        db.add(proj)
        db.flush()

        db.add(
            BusinessExperiment(
                project_id=proj.id,
                title="Bóc tách hóa đơn tự động cho SME [DEMO]",
                hypothesis="SME sẵn sàng trả $9/tháng nếu tiết kiệm 4 giờ nhập liệu mỗi tuần.",
                market_segment="Kế toán hộ kinh doanh & SME",
                opportunity_score=82.5,
                validation_status="VALIDATED",
                visitors=120,
                signups=18,
                paying_customers=0,
                data_origin=DataOrigin.AI_GENERATED.value,
                is_demo=True,
            )
        )
        db.add(
            RevenueRecord(
                project_id=proj.id,
                description="Giao dịch cổng thanh toán Sandbox thử nghiệm (KHÔNG tính vào doanh thu thực)",
                gross_amount_usd=19.0,
                net_received_usd=0.0,
                refund_amount_usd=0.0,
                is_test_payment=True,
                is_demo=True,
                verified=False,
                data_origin=DataOrigin.SOURCE.value,
            )
        )

    # 6. Safely import any API keys configured in .env into CredentialVault (idempotent, masked)
    from app.models.entities import Credential
    from app.security.vault import get_vault

    vault = get_vault()
    env_key_pairs = [
        ("google-gemini", cfg.GEMINI_API_KEY or cfg.GOOGLE_API_KEY, "Gemini Key (.env)"),
        ("groq-cloud", cfg.GROQ_API_KEY, "Groq Key (.env)"),
        ("openrouter-free", cfg.OPENROUTER_API_KEY, "OpenRouter Key (.env)"),
    ]
    for prov_id, raw_secret, label in env_key_pairs:
        clean_secret = (raw_secret or "").strip()
        if not clean_secret:
            continue
        masked = vault.mask_secret(clean_secret)
        existing_cred = db.scalar(
            select(Credential).where(Credential.provider_id == prov_id, Credential.masked_preview == masked)
        )
        if not existing_cred:
            encrypted, _ = vault.encrypt(clean_secret)
            db.add(
                Credential(
                    provider_id=prov_id,
                    label=label,
                    auth_type="API_KEY",
                    encrypted_secret=encrypted,
                    masked_preview=masked,
                    enabled=True,
                    priority=1,
                    auth_status="CONFIGURED",
                )
            )
            prov = db.scalar(select(Provider).where(Provider.id == prov_id))
            if prov:
                prov.enabled = True
                if prov.health_status == "MISSING_CREDENTIAL":
                    prov.health_status = "CONFIGURED"

    db.commit()


def recover_interrupted_tasks_on_startup(db: Session) -> int:
    """
    Crash recovery: transitions any tasks left in RUNNING state during an unclean shutdown
    into PAUSED while preserving their checkpoints so operators can safely resume them.
    """
    interrupted = db.scalars(select(Task).where(Task.status == TaskStatus.RUNNING.value)).all()
    count = 0
    for task in interrupted:
        task.status = TaskStatus.PAUSED.value
        agent = db.scalar(select(Agent).where(Agent.id == task.agent_id))
        if agent:
            set_agent_status(db, agent, AgentStatus.PAUSED.value, task.id)
        event_bus.publish(
            db,
            event_type=EventType.TASK_PAUSED.value,
            entity_id=task.id,
            task_id=task.id,
            agent_id=task.agent_id,
            payload={
                "task_id": task.id,
                "reason": "Recovered cleanly after backend restart; ready to resume from checkpoint.",
                "completed_steps": task.completed_steps,
            },
        )
        count += 1
    if count:
        db.commit()
    return count


async def run_business_mvp_pipeline(db: Session, niche_topic: str) -> dict[str, Any]:
    """
    Milestone 5 — Bounded Multi-Agent Business MVP Pipeline:
    1. Business Researcher -> Market Opportunity Scan
    2. Business Evaluator -> Opportunity Scoring
    3. Product Builder -> Sandbox Landing Page Artifact
    4. QA Engineer -> Automated Verification
    5. Marketing Agent -> Ethical Launch Copy
    """
    engine = TaskEngine()
    slug_base = re.sub(r"[^a-z0-9]+", "-", niche_topic.lower()).strip("-")[:40] or "digital-product"
    slug = f"{slug_base}-{int(datetime.now(timezone.utc).timestamp()) % 10000}"

    project = Project(
        name=f"MVP: {niche_topic[:80]}",
        slug=slug,
        stage="RESEARCH",
        status="ACTIVE",
        summary=f"Chu trình tự động hóa có kiểm soát cho ý tưởng: {niche_topic}",
        data_origin=DataOrigin.AI_GENERATED.value,
        is_demo=bool(engine._get_settings(db).demo_mode),
    )
    db.add(project)
    db.flush()

    pipeline_specs = [
        (
            "business-researcher",
            f"[Nghiên cứu] {niche_topic[:60]}",
            f"Phân tích khách hàng mục tiêu, nhu cầu thực tế và đối thủ cho: {niche_topic}",
            ["Thu thập vấn đề thị trường", "Xác định phân khúc khách hàng"],
        ),
        (
            "business-evaluator",
            f"[Thẩm định] {niche_topic[:60]}",
            f"Đánh giá chi phí vận hành AI, rủi ro và chấm điểm cơ hội cho: {niche_topic}",
            ["Phân tích Unit Economics", "Chốt điểm đánh giá cơ hội"],
        ),
        (
            "product-builder",
            f"[Xây dựng Sandbox] {niche_topic[:60]}",
            f"Thiết kế và tạo Landing Page sản phẩm số tối thiểu trong Sandbox cho: {niche_topic}",
            ["Thiết kế cấu trúc sản phẩm", "Sinh mã HTML Sandbox Preview"],
        ),
        (
            "qa-engineer",
            f"[Kiểm thử QA] {niche_topic[:60]}",
            f"Kiểm tra cấu trúc HTML, tính rõ ràng thông điệp và an toàn dữ liệu cho: {niche_topic}",
            ["Kiểm thử giao diện & nội dung", "Xác nhận đạt chuẩn Sandbox"],
        ),
        (
            "marketing-agent",
            f"[Tiếp thị] {niche_topic[:60]}",
            f"Soạn kế hoạch tiếp thị nội dung minh bạch, hợp pháp cho: {niche_topic}",
            ["Soạn thông điệp giá trị", "Đề xuất kênh phân phối tự nhiên"],
        ),
    ]

    executed_tasks: list[Task] = []
    for depth_idx, (ag_id, title, desc, steps) in enumerate(pipeline_specs):
        t = engine.create_task(
            db,
            title=title,
            description=desc,
            agent_id=ag_id,
            priority=3,
            task_type="general",
            planned_steps=steps,
            project_id=project.id,
            depth=min(depth_idx, 3),
        )
        t = await engine.execute_task(db, t.id)
        executed_tasks.append(t)
        if t.status != TaskStatus.COMPLETED.value:
            break

    all_done = all(t.status == TaskStatus.COMPLETED.value for t in executed_tasks)
    score = 84.0 if all_done else 60.0
    project.opportunity_score = score
    project.qa_passed = all_done
    project.stage = "SANDBOX_READY" if all_done else "DEVELOPMENT"
    project.sandbox_html = (
        f"<!doctype html><html lang='vi'><head><meta charset='utf-8'/>"
        f"<title>{project.name} — Sandbox</title>"
        "<style>body{margin:0;font-family:Inter,system-ui,sans-serif;background:#090d16;color:#e2e8f0;padding:40px}"
        ".wrap{max-width:760px;margin:0 auto;background:#111827;border:1px solid #22d3ee;border-radius:16px;padding:32px}"
        ".pill{display:inline-block;background:#06b6d4;color:#082f49;font-weight:700;padding:4px 12px;border-radius:999px;font-size:12px}"
        "h1{color:#f8fafc;margin-top:16px} .cta{margin-top:24px;display:inline-block;background:#10b981;color:#052e16;"
        "font-weight:700;padding:12px 20px;border-radius:10px;text-decoration:none}</style></head><body>"
        f"<div class='wrap'><span class='pill'>SANDBOX ENVIRONMENT • QA {'PASSED' if all_done else 'PENDING'}</span>"
        f"<h1>{niche_topic}</h1>"
        f"<p>{executed_tasks[-1].result_summary if executed_tasks else ''}</p>"
        f"<p><strong>Điểm đánh giá cơ hội:</strong> {score}/100</p>"
        "<a class='cta' href='#sandbox'>Đăng ký dùng thử (Chế độ Thử nghiệm)</a></div></body></html>"
    )

    exp = BusinessExperiment(
        project_id=project.id,
        title=f"Thử nghiệm MVP: {niche_topic[:100]}",
        hypothesis=f"Giải pháp số '{niche_topic[:80]}' giải quyết nhu cầu tự động hóa với chi phí API thấp.",
        market_segment="Khách hàng B2B / Solopreneur",
        opportunity_score=score,
        validation_status="VALIDATED" if all_done else "RESEARCHED",
        visitors=0,
        signups=0,
        paying_customers=0,
        data_origin=DataOrigin.AI_GENERATED.value,
        is_demo=project.is_demo,
    )
    db.add(exp)
    emit_notification(
        db,
        notif_type=NotificationType.SUCCESS.value if all_done else NotificationType.WARNING.value,
        title=f"Chu trình Business MVP: {project.name}",
        message=f"Đã thực thi {len(executed_tasks)}/5 tác vụ chuyên môn. Trạng thái: {project.stage}.",
    )
    db.commit()

    return {
        "project_id": project.id,
        "slug": project.slug,
        "stage": project.stage,
        "qa_passed": project.qa_passed,
        "opportunity_score": project.opportunity_score,
        "preview_url": f"/api/workflows/sandbox-preview/{project.slug}",
        "tasks": [{"id": t.id, "agent_id": t.agent_id, "status": t.status, "progress_pct": t.progress_pct} for t in executed_tasks],
    }
