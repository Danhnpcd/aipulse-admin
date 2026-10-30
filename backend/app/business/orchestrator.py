import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.registry import set_agent_status
from app.business.monetization_and_economics import (
    calculate_unit_economics,
    ensure_product_catalog_for_artifact,
    generate_ethical_marketing_drafts,
    get_customer_validation_summary,
)
from app.business.research_and_scoring import (
    create_locked_experiment,
    research_and_rank_opportunities,
    serialize_opportunity,
)
from app.business.workspace_builder import (
    build_product_in_workspace,
    deploy_product_controlled,
    run_isolated_product_tests,
)
from app.core.config import get_settings
from app.events.bus import emit_notification, event_bus
from app.models.entities import (
    Agent,
    BusinessExperiment,
    BusinessOpportunity,
    CostRecord,
    DeploymentRecord,
    ExperimentSpec,
    MarketingCampaignItem,
    OrchestratorDecision,
    ProductArtifact,
    ProductCatalogItem,
    Project,
    SystemSetting,
    Task,
)
from app.models.enums import (
    AgentStatus,
    DataOrigin,
    EventType,
    NotificationType,
    ProjectLifecycleStage,
    TaskStatus,
)
from app.tasks.engine import TaskEngine

ORDERED_LIFECYCLE_STAGES: list[str] = [
    ProjectLifecycleStage.DISCOVERY.value,
    ProjectLifecycleStage.OPPORTUNITY_VALIDATION.value,
    ProjectLifecycleStage.PRODUCT_PLANNING.value,
    ProjectLifecycleStage.BUILDING.value,
    ProjectLifecycleStage.TESTING.value,
    ProjectLifecycleStage.READY_FOR_REVIEW.value,
    ProjectLifecycleStage.DEPLOYMENT.value,
    ProjectLifecycleStage.MARKETING.value,
    ProjectLifecycleStage.MONETIZATION.value,
    ProjectLifecycleStage.MEASUREMENT.value,
    ProjectLifecycleStage.OPTIMIZATION.value,
]

MAX_ORCHESTRATOR_STEPS_PER_RUN = 10


class AutonomousBusinessOrchestrator:
    """
    Central Autonomous Business Orchestrator (Section 3 & Section 16):
    - Governs the 15-state business project lifecycle with explicit gate checks.
    - Prevents unbounded planning loops (strictly capped steps, time, and budget).
    - Deduplicates tasks by deterministic idempotency keys.
    - Detects stalled tasks and recovers safely after restarts or model failovers.
    - Logs every decision in `orchestrator_decisions` with inputs, alternatives, rationale,
      estimated cost, success criteria, and post-execution outcome.
    """

    def __init__(self, task_engine: TaskEngine | None = None):
        self.engine = task_engine or TaskEngine()

    def _check_system_and_budget_gates(self, db: Session) -> tuple[bool, str, float]:
        settings_row = self.engine._get_settings(db)
        if settings_row.emergency_stop:
            return False, "EMERGENCY_STOP đang bật — dừng mọi chu kỳ tự kinh doanh.", 0.0
        if settings_row.system_paused:
            return False, "Hệ thống đang ở trạng thái TẠM DỪNG (system_paused=True).", 0.0

        spent_prod = float(
            db.scalar(
                select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(CostRecord.is_demo.is_(False))
            )
            or 0.0
        )
        remaining = round(max(0.0, float(settings_row.daily_budget_usd) - spent_prod), 4)
        if spent_prod >= float(settings_row.daily_budget_usd):
            emit_notification(
                db,
                notif_type=NotificationType.BUDGET_LIMIT_REACHED.value,
                title="Tạm dừng Orchestrator — Đạt trần ngân sách ngày",
                message=f"Chi phí thực (${spent_prod:.4f}) đã chạm ngưỡng ngân sách (${settings_row.daily_budget_usd:.2f}).",
            )
            return False, "BUDGET_EXCEEDED", 0.0
        return True, "OK", remaining

    def _check_legal_compliance_gate(self, db: Session, project: Project) -> tuple[bool, str]:
        """
        Legal & Compliance Gate Check (Team Pháp Lý AI):
        Thẩm định 5 trụ cột pháp luật Việt Nam:
        1. Nghị định 147/2024/NĐ-CP (Giấy phép ICP đối với trang tin điện tử tổng hợp).
        2. Điều 32 Bộ luật Dân sự 2015 (Quyền hình ảnh cá nhân / Model Release).
        3. Nghị định 52/2013/NĐ-CP & 85/2021/NĐ-CP (Đăng ký website TMĐT với Bộ Công Thương).
        4. Thông tư 40/2021/TT-BTC (Nghĩa vụ thuế kinh doanh số).
        5. Nghị định 13/2023/NĐ-CP (Bảo vệ dữ liệu cá nhân & Consent).
        """
        if project.status == "PAUSED_LEGAL_AUDIT":
            return False, "Dự án đang trong trạng thái TẠM DỪNG KIỂM TOÁN PHÁP LÝ (PAUSED_LEGAL_AUDIT). Cần chứng nhận của Team Pháp Lý trước khi tiếp tục."

        slug = (project.slug or "").lower()
        if "news" in slug or "aipulse" in slug:
            if project.status != "SAFE_MODE":
                return False, "Cổng Pháp Lý: Dự án tin tức chưa có Giấy phép Trang thông tin điện tử tổng hợp (ICP) theo Nghị định 147/2024/NĐ-CP. Cần chuyển sang SAFE_MODE (Blog cá nhân)."

        if "tiktok" in slug or "fashion" in slug:
            if project.status != "SAFE_MODE":
                return False, "Cổng Pháp Lý: Dự án video thời trang chưa hoàn tất kiểm toán bản quyền người mẫu (Điều 32 BLDS 2015). Cần chứng nhận 100% người mẫu ảo AI."

        return True, "LEGAL_CLEARANCE_PASSED"

    def _log_decision(
        self,
        db: Session,
        *,
        project_id: str,
        stage_from: str,
        stage_to: str,
        decision_type: str,
        inputs: dict[str, Any],
        options: list[str],
        rationale: str,
        estimated_cost_usd: float,
        success_criteria: str,
        outcome: str,
    ) -> OrchestratorDecision:
        cycle_cnt = (
            db.scalar(
                select(func.count(OrchestratorDecision.id)).where(OrchestratorDecision.project_id == project_id)
            )
            or 0
        ) + 1
        dec = OrchestratorDecision(
            project_id=project_id,
            stage_from=stage_from,
            stage_to=stage_to,
            decision_type=decision_type,
            cycle_index=int(cycle_cnt),
            input_summary_json=inputs,
            options_considered_json=options,
            rationale=rationale,
            estimated_cost_usd=estimated_cost_usd,
            success_criteria=success_criteria,
            execution_outcome=outcome,
        )
        db.add(dec)
        db.flush()
        event_bus.publish(
            db,
            event_type=EventType.BUSINESS_DECISION_LOGGED.value,
            entity_id=project_id,
            agent_id="ceo-orchestrator",
            payload={
                "decision_id": dec.id,
                "project_id": project_id,
                "stage_from": stage_from,
                "stage_to": stage_to,
                "decision_type": decision_type,
            },
        )
        return dec

    def _transition_stage(self, db: Session, project: Project, new_stage: str) -> str:
        prev = project.stage
        project.stage = new_stage
        db.flush()
        event_bus.publish(
            db,
            event_type=EventType.PROJECT_STAGE_CHANGED.value,
            entity_id=project.id,
            agent_id="ceo-orchestrator",
            payload={"project_id": project.id, "from": prev, "to": new_stage},
        )
        return prev

    def create_business_project(
        self,
        db: Session,
        *,
        name: str,
        focus_domain: str = "Tự động hóa đối soát hóa đơn & dữ liệu tài chính cho SME",
        opportunity_id: str | None = None,
    ) -> Project:
        ok, reason, _ = self._check_system_and_budget_gates(db)
        if not ok:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)

        import unicodedata
        normalized_name = unicodedata.normalize("NFKD", name).encode("ASCII", "ignore").decode("utf-8")
        slug_base = re.sub(r"[^a-z0-9]+", "-", normalized_name.lower()).strip("-")[:38] or "autonomous-venture"
        slug = f"{slug_base}-{int(datetime.now(timezone.utc).timestamp()) % 100000}"

        opp = (
            db.scalar(select(BusinessOpportunity).where(BusinessOpportunity.id == opportunity_id))
            if opportunity_id
            else None
        )
        initial_stage = (
            ProjectLifecycleStage.OPPORTUNITY_VALIDATION.value
            if opp
            else ProjectLifecycleStage.DISCOVERY.value
        )

        proj = Project(
            name=name,
            slug=slug,
            stage=initial_stage,
            status="ACTIVE",
            summary=focus_domain,
            opportunity_score=opp.total_score if opp else None,
            qa_passed=False,
            data_origin=DataOrigin.SOURCE.value,
            is_demo=False,
        )
        db.add(proj)
        db.flush()

        if opp:
            opp.project_id = proj.id
            opp.selection_status = "SELECTED_FOR_MVP"

        self._log_decision(
            db,
            project_id=proj.id,
            stage_from="NONE",
            stage_to=initial_stage,
            decision_type="CREATE_PROJECT",
            inputs={"name": name, "focus_domain": focus_domain, "opportunity_id": opportunity_id},
            options=["Tạo dự án từ cơ hội đã chấm điểm", "Khởi tạo từ pha DISCOVERY khảo sát 3 hướng"],
            rationale="Khởi tạo dự án tự kinh doanh đơn lẻ để tập trung nguồn lực và kiểm soát ngân sách chặt chẽ.",
            estimated_cost_usd=0.02,
            success_criteria="Đưa 01 sản phẩm số vượt qua pytest cô lập, lên staging và sẵn sàng đo lường khách hàng thật.",
            outcome=f"Project {proj.slug} initialized in {initial_stage}",
        )
        db.commit()
        db.refresh(proj)
        return proj

    async def advance_one_stage(self, db: Session, project: Project) -> dict[str, Any]:
        """Executes strictly ONE verified lifecycle transition with full gate checks and decision logging."""
        if project.status == "PAUSED" or project.stage == ProjectLifecycleStage.PAUSED.value:
            return {"project_id": project.id, "stage": project.stage, "halted": True, "reason": "PROJECT_PAUSED"}

        ok, gate_reason, remaining_budget = self._check_system_and_budget_gates(db)
        if not ok:
            project.status = "PAUSED"
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=project.stage,
                stage_to=ProjectLifecycleStage.PAUSED.value,
                decision_type="PAUSE_EXPERIMENT",
                inputs={"reason": gate_reason},
                options=["Tạm dừng bảo toàn trạng thái", "Tiếp tục vượt ngưỡng (Bị cấm)"],
                rationale=gate_reason,
                estimated_cost_usd=0.0,
                success_criteria="Đảm bảo không vượt ngân sách hoặc vi phạm lệnh dừng.",
                outcome="PAUSED_SAFELY",
            )
            project.stage = ProjectLifecycleStage.PAUSED.value
            db.commit()
            return {"project_id": project.id, "stage": project.stage, "halted": True, "reason": gate_reason}

        cur = project.stage

        # 1. DISCOVERY -> OPPORTUNITY_VALIDATION
        if cur in (ProjectLifecycleStage.DISCOVERY.value, "RESEARCH"):
            res = await research_and_rank_opportunities(
                db,
                focus_domain=project.summary or project.name,
                project_id=project.id,
                engine=self.engine,
            )
            sel = res["selected_opportunity"]
            project.opportunity_score = sel["total_score"]
            next_st = (
                ProjectLifecycleStage.OPPORTUNITY_VALIDATION.value
                if sel["total_score"] >= 60.0
                else ProjectLifecycleStage.PAUSED.value
            )
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="SELECT_SINGLE_MVP_OPPORTUNITY",
                inputs={"candidates_evaluated": len(res["all_candidates"]), "top_score": sel["total_score"]},
                options=[c["title"] for c in res["all_candidates"]],
                rationale=(
                    f"Chọn duy nhất '{sel['title']}' ({sel['total_score']}/100, độ tin cậy {sel['confidence_score']}) "
                    f"vì chi phí vận hành thấp, khả thi kỹ thuật cao và không lệ thuộc vào 1 API miễn phí."
                ),
                estimated_cost_usd=sel["estimated_dev_cost_usd"],
                success_criteria="Điểm cơ hội >= 60/100 và có kế hoạch kiểm chứng chi phí thấp.",
                outcome=f"Selected {sel['id']}",
            )
            db.commit()
            return {"stage": project.stage, "selected_opportunity": sel}

        # 2. OPPORTUNITY_VALIDATION -> PRODUCT_PLANNING
        if cur == ProjectLifecycleStage.OPPORTUNITY_VALIDATION.value:
            opp = db.scalar(
                select(BusinessOpportunity)
                .where(BusinessOpportunity.project_id == project.id)
                .order_by(BusinessOpportunity.total_score.desc())
                .limit(1)
            )
            if not opp:
                raise HTTPException(status_code=409, detail="Chưa có BusinessOpportunity để xác thực.")

            existing_exp = db.scalar(
                select(BusinessExperiment).where(
                    BusinessExperiment.project_id == project.id, BusinessExperiment.is_demo.is_(False)
                )
            )
            if not existing_exp:
                exp, spec = create_locked_experiment(
                    db,
                    project_id=project.id,
                    title=f"Thử nghiệm nhu cầu thực: {opp.title[:100]}",
                    hypothesis=opp.pricing_hypothesis,
                    market_segment=opp.target_customer,
                    opportunity_score=opp.total_score,
                    max_budget_usd=min(remaining_budget, 5.0),
                    is_demo=False,
                )
                exp_id = exp.id
                locked = spec.criteria_locked
            else:
                exp_id = existing_exp.id
                locked = True

            next_st = ProjectLifecycleStage.PRODUCT_PLANNING.value
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="LOCK_EXPERIMENT_CRITERIA",
                inputs={"opportunity_id": opp.id, "experiment_id": exp_id, "criteria_locked": locked},
                options=["Khóa tiêu chí thử nghiệm và lập kế hoạch MVP", "Bỏ qua cơ hội"],
                rationale="Đã thiết lập giả thuyết, ngưỡng thành công/dừng cố định (criteria_locked=True) trước khi viết mã.",
                estimated_cost_usd=0.01,
                success_criteria="ExperimentSpec được tạo và khóa tiêu chí thành công.",
                outcome=f"Experiment {exp_id} locked",
            )
            db.commit()
            return {"stage": project.stage, "experiment_id": exp_id}

        # 3. PRODUCT_PLANNING -> BUILDING
        if cur == ProjectLifecycleStage.PRODUCT_PLANNING.value:
            plan_task = self.engine.create_task(
                db,
                title=f"[CEO Orchestrator] Lập kiến trúc & kế hoạch thực thi MVP: {project.name[:55]}",
                description="Xác định danh sách công việc có thứ tự, tiêu chí hoàn thành và phạm vi MVP tối thiểu.",
                agent_id="ceo-orchestrator",
                project_id=project.id,
                idempotency_key=f"biz-plan:{project.id}",
                planned_steps=[
                    "Chốt phạm vi tính năng giải quyết trực tiếp nỗi đau khách hàng",
                    "Thiết lập cổng kiểm soát chất lượng pytest và ngân sách",
                ],
            )
            await self.engine.execute_task(db, plan_task.id)
            next_st = ProjectLifecycleStage.BUILDING.value
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="APPROVE_PRODUCT_PLAN",
                inputs={"plan_task_id": plan_task.id, "task_status": plan_task.status},
                options=["Chuyển sang BUILDING trong workspace cô lập", "Thu hẹp thêm phạm vi"],
                rationale="Kế hoạch sản phẩm rõ ràng, sử dụng lõi kiểm toán tất định để không phụ thuộc cứng vào quota AI.",
                estimated_cost_usd=0.02,
                success_criteria="Task lập kế hoạch hoàn thành 100%.",
                outcome="Ready for isolated workspace build",
            )
            db.commit()
            return {"stage": project.stage, "plan_task_id": plan_task.id}

        # 4. BUILDING -> TESTING
        if cur == ProjectLifecycleStage.BUILDING.value:
            opp = db.scalar(
                select(BusinessOpportunity)
                .where(BusinessOpportunity.project_id == project.id)
                .order_by(BusinessOpportunity.total_score.desc())
                .limit(1)
            )
            prod_title = f"InvoiceGuard — {project.name[:60]}"
            artifact = await build_product_in_workspace(
                db,
                project=project,
                product_name=prod_title,
                opportunity_id=opp.id if opp else None,
                version="0.1.0",
                engine=self.engine,
            )
            next_st = ProjectLifecycleStage.TESTING.value
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="WORKSPACE_BUILD_COMPLETED",
                inputs={"product_id": artifact.id, "files_count": len(artifact.files_manifest_json)},
                options=["Chuyển sang TESTING (chạy pytest thực tế)", "Xây dựng lại"],
                rationale="Đã tạo đầy đủ đặc tả, mã nguồn, unit test, README và Landing Page trong workspace cô lập.",
                estimated_cost_usd=0.02,
                success_criteria="Tạo thành công gói mã nguồn sạch không chứa khóa bí mật.",
                outcome=f"Built {artifact.slug} v{artifact.version}",
            )
            db.commit()
            return {"stage": project.stage, "product_id": artifact.id}

        # 5. TESTING -> READY_FOR_REVIEW (or FAILED if tests fail)
        if cur == ProjectLifecycleStage.TESTING.value:
            artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.project_id == project.id))
            if not artifact:
                raise HTTPException(status_code=409, detail="Chưa có ProductArtifact để kiểm thử.")
            test_res = await run_isolated_product_tests(db, product=artifact, engine=self.engine)
            next_st = (
                ProjectLifecycleStage.READY_FOR_REVIEW.value
                if test_res["test_passed"]
                else ProjectLifecycleStage.FAILED.value
            )
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="VERIFY_AUTOMATED_TESTS",
                inputs={
                    "test_passed": test_res["test_passed"],
                    "test_total": test_res["test_total"],
                    "test_failures": test_res["test_failures"],
                },
                options=["Đưa sang READY_FOR_REVIEW nếu PASS 100%", "Chặn triển khai và đánh dấu FAILED nếu lỗi"],
                rationale="Chỉ cho phép chuyển giai đoạn khi bộ kiểm thử tự động pytest chạy thật đạt PASS 100%.",
                estimated_cost_usd=0.01,
                success_criteria="pytest exit_code == 0 và test_failures == 0.",
                outcome=f"Tests passed={test_res['test_passed']} ({test_res['test_total']} tests)",
            )
            db.commit()
            return {"stage": project.stage, "test_result": test_res}

        # 6. READY_FOR_REVIEW -> DEPLOYMENT
        if cur == ProjectLifecycleStage.READY_FOR_REVIEW.value:
            legal_ok, legal_reason = self._check_legal_compliance_gate(db, project)
            if not legal_ok:
                project.status = "PAUSED_LEGAL_AUDIT"
                self._log_decision(
                    db,
                    project_id=project.id,
                    stage_from=cur,
                    stage_to=cur,
                    decision_type="LEGAL_GATE_VETO",
                    inputs={"reason": legal_reason},
                    options=["Tạm dừng dự án để bổ sung giấy phép & kiểm toán pháp lý", "Triển khai mạo hiểm (Bị nghiêm cấm)"],
                    rationale=legal_reason,
                    estimated_cost_usd=0.0,
                    success_criteria="Có giấy phép ICP / Thẩm định Model Release / Đăng ký TMĐT hợp lệ.",
                    outcome="LEGAL_GATE_HALTED",
                )
                db.commit()
                return {"stage": project.stage, "halted": True, "legal_gate_error": legal_reason}
            artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.project_id == project.id))
            if not artifact or not artifact.test_passed:
                raise HTTPException(status_code=409, detail="Sản phẩm chưa đạt kiểm thử QA, không thể triển khai.")
            dep = deploy_product_controlled(db, product=artifact, environment="staging")
            next_st = (
                ProjectLifecycleStage.DEPLOYMENT.value
                if dep.status == "SUCCEEDED"
                else ProjectLifecycleStage.FAILED.value
            )
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="DEPLOY_TO_STAGING",
                inputs={"deployment_id": dep.id, "checks": dep.checks_json},
                options=["Triển khai vào môi trường Staging có Smoke Test", "Triển khai thẳng Production (Bị chặn, cần duyệt)"],
                rationale="Triển khai an toàn sang Staging, xác nhận Smoke Test và tạo điểm khôi phục (rollback) trước.",
                estimated_cost_usd=0.0,
                success_criteria="Tất cả 6 bước kiểm tra an toàn & Smoke Test đạt True.",
                outcome=f"Staging deployment {dep.status}",
            )
            db.commit()
            return {"stage": project.stage, "deployment_id": dep.id}

        # 7. DEPLOYMENT -> MARKETING
        if cur == ProjectLifecycleStage.DEPLOYMENT.value:
            artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.project_id == project.id))
            if not artifact:
                raise HTTPException(status_code=409, detail="ProductArtifact missing")
            drafts = await generate_ethical_marketing_drafts(
                db,
                project=project,
                product=artifact,
                target_audience="Kế toán dịch vụ, chủ shop nhỏ và freelancer",
                engine=self.engine,
            )
            next_st = ProjectLifecycleStage.MARKETING.value
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="PREPARE_APPROVAL_GATED_MARKETING",
                inputs={"drafts_created": len(drafts), "publish_status": "PENDING_APPROVAL"},
                options=["Lưu bản nháp chờ Admin duyệt xuất bản", "Tự động đăng công khai (Bị chặn theo chính sách)"],
                rationale="Tuân thủ tiếp thị hợp pháp: chuẩn bị nội dung SEO/hướng dẫn hữu ích và chờ phê duyệt trước khi công bố.",
                estimated_cost_usd=0.01,
                success_criteria="Các bản nháp vượt qua kiểm tra chất lượng và gắn yêu cầu phê duyệt.",
                outcome=f"{len(drafts)} drafts queued in PENDING_APPROVAL",
            )
            db.commit()
            return {"stage": project.stage, "marketing_drafts": len(drafts)}

        # 8. MARKETING -> MONETIZATION
        if cur == ProjectLifecycleStage.MARKETING.value:
            artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.project_id == project.id))
            if not artifact:
                raise HTTPException(status_code=409, detail="ProductArtifact missing")
            skus = ensure_product_catalog_for_artifact(db, artifact)
            next_st = ProjectLifecycleStage.MONETIZATION.value
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="CONFIGURE_SANDBOX_MONETIZATION",
                inputs={"catalog_skus": [s.sku for s in skus]},
                options=["Kích hoạt gói One-Time $19 & Subscription $9/tháng trong Sandbox", "Mở thanh toán thật chưa duyệt (Bị cấm)"],
                rationale="Cấu hình danh mục sản phẩm và kiểm chứng thanh toán qua Webhook HMAC trong Sandbox trước.",
                estimated_cost_usd=0.0,
                success_criteria="Catalog sẵn sàng tạo Checkout Session và xác minh chữ ký Webhook.",
                outcome=f"{len(skus)} SKUs configured (Sandbox active, Live gated)",
            )
            db.commit()
            return {"stage": project.stage, "catalog_count": len(skus)}

        # 9. MONETIZATION -> MEASUREMENT
        if cur == ProjectLifecycleStage.MONETIZATION.value:
            cust = get_customer_validation_summary(db, project_id=project.id)
            econ = calculate_unit_economics(db, project_id=project.id)
            next_st = ProjectLifecycleStage.MEASUREMENT.value
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="MEASURE_REAL_SIGNALS_AND_ECONOMICS",
                inputs={
                    "customer_status": cust["customer_status"],
                    "real_visitors": cust["real_visitors"],
                    "confirmed_net_revenue_usd": econ["net_received_usd"],
                },
                options=["Đo lường trung thực tín hiệu khách hàng thật", "Suy diễn doanh thu ước tính (Bị cấm)"],
                rationale=(
                    f"Trạng thái khách hàng hiện tại: {cust['customer_status']}. "
                    f"Doanh thu thực xác minh: ${econ['net_received_usd']:.2f} (tách biệt hoàn toàn khỏi giao dịch Sandbox)."
                ),
                estimated_cost_usd=0.0,
                success_criteria="Ghi nhận đúng số liệu thật hoặc hiển thị NO_CUSTOMERS_YET / INSUFFICIENT_DATA.",
                outcome=f"Customer state={cust['customer_status']}",
            )
            db.commit()
            return {"stage": project.stage, "customer_validation": cust, "unit_economics": econ}

        # 10. MEASUREMENT -> OPTIMIZATION
        if cur == ProjectLifecycleStage.MEASUREMENT.value:
            cust = get_customer_validation_summary(db, project_id=project.id)
            econ = calculate_unit_economics(db, project_id=project.id)
            recommendation = (
                "CONTINUE_COLLECTING_TRAFFIC"
                if cust["real_visitors"] < 20
                else ("ITERATE_LANDING_VALUE_PROP" if cust["voluntary_signups"] == 0 else "PREPARE_LIVE_APPROVAL")
            )
            next_st = ProjectLifecycleStage.OPTIMIZATION.value
            self._transition_stage(db, project, next_st)
            self._log_decision(
                db,
                project_id=project.id,
                stage_from=cur,
                stage_to=next_st,
                decision_type="OPTIMIZATION_VERDICT",
                inputs={
                    "visitors": cust["real_visitors"],
                    "signups": cust["voluntary_signups"],
                    "net_profit_est": econ["estimated_net_profit_usd"],
                },
                options=["Tiếp tục thu thập lượt truy cập thật", "Cải tiến thông điệp Landing Page", "Dừng thử nghiệm"],
                rationale=f"Quyết định dựa trên dữ liệu thực tế: {recommendation}. Không tự tuyên bố có lãi khi chưa có giao dịch Live xác thực.",
                estimated_cost_usd=0.0,
                success_criteria="Hoàn tất 1 vòng lặp tự kinh doanh khép kín từ nghiên cứu đến đo lường tối ưu.",
                outcome=recommendation,
            )
            db.commit()
            return {"stage": project.stage, "recommendation": recommendation}

        return {"stage": project.stage, "message": "Dự án đã hoàn tất chu trình tới pha OPTIMIZATION và đang theo dõi dữ liệu thực."}

    async def run_bounded_lifecycle(
        self,
        db: Session,
        project: Project,
        *,
        max_transitions: int = 10,
    ) -> dict[str, Any]:
        """
        Runs up to `min(max_transitions, MAX_ORCHESTRATOR_STEPS_PER_RUN)` stage transitions,
        halting immediately if paused, failed, blocked by budget, or reaching OPTIMIZATION.
        """
        limit = max(1, min(max_transitions, MAX_ORCHESTRATOR_STEPS_PER_RUN))
        transitions_executed: list[dict[str, Any]] = []

        for _ in range(limit):
            db.refresh(project)
            if project.stage in (
                ProjectLifecycleStage.OPTIMIZATION.value,
                ProjectLifecycleStage.PAUSED.value,
                ProjectLifecycleStage.FAILED.value,
                ProjectLifecycleStage.COMPLETED.value,
                ProjectLifecycleStage.ARCHIVED.value,
            ):
                break
            step_info = await self.advance_one_stage(db, project)
            transitions_executed.append(step_info)
            if step_info.get("halted"):
                break

        db.refresh(project)
        return self.get_project_full_dossier(db, project) | {"transitions_executed": transitions_executed}

    def pause_business_project(self, db: Session, project: Project, reason: str = "Tạm dừng theo lệnh người vận hành") -> Project:
        prev = project.stage
        project.status = "PAUSED"
        project.stage = ProjectLifecycleStage.PAUSED.value
        self._log_decision(
            db,
            project_id=project.id,
            stage_from=prev,
            stage_to=ProjectLifecycleStage.PAUSED.value,
            decision_type="PAUSE_PROJECT",
            inputs={"reason": reason},
            options=["Tạm dừng dự án"],
            rationale=reason,
            estimated_cost_usd=0.0,
            success_criteria="Toàn bộ tiến trình mới trên dự án được tạm dừng an toàn.",
            outcome="PAUSED",
        )
        db.commit()
        db.refresh(project)
        return project

    def resume_business_project(self, db: Session, project: Project) -> Project:
        # Determine last active non-paused stage from decision log
        last_dec = db.scalar(
            select(OrchestratorDecision)
            .where(
                OrchestratorDecision.project_id == project.id,
                OrchestratorDecision.stage_from != ProjectLifecycleStage.PAUSED.value,
                OrchestratorDecision.stage_to == ProjectLifecycleStage.PAUSED.value,
            )
            .order_by(OrchestratorDecision.created_at.desc())
            .limit(1)
        )
        restored_stage = (
            last_dec.stage_from
            if last_dec and last_dec.stage_from not in ("NONE", ProjectLifecycleStage.PAUSED.value)
            else ProjectLifecycleStage.DISCOVERY.value
        )
        project.status = "ACTIVE"
        project.stage = restored_stage
        self._log_decision(
            db,
            project_id=project.id,
            stage_from=ProjectLifecycleStage.PAUSED.value,
            stage_to=restored_stage,
            decision_type="RESUME_PROJECT",
            inputs={"restored_stage": restored_stage},
            options=["Khôi phục về giai đoạn trước khi tạm dừng"],
            rationale="Người vận hành khôi phục dự án; tiếp tục từ trạng thái đã lưu.",
            estimated_cost_usd=0.0,
            success_criteria="Khôi phục đúng giai đoạn mà không chạy trùng bước cũ.",
            outcome=f"Resumed at {restored_stage}",
        )
        db.commit()
        db.refresh(project)
        return project

    def detect_and_recover_stalled_tasks(self, db: Session, timeout_seconds: int | None = None) -> list[str]:
        """
        Phase 10 Watchdog: Detects tasks stuck in RUNNING beyond timeout, transitions them safely
        to PAUSED (preserving checkpoints!) and resets their agents so work is never silently hung.
        """
        cfg = get_settings()
        max_age = timeout_seconds if timeout_seconds is not None else cfg.STALLED_TASK_TIMEOUT_SECONDS
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age)

        running = db.scalars(select(Task).where(Task.status == TaskStatus.RUNNING.value)).all()
        recovered_ids: list[str] = []
        for t in running:
            ref_time = t.updated_at or t.started_at or t.created_at
            if ref_time and ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=timezone.utc)
            if ref_time and ref_time < cutoff:
                t.status = TaskStatus.PAUSED.value
                t.last_error = f"Phát hiện tác vụ treo quá {max_age}s — đã tạm dừng an toàn tại checkpoint #{t.completed_steps}."
                ag = db.scalar(select(Agent).where(Agent.id == t.agent_id))
                if ag:
                    set_agent_status(db, ag, AgentStatus.PAUSED.value, t.id)
                emit_notification(
                    db,
                    notif_type=NotificationType.TASK_PAUSED.value,
                    title=f"Phát hiện Task bị đình trệ: {t.title[:60]}",
                    message=t.last_error,
                    task_id=t.id,
                    agent_id=t.agent_id,
                )
                recovered_ids.append(t.id)

        if recovered_ids:
            db.commit()
        return recovered_ids

    def get_project_full_dossier(self, db: Session, project: Project) -> dict[str, Any]:
        opportunities = db.scalars(
            select(BusinessOpportunity)
            .where(BusinessOpportunity.project_id == project.id)
            .order_by(BusinessOpportunity.total_score.desc())
        ).all()
        product = db.scalar(select(ProductArtifact).where(ProductArtifact.project_id == project.id))
        decisions = db.scalars(
            select(OrchestratorDecision)
            .where(OrchestratorDecision.project_id == project.id)
            .order_by(OrchestratorDecision.created_at.desc())
            .limit(30)
        ).all()
        deployments = db.scalars(
            select(DeploymentRecord)
            .where(DeploymentRecord.project_id == project.id)
            .order_by(DeploymentRecord.deployed_at.desc())
        ).all()
        marketing_items = db.scalars(
            select(MarketingCampaignItem)
            .where(MarketingCampaignItem.project_id == project.id)
            .order_by(MarketingCampaignItem.created_at.desc())
        ).all()
        catalog = db.scalars(
            select(ProductCatalogItem).where(ProductCatalogItem.project_id == project.id)
        ).all()
        experiments = db.scalars(
            select(BusinessExperiment).where(BusinessExperiment.project_id == project.id)
        ).all()

        exp_out = []
        for ex in experiments:
            sp = db.scalar(select(ExperimentSpec).where(ExperimentSpec.experiment_id == ex.id))
            exp_out.append(
                {
                    "id": ex.id,
                    "title": ex.title,
                    "hypothesis": ex.hypothesis,
                    "market_segment": ex.market_segment,
                    "opportunity_score": ex.opportunity_score,
                    "validation_status": ex.validation_status,
                    "visitors": ex.visitors,
                    "signups": ex.signups,
                    "paying_customers": ex.paying_customers,
                    "is_demo": ex.is_demo,
                    "criteria_locked": sp.criteria_locked if sp else False,
                    "success_metric": sp.success_metric_json if sp else {},
                    "stop_metric": sp.stop_metric_json if sp else {},
                    "next_decision": sp.next_decision if sp else "PENDING",
                }
            )

        return {
            "id": project.id,
            "name": project.name,
            "slug": project.slug,
            "stage": project.stage,
            "status": project.status,
            "summary": project.summary,
            "opportunity_score": project.opportunity_score,
            "qa_passed": project.qa_passed,
            "is_demo": project.is_demo,
            "created_at": project.created_at.isoformat() if project.created_at else None,
            "opportunities": [serialize_opportunity(o) for o in opportunities],
            "product": (
                {
                    "id": product.id,
                    "name": product.name,
                    "slug": product.slug,
                    "version": product.version,
                    "workspace_path": product.workspace_path,
                    "build_status": product.build_status,
                    "test_passed": product.test_passed,
                    "test_total": product.test_total,
                    "test_failures": product.test_failures,
                    "test_output": product.test_output,
                    "files_manifest": product.files_manifest_json,
                    "user_stories": product.user_stories_json,
                    "acceptance_criteria": product.acceptance_criteria_json,
                    "landing_page_url": product.landing_page_url,
                }
                if product
                else None
            ),
            "deployments": [
                {
                    "id": d.id,
                    "version": d.version,
                    "environment": d.environment,
                    "status": d.status,
                    "checks": d.checks_json,
                    "rollback_version": d.rollback_version,
                    "approval_request_id": d.approval_request_id,
                    "deployed_at": d.deployed_at.isoformat() if d.deployed_at else None,
                }
                for d in deployments
            ],
            "marketing_items": [
                {
                    "id": m.id,
                    "channel": m.channel,
                    "title": m.title,
                    "publish_status": m.publish_status,
                    "quality_check_passed": m.quality_check_passed,
                    "impressions": m.impressions,
                    "clicks": m.clicks,
                    "conversions": m.conversions,
                }
                for m in marketing_items
            ],
            "catalog": [
                {
                    "id": c.id,
                    "sku": c.sku,
                    "name": c.name,
                    "billing_model": c.billing_model,
                    "price_usd": c.price_usd,
                    "sandbox_enabled": c.sandbox_enabled,
                    "live_approved": c.live_approved,
                }
                for c in catalog
            ],
            "experiments": exp_out,
            "customer_validation": get_customer_validation_summary(db, project_id=project.id),
            "unit_economics": calculate_unit_economics(db, project_id=project.id),
            "decisions": [
                {
                    "id": d.id,
                    "cycle_index": d.cycle_index,
                    "stage_from": d.stage_from,
                    "stage_to": d.stage_to,
                    "decision_type": d.decision_type,
                    "inputs": d.input_summary_json,
                    "options_considered": d.options_considered_json,
                    "rationale": d.rationale,
                    "estimated_cost_usd": d.estimated_cost_usd,
                    "success_criteria": d.success_criteria,
                    "execution_outcome": d.execution_outcome,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in decisions
            ],
        }
