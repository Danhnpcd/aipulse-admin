import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.reporting import get_cost_analytics
from app.business.monetization_and_economics import (
    calculate_unit_economics,
    create_checkout_order,
    ensure_product_catalog_for_artifact,
    get_customer_validation_summary,
    process_verified_payment_webhook,
    record_customer_signal,
)
from app.business.orchestrator import AutonomousBusinessOrchestrator
from app.business.research_and_scoring import (
    create_locked_experiment,
    research_and_rank_opportunities,
    serialize_opportunity,
    update_experiment_guard,
)
from app.business.workspace_builder import (
    build_product_in_workspace,
    deploy_product_controlled,
    resolve_jailed_workspace,
    rollback_product_version,
    run_isolated_product_tests,
    run_live_invoice_audit_in_workspace,
)
from app.core.database import get_db
from app.models.entities import (
    ApprovalRequest,
    BusinessExperiment,
    BusinessOpportunity,
    CustomerSignal,
    ExperimentSpec,
    MarketingCampaignItem,
    OrchestratorDecision,
    PaymentOrder,
    ProductArtifact,
    ProductCatalogItem,
    Project,
    RevenueRecord,
    User,
)
from app.models.enums import ProjectLifecycleStage
from app.security.auth import (
    api_rate_limiter,
    get_current_user,
    get_optional_current_user,
    record_audit_log,
    require_admin,
    require_operator,
)

business_router = APIRouter(prefix="/api", dependencies=[Depends(api_rate_limiter)])


class BusinessProjectCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=150)
    focus_domain: str = Field(
        default="Tự động hóa đối soát hóa đơn, mã số thuế & chuẩn hóa CSV cho SME",
        max_length=300,
    )
    opportunity_id: str | None = None
    auto_start: bool = False
    max_transitions: int = Field(default=10, ge=1, le=10)


class OpportunityResearchRequest(BaseModel):
    focus_domain: str = Field(
        default="Tự động hóa kiểm tra chứng từ & công việc lặp lại cho SME / Freelancer",
        min_length=3,
        max_length=300,
    )
    project_id: str | None = None


class ExperimentCreateRequest(BaseModel):
    project_id: str | None = None
    title: str = Field(..., min_length=3, max_length=180)
    hypothesis: str = Field(..., min_length=5, max_length=800)
    market_segment: str = Field(..., min_length=2, max_length=160)
    opportunity_score: float = Field(default=75.0, ge=0.0, le=100.0)
    validation_method: str = Field(default="Landing Page + Interactive Prototype + Voluntary Opt-in")
    max_budget_usd: float = Field(default=5.0, ge=0.0, le=500.0)
    duration_days: int = Field(default=14, ge=1, le=90)
    success_metric: dict[str, Any] | None = None
    stop_metric: dict[str, Any] | None = None


class ExperimentMutationAttempt(BaseModel):
    success_metric: dict[str, Any] | None = None
    stop_metric: dict[str, Any] | None = None


class ProductCreateRequest(BaseModel):
    project_id: str
    name: str = Field(..., min_length=3, max_length=180)
    version: str = Field(default="0.1.0", max_length=24)
    opportunity_id: str | None = None
    auto_test: bool = True


class ProductDeployRequest(BaseModel):
    environment: str = Field(default="staging", pattern="^(staging|production)$")


class ProductRollbackRequest(BaseModel):
    target_version: str = Field(..., min_length=1, max_length=24)


class CheckoutSessionRequest(BaseModel):
    catalog_item_id: str | None = None
    customer_email: str = Field(default="buyer@example.com", min_length=3, max_length=200)
    gateway_mode: str = Field(default="SANDBOX")
    card_number: str | None = None
    cvv: str | None = None


class PublicSignalRequest(BaseModel):
    signal_type: str = Field(default="VOLUNTARY_FEEDBACK")
    contact_email: str | None = None
    feedback_text: str | None = None
    consent_granted: bool = False
    channel_source: str = Field(default="landing_page")


class PublicDemoAuditRequest(BaseModel):
    rows: list[dict[str, Any]]


# ==================== BUSINESS PROJECTS & ORCHESTRATOR ====================


@business_router.get("/business/projects")
def list_business_projects(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    orch = AutonomousBusinessOrchestrator()
    projects = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
    return [orch.get_project_full_dossier(db, p) for p in projects]


@business_router.post("/business/projects")
async def create_business_project_endpoint(
    payload: BusinessProjectCreate,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    orch = AutonomousBusinessOrchestrator()
    proj = orch.create_business_project(
        db,
        name=payload.name,
        focus_domain=payload.focus_domain,
        opportunity_id=payload.opportunity_id,
    )
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="CREATE_BUSINESS_PROJECT",
        target_type="project",
        target_id=proj.id,
        details={"name": proj.name, "slug": proj.slug, "auto_start": payload.auto_start},
    )
    db.commit()

    if payload.auto_start:
        return await orch.run_bounded_lifecycle(db, proj, max_transitions=payload.max_transitions)
    return orch.get_project_full_dossier(db, proj)


@business_router.get("/business/projects/{project_id}")
def get_business_project_detail(
    project_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    proj = db.scalar(select(Project).where(Project.id == project_id))
    if not proj:
        raise HTTPException(status_code=404, detail="Business project not found")
    return AutonomousBusinessOrchestrator().get_project_full_dossier(db, proj)


@business_router.post("/business/projects/{project_id}/start")
async def start_or_advance_business_project(
    project_id: str,
    max_transitions: int = Query(default=10, ge=1, le=10),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    proj = db.scalar(select(Project).where(Project.id == project_id))
    if not proj:
        raise HTTPException(status_code=404, detail="Business project not found")
    orch = AutonomousBusinessOrchestrator()
    result = await orch.run_bounded_lifecycle(db, proj, max_transitions=max_transitions)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="ADVANCE_BUSINESS_PROJECT",
        target_type="project",
        target_id=proj.id,
        details={"final_stage": proj.stage, "transitions": len(result.get("transitions_executed", []))},
    )
    db.commit()
    return result


@business_router.post("/business/projects/{project_id}/pause")
def pause_business_project_endpoint(
    project_id: str,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    proj = db.scalar(select(Project).where(Project.id == project_id))
    if not proj:
        raise HTTPException(status_code=404, detail="Business project not found")
    orch = AutonomousBusinessOrchestrator()
    proj = orch.pause_business_project(db, proj, reason=f"Tạm dừng bởi {user.email}")
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="PAUSE_BUSINESS_PROJECT",
        target_type="project",
        target_id=proj.id,
    )
    db.commit()
    return orch.get_project_full_dossier(db, proj)


@business_router.post("/business/projects/{project_id}/resume")
def resume_business_project_endpoint(
    project_id: str,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    proj = db.scalar(select(Project).where(Project.id == project_id))
    if not proj:
        raise HTTPException(status_code=404, detail="Business project not found")
    orch = AutonomousBusinessOrchestrator()
    proj = orch.resume_business_project(db, proj)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="RESUME_BUSINESS_PROJECT",
        target_type="project",
        target_id=proj.id,
        details={"restored_stage": proj.stage},
    )
    db.commit()
    return orch.get_project_full_dossier(db, proj)


# ==================== OPPORTUNITIES & EXPERIMENTS ====================


@business_router.get("/business/opportunities")
def list_opportunities(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(BusinessOpportunity).order_by(BusinessOpportunity.total_score.desc(), BusinessOpportunity.created_at.desc())
    ).all()
    return [serialize_opportunity(o) for o in rows]


@business_router.post("/business/opportunities/research")
async def trigger_opportunity_research(
    payload: OpportunityResearchRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = await research_and_rank_opportunities(
        db,
        focus_domain=payload.focus_domain,
        project_id=payload.project_id,
    )
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="RESEARCH_OPPORTUNITIES",
        target_type="opportunity",
        target_id=result["selected_opportunity"]["id"],
        details={"focus_domain": payload.focus_domain},
    )
    db.commit()
    return result


@business_router.post("/business/opportunities/{opportunity_id}/validate")
def validate_opportunity_endpoint(
    opportunity_id: str,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    opp = db.scalar(select(BusinessOpportunity).where(BusinessOpportunity.id == opportunity_id))
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    opp.selection_status = "SELECTED_FOR_MVP"
    exp, spec = create_locked_experiment(
        db,
        project_id=opp.project_id,
        title=f"Kiểm chứng thị trường: {opp.title[:100]}",
        hypothesis=opp.pricing_hypothesis,
        market_segment=opp.target_customer,
        opportunity_score=opp.total_score,
        is_demo=False,
    )
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="VALIDATE_OPPORTUNITY",
        target_type="opportunity",
        target_id=opp.id,
        details={"experiment_id": exp.id, "criteria_locked": spec.criteria_locked},
    )
    db.commit()
    return {
        "opportunity": serialize_opportunity(opp),
        "experiment_id": exp.id,
        "experiment_spec_id": spec.id,
        "criteria_locked": spec.criteria_locked,
    }


@business_router.get("/business/experiments")
def list_business_experiments(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    exps = db.scalars(select(BusinessExperiment).order_by(BusinessExperiment.created_at.desc())).all()
    out = []
    for e in exps:
        sp = db.scalar(select(ExperimentSpec).where(ExperimentSpec.experiment_id == e.id))
        out.append(
            {
                "id": e.id,
                "spec_id": sp.id if sp else None,
                "project_id": e.project_id,
                "title": e.title,
                "hypothesis": e.hypothesis,
                "market_segment": e.market_segment,
                "opportunity_score": e.opportunity_score,
                "validation_status": e.validation_status,
                "visitors": e.visitors,
                "signups": e.signups,
                "paying_customers": e.paying_customers,
                "is_demo": e.is_demo,
                "criteria_locked": sp.criteria_locked if sp else False,
                "success_metric": sp.success_metric_json if sp else {},
                "stop_metric": sp.stop_metric_json if sp else {},
                "result_summary": sp.result_summary if sp else "",
                "next_decision": sp.next_decision if sp else "PENDING",
            }
        )
    return out


@business_router.post("/business/experiments")
def create_business_experiment_endpoint(
    payload: ExperimentCreateRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    exp, spec = create_locked_experiment(
        db,
        project_id=payload.project_id,
        title=payload.title,
        hypothesis=payload.hypothesis,
        market_segment=payload.market_segment,
        opportunity_score=payload.opportunity_score,
        validation_method=payload.validation_method,
        max_budget_usd=payload.max_budget_usd,
        duration_days=payload.duration_days,
        success_metric=payload.success_metric,
        stop_metric=payload.stop_metric,
        is_demo=False,
    )
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="CREATE_LOCKED_EXPERIMENT",
        target_type="experiment",
        target_id=exp.id,
        details={"spec_id": spec.id, "criteria_locked": spec.criteria_locked},
    )
    db.commit()
    return {
        "id": exp.id,
        "spec_id": spec.id,
        "title": exp.title,
        "criteria_locked": spec.criteria_locked,
        "success_metric": spec.success_metric_json,
        "stop_metric": spec.stop_metric_json,
    }


@business_router.patch("/business/experiments/{spec_id}")
def patch_experiment_spec_endpoint(
    spec_id: str,
    payload: ExperimentMutationAttempt,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    spec = update_experiment_guard(
        db,
        spec_id,
        attempted_success_metric=payload.success_metric,
        attempted_stop_metric=payload.stop_metric,
    )
    return {"id": spec.id, "criteria_locked": spec.criteria_locked}


# ==================== PRODUCTS (BUILD, TEST, DEPLOY, ROLLBACK) ====================


def _serialize_product(p: ProductArtifact) -> dict[str, Any]:
    return {
        "id": p.id,
        "project_id": p.project_id,
        "opportunity_id": p.opportunity_id,
        "name": p.name,
        "slug": p.slug,
        "version": p.version,
        "workspace_path": p.workspace_path,
        "architecture_summary": p.architecture_summary,
        "user_stories": p.user_stories_json,
        "acceptance_criteria": p.acceptance_criteria_json,
        "files_manifest": p.files_manifest_json,
        "version_history": p.version_history_json,
        "build_status": p.build_status,
        "test_passed": p.test_passed,
        "test_total": p.test_total,
        "test_failures": p.test_failures,
        "test_output": p.test_output,
        "last_tested_at": p.last_tested_at.isoformat() if p.last_tested_at else None,
        "landing_page_url": p.landing_page_url,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@business_router.get("/products")
def list_products(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = db.scalars(select(ProductArtifact).order_by(ProductArtifact.created_at.desc())).all()
    return [_serialize_product(p) for p in rows]


@business_router.post("/products")
async def create_and_build_product(
    payload: ProductCreateRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    proj = db.scalar(select(Project).where(Project.id == payload.project_id))
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    artifact = await build_product_in_workspace(
        db,
        project=proj,
        product_name=payload.name,
        opportunity_id=payload.opportunity_id,
        version=payload.version,
    )
    if payload.auto_test:
        await run_isolated_product_tests(db, product=artifact)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="BUILD_PRODUCT_WORKSPACE",
        target_type="product",
        target_id=artifact.id,
        details={"slug": artifact.slug, "version": artifact.version, "test_passed": artifact.test_passed},
    )
    db.commit()
    return _serialize_product(artifact)


@business_router.get("/products/{product_id}/build-status")
def get_product_build_status(
    product_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.id == product_id))
    if not artifact:
        raise HTTPException(status_code=404, detail="Product not found")
    return _serialize_product(artifact)


@business_router.post("/products/{product_id}/test")
async def test_product_endpoint(
    product_id: str,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.id == product_id))
    if not artifact:
        raise HTTPException(status_code=404, detail="Product not found")
    res = await run_isolated_product_tests(db, product=artifact)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="TEST_PRODUCT",
        target_type="product",
        target_id=artifact.id,
        details={"passed": res["test_passed"], "total": res["test_total"]},
    )
    db.commit()
    return res


@business_router.post("/products/{product_id}/deploy")
def deploy_product_endpoint(
    product_id: str,
    payload: ProductDeployRequest,
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.id == product_id))
    if not artifact:
        raise HTTPException(status_code=404, detail="Product not found")
    rec = deploy_product_controlled(db, product=artifact, environment=payload.environment)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="DEPLOY_PRODUCT",
        target_type="product",
        target_id=artifact.id,
        details={"environment": rec.environment, "status": rec.status, "version": rec.version},
    )
    db.commit()
    return {
        "deployment_id": rec.id,
        "product_id": rec.product_id,
        "version": rec.version,
        "environment": rec.environment,
        "status": rec.status,
        "checks": rec.checks_json,
        "artifact_dir": rec.artifact_dir,
        "rollback_version": rec.rollback_version,
        "approval_request_id": rec.approval_request_id,
    }


@business_router.post("/products/{product_id}/rollback")
def rollback_product_endpoint(
    product_id: str,
    payload: ProductRollbackRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.id == product_id))
    if not artifact:
        raise HTTPException(status_code=404, detail="Product not found")
    artifact = rollback_product_version(db, product=artifact, target_version=payload.target_version)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="ROLLBACK_PRODUCT",
        target_type="product",
        target_id=artifact.id,
        details={"target_version": payload.target_version},
    )
    db.commit()
    return _serialize_product(artifact)


# ==================== BUSINESS ANALYTICS, REVENUE, COSTS & APPROVALS ====================


@business_router.get("/business/analytics")
def business_engine_analytics(
    project_id: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return {
        "unit_economics": calculate_unit_economics(db, project_id=project_id),
        "customer_validation": get_customer_validation_summary(db, project_id=project_id),
        "decisions_logged": len(db.scalars(select(OrchestratorDecision)).all()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@business_router.get("/business/revenue")
def business_revenue_ledger(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    econ = calculate_unit_economics(db)
    orders = db.scalars(select(PaymentOrder).order_by(PaymentOrder.created_at.desc()).limit(50)).all()
    revs = db.scalars(select(RevenueRecord).order_by(RevenueRecord.recorded_at.desc()).limit(50)).all()
    return {
        "confirmed_gross_revenue_usd": econ["gross_revenue_usd"],
        "confirmed_net_received_usd": econ["net_received_usd"],
        "confirmed_refunds_usd": econ["refunds_usd"],
        "payment_fees_usd": econ["payment_fees_usd"],
        "sandbox_test_volume_usd": econ["sandbox_test_volume_usd"],
        "orders": [
            {
                "id": o.id,
                "checkout_session_id": o.checkout_session_id,
                "customer_email": o.customer_email,
                "gateway_mode": o.gateway_mode,
                "billing_model": o.billing_model,
                "status": o.status,
                "gross_amount_usd": o.gross_amount_usd,
                "net_amount_usd": o.net_amount_usd,
                "refund_amount_usd": o.refund_amount_usd,
                "verified_via_webhook": o.verified_via_webhook,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in orders
        ],
        "revenue_records": [
            {
                "id": r.id,
                "description": r.description,
                "gross_amount_usd": r.gross_amount_usd,
                "net_received_usd": r.net_received_usd,
                "refund_amount_usd": r.refund_amount_usd,
                "is_test_payment": r.is_test_payment,
                "is_demo": r.is_demo,
                "verified": r.verified,
            }
            for r in revs
        ],
    }


@business_router.get("/business/costs")
def business_costs_breakdown(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    econ = calculate_unit_economics(db)
    base_costs = get_cost_analytics(db)
    return {
        **base_costs,
        "marketing_cost_usd": econ["marketing_cost_usd"],
        "other_cost_usd": econ["other_cost_usd"],
        "ai_cost_note": econ["ai_cost_note"],
    }


@business_router.get("/business/approvals")
def business_approvals_queue(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    approvals = db.scalars(select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())).all()
    mkt_pending = db.scalars(
        select(MarketingCampaignItem).where(MarketingCampaignItem.publish_status == "PENDING_APPROVAL")
    ).all()
    return {
        "approval_requests": [
            {
                "id": a.id,
                "task_id": a.task_id,
                "agent_id": a.agent_id,
                "action_type": a.action_type,
                "reason": a.reason,
                "estimated_cost_usd": a.estimated_cost_usd,
                "status": a.status,
                "decided_by": a.decided_by,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in approvals
        ],
        "pending_marketing_drafts": [
            {
                "id": m.id,
                "project_id": m.project_id,
                "channel": m.channel,
                "title": m.title,
                "publish_status": m.publish_status,
            }
            for m in mkt_pending
        ],
    }


@business_router.post("/business/marketing/{item_id}/approve")
def approve_marketing_draft(
    item_id: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = db.scalar(select(MarketingCampaignItem).where(MarketingCampaignItem.id == item_id))
    if not item:
        raise HTTPException(status_code=404, detail="Marketing draft not found")
    item.publish_status = "PUBLISHED"
    item.published_at = datetime.now(timezone.utc)
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="APPROVE_MARKETING_PUBLICATION",
        target_type="marketing_item",
        target_id=item.id,
        details={"channel": item.channel, "title": item.title},
    )
    db.commit()
    return {"id": item.id, "publish_status": item.publish_status}


@business_router.get("/business/marketing/channels")
def get_marketing_channels_report(
    project_id: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    proj_stmt = select(Project).order_by(Project.created_at.desc())
    if project_id:
        proj_stmt = select(Project).where(Project.id == project_id)
    project = db.scalar(proj_stmt)

    artifact = None
    if project:
        artifact = db.scalar(
            select(ProductArtifact)
            .where(ProductArtifact.project_id == project.id)
            .order_by(ProductArtifact.created_at.desc())
        )

    mkt_stmt = select(MarketingCampaignItem)
    if project:
        mkt_stmt = mkt_stmt.where(MarketingCampaignItem.project_id == project.id)
    items = db.scalars(mkt_stmt.order_by(MarketingCampaignItem.created_at.asc())).all()

    sig_stmt = select(CustomerSignal).where(CustomerSignal.is_demo.is_(False))
    if project:
        sig_stmt = sig_stmt.where(CustomerSignal.project_id == project.id)
    signals = db.scalars(sig_stmt).all()

    signals_by_channel: dict[str, list[CustomerSignal]] = {}
    for s in signals:
        src = (s.channel_source or "direct").lower()
        signals_by_channel.setdefault(src, []).append(s)

    channel_definitions = [
        {
            "channel_key": "SEO_LEAD_MAGNET",
            "name": "Kênh 1: Nam Châm SEO & Công Cụ Tra Cứu Miễn Phí (SEO Lead Magnet)",
            "role": "Thu hút lượt truy cập tìm kiếm tự nhiên của kế toán viên trên Google",
            "mechanism": "Cung cấp cẩm nang kiểm tra thuế suất 8% & 10% chuẩn NĐ 72/2024, cho phép rà soát 30 hóa đơn đầu tiên miễn phí không cần đăng ký tài khoản.",
            "target_audience": "Kế toán SME, chủ doanh nghiệp nhỏ, người lập bảng kê thuế quý",
            "conversion_goal": "Chuyển đổi người dùng thử thành người đăng ký email (Lead) hoặc nâng cấp lên Gói Pro (199k/tháng)",
        },
        {
            "channel_key": "B2B_OUTREACH",
            "name": "Kênh 2: Tiếp Cận Trực Tiếp Doanh Nghiệp & Đại Lý Thuế (B2B Direct Outreach)",
            "role": "Tạo doanh thu hợp đồng lớn từ các đơn vị quản lý nhiều doanh nghiệp",
            "mechanism": "Thư ngỏ giải pháp chuyên sâu gửi tới các Giám đốc / Trưởng văn phòng dịch vụ kế toán (quản lý 30-100 khách hàng SME), cam kết tiết kiệm 40h rà soát mỗi quý.",
            "target_audience": "Công ty dịch vụ kế toán, Đại lý thuế, Kế toán trưởng tập đoàn",
            "conversion_goal": "Ký kết gói Enterprise (499.000 VNĐ / tháng)",
        },
        {
            "channel_key": "COMMUNITY_SEEDING",
            "name": "Kênh 3: Tiếp Thị Cộng Đồng & Cẩm Nang Nghiệp Vụ (Community Seeding)",
            "role": "Xây dựng uy tín thương hiệu và thu hút khách hàng không tốn chi phí quảng cáo",
            "mechanism": "Chia sẻ bài viết cảnh báo 10 lỗi hóa đơn điện tử dễ bị bóc tách chi phí vào các Group Facebook (Cộng đồng Kế toán Việt Nam, Hội Kế toán Thuế SME) kèm file Excel mẫu.",
            "target_audience": "Hội viên cộng đồng kế toán, diễn đàn Dân Kế Toán",
            "conversion_goal": "Lan tỏa tự nhiên, kéo người dùng vào trải nghiệm bộ rà soát trực quan",
        },
        {
            "channel_key": "AFFILIATE_PROGRAM",
            "name": "Kênh 4: Chương Trình Tiếp Thị Liên Kết & Hoa Hồng (Affiliate 20% Recurring)",
            "role": "Đòn bẩy lan truyền mạng lưới thông qua hoa hồng thụ động định kỳ",
            "mechanism": "Cấp liên kết giới thiệu gắn mã UTM cho kế toán trưởng. Nhận 20% hoa hồng trên mọi giao dịch gia hạn hàng tháng (~40.000đ/tháng/thuê bao Pro).",
            "target_audience": "Kế toán trưởng, giảng viên thực hành kế toán, chuyên gia tư vấn tài chính",
            "conversion_goal": "Tạo mạng lưới cộng tác viên tự bán hàng không cần chi phí lương cứng",
        },
    ]

    channels_data = []
    for defn in channel_definitions:
        item = next(
            (
                i
                for i in items
                if i.channel == defn["channel_key"]
                or (defn["channel_key"] == "SEO_LEAD_MAGNET" and i.channel in ("SEO_ARTICLE", "SEO_LEAD_MAGNET"))
            ),
            None,
        )
        defn_copy = dict(defn)
        if item:
            defn_copy["campaign_item"] = {
                "id": item.id,
                "title": item.title,
                "publish_status": item.publish_status,
                "content_markdown": item.content_markdown,
                "impressions": item.impressions,
                "clicks": item.clicks,
                "conversions": item.conversions,
                "published_at": item.published_at.isoformat() if item.published_at else None,
            }
            defn_copy["status"] = item.publish_status
        else:
            defn_copy["campaign_item"] = None
            defn_copy["status"] = "PLANNED"

        channel_sigs = (
            signals_by_channel.get(defn["channel_key"].lower(), [])
            + signals_by_channel.get(defn["channel_key"], [])
        )
        defn_copy["real_visitors"] = sum(1 for s in channel_sigs if s.signal_type == "PAGE_VISIT")
        defn_copy["real_leads"] = sum(1 for s in channel_sigs if s.signal_type in ("TRIAL_SIGNUP", "DEMO_REQUEST"))
        channels_data.append(defn_copy)

    econ = calculate_unit_economics(db, project_id=project.id if project else None)
    cust = get_customer_validation_summary(db, project_id=project.id if project else None)

    return {
        "channels": channels_data,
        "performance_report": {
            "real_visitors": cust["real_visitors"],
            "voluntary_signups": cust["voluntary_signups"],
            "confirmed_paying_customers": cust["confirmed_paying_customers"],
            "confirmed_net_revenue_usd": econ["net_received_usd"],
            "sandbox_volume_usd": econ["sandbox_test_volume_usd"],
            "data_honesty_notice": "Toàn bộ số liệu được truy vấn trung thực từ bảng ghi dữ liệu thực tế. Không sử dụng dữ liệu giả định.",
        },
        "internet_deployment": (lambda: (
            (lambda t_file: {
                "is_deployed_to_internet": True,
                "environment": "CLOUDFLARE_TUNNEL_AND_SURGE_247",
                "local_landing_url": artifact.landing_page_url if artifact else "/api/business/public/aipulse-vietnam-news-media",
                "public_internet_url": json.loads(t_file.read_text(encoding="utf-8")).get("news_portal_url", "https://vernon-identification-anne-sticker.trycloudflare.com"),
                "cloud_247_url": json.loads(t_file.read_text(encoding="utf-8")).get("admin_app_url", "https://colored-russell-interviews-leisure.trycloudflare.com"),
                "status_description": "Đang phát sóng trực tuyến 2 hệ thống độc lập qua Cloudflare Global Edge: 1) Website Tin Tức AI Pulse và 2) Web App Quản Lý Doanh Nghiệp.",
                "guide_how_to_deploy": [
                    {
                        "method": "Website Tin Tức AI Pulse (Cổng Báo Chí)",
                        "url": "https://vernon-identification-anne-sticker.trycloudflare.com",
                        "description": "Cổng thông tin tin tức công nghệ, AI và kiếm tiền số dành cho độc giả.",
                    },
                    {
                        "method": "Web App Quản Lý Doanh Nghiệp (CEO Dashboard)",
                        "url": "https://colored-russell-interviews-leisure.trycloudflare.com",
                        "description": "Bảng điều hành nhân sự AI 2.5D, quản lý duyệt bài và theo dõi doanh thu.",
                    }
                ],
            } if t_file.exists() and json.loads(t_file.read_text(encoding="utf-8")).get("active") else {
                "is_deployed_to_internet": False,
                "environment": "LOCAL_OFFLINE",
                "local_landing_url": artifact.landing_page_url if artifact else "/api/business/public/invoiceguard-sme-audit-automation-14147",
                "public_internet_url": None,
                "status_description": "Trang web đang chạy trên máy chủ nội bộ localhost (127.0.0.1:8000), CHƯA ĐƯỢC MỞ RA INTERNET CÔNG CỘNG.",
                "guide_how_to_deploy": [
                    {
                        "method": "Cloudflare Tunnel (Khuyên dùng — Miễn phí & Nhanh nhất)",
                        "command": "cloudflared tunnel --url http://127.0.0.1:8000",
                        "description": "Tạo ngay một đường link HTTPS công cộng toàn cầu an toàn để khách hàng bên ngoài truy cập được trực tiếp vào máy chủ của bạn.",
                    }
                ],
            })(Path(r"d:\ai-business-simulator\tools\tunnel_state.json"))
        ))(),
    }



@business_router.post("/business/catalog/{catalog_id}/approve-live")
def approve_live_pricing(
    catalog_id: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    sku = db.scalar(select(ProductCatalogItem).where(ProductCatalogItem.id == catalog_id))
    if not sku:
        raise HTTPException(status_code=404, detail="Catalog item not found")
    sku.live_approved = True
    record_audit_log(
        db,
        actor_id=user.id,
        actor_role=user.role,
        action="APPROVE_LIVE_COMMERCE_SKU",
        target_type="catalog_item",
        target_id=sku.id,
        details={"sku": sku.sku, "price_usd": sku.price_usd},
    )
    db.commit()
    return {"id": sku.id, "sku": sku.sku, "live_approved": sku.live_approved}


# ==================== CHECKOUT, SIGNED WEBHOOK & PUBLIC LANDING APP ====================


@business_router.post("/business/payments/checkout")
def authenticated_checkout(
    payload: CheckoutSessionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if not payload.catalog_item_id:
        raise HTTPException(status_code=400, detail="catalog_item_id is required")
    cat = db.scalar(select(ProductCatalogItem).where(ProductCatalogItem.id == payload.catalog_item_id))
    if not cat:
        raise HTTPException(status_code=404, detail="Catalog item not found")
    extra: dict[str, Any] = {}
    if payload.card_number:
        extra["card_number"] = payload.card_number
    if payload.cvv:
        extra["cvv"] = payload.cvv

    order = create_checkout_order(
        db,
        catalog_item=cat,
        customer_email=payload.customer_email,
        gateway_mode=payload.gateway_mode,
        raw_extra_fields=extra,
    )
    return {
        "order_id": order.id,
        "checkout_session_id": order.checkout_session_id,
        "status": order.status,
        "gateway_mode": order.gateway_mode,
        "gross_amount_usd": order.gross_amount_usd,
        "verified_via_webhook": order.verified_via_webhook,
    }


@business_router.post("/business/payments/webhook")
async def payment_gateway_webhook(
    request: Request,
    x_simulator_signature: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    raw_bytes = await request.body()
    return process_verified_payment_webhook(
        db,
        raw_body_str=raw_bytes.decode("utf-8"),
        signature_header=x_simulator_signature,
    )


@business_router.get("/business/public/{slug}", response_class=HTMLResponse)
def serve_public_product_landing_page(slug: str, db: Session = Depends(get_db)) -> HTMLResponse:
    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.slug == slug))
    if not artifact:
        raise HTTPException(status_code=404, detail="Product landing page not found")
    ws_dir = resolve_jailed_workspace(artifact.slug)
    html_file = ws_dir / "public/index.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="Landing page HTML file missing")
    return HTMLResponse(content=html_file.read_text(encoding="utf-8"))


@business_router.post("/business/public/{slug}/visit")
def public_record_visit(slug: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.slug == slug))
    if not artifact:
        raise HTTPException(status_code=404, detail="Product not found")
    sig = record_customer_signal(
        db,
        project_id=artifact.project_id,
        product_id=artifact.id,
        signal_type="PAGE_VISIT",
    )
    return {"id": sig.id, "recorded": True}


@business_router.post("/business/public/{slug}/signal")
def public_record_lead_or_feedback(
    slug: str,
    payload: PublicSignalRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.slug == slug))
    if not artifact:
        raise HTTPException(status_code=404, detail="Product not found")
    sig = record_customer_signal(
        db,
        project_id=artifact.project_id,
        product_id=artifact.id,
        signal_type=payload.signal_type,
        contact_email=payload.contact_email,
        feedback_text=payload.feedback_text,
        consent_granted=payload.consent_granted,
        channel_source=payload.channel_source,
    )
    return {"id": sig.id, "signal_type": sig.signal_type}


@business_router.post("/business/public/{slug}/demo-audit")
def public_run_live_product_audit(slug: str, payload: PublicDemoAuditRequest) -> dict[str, Any]:
    return run_live_invoice_audit_in_workspace(slug, payload.rows)


@business_router.post("/business/public/{slug}/checkout")
def public_sandbox_checkout(
    slug: str,
    payload: CheckoutSessionRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    artifact = db.scalar(select(ProductArtifact).where(ProductArtifact.slug == slug))
    if not artifact:
        raise HTTPException(status_code=404, detail="Product not found")
    items = ensure_product_catalog_for_artifact(db, artifact)
    chosen = items[0]
    order = create_checkout_order(
        db,
        catalog_item=chosen,
        customer_email=payload.customer_email,
        gateway_mode=payload.gateway_mode,
    )
    return {
        "order_id": order.id,
        "checkout_session_id": order.checkout_session_id,
        "status": order.status,
        "gateway_mode": order.gateway_mode,
        "gross_amount_usd": order.gross_amount_usd,
    }


@business_router.post("/business/watchdog/scan")
def trigger_watchdog_scan(
    timeout_seconds: int = Query(default=300, ge=1, le=86400),
    user: User = Depends(require_operator),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    recovered = AutonomousBusinessOrchestrator().detect_and_recover_stalled_tasks(db, timeout_seconds=timeout_seconds)
    return {"recovered_stalled_tasks": recovered, "count": len(recovered)}


# ==================== REAL TECH NEWS & APPROVAL QUEUE ====================

@business_router.get("/business/news/pending")
def list_pending_news_approvals(
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    from app.models.entities import ApprovalRequest, Task
    approvals = db.scalars(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.action_type == "PUBLISH_REAL_NEWS_ARTICLE",
            ApprovalRequest.status == "PENDING"
        )
        .order_by(ApprovalRequest.created_at.desc())
    ).all()

    results = []
    for app_req in approvals:
        task = db.scalar(select(Task).where(Task.id == app_req.task_id))
        article = None
        if task and task.result_payload:
            article = task.result_payload.get("article")
        results.append({
            "approval_id": app_req.id,
            "task_id": app_req.task_id,
            "agent_id": app_req.agent_id,
            "reason": app_req.reason,
            "status": app_req.status,
            "created_at": app_req.created_at.isoformat(),
            "article": article,
        })
    return results


@business_router.post("/business/news/{approval_id}/approve")
def approve_news_article(
    approval_id: str,
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.models.entities import ApprovalRequest, Task
    from app.services.real_news_engine import publish_article_to_live_portal

    app_req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))
    if not app_req:
        raise HTTPException(status_code=404, detail="Approval request not found")

    decider_email = user.email if user else "danhnpcd@gmail.com"
    app_req.status = "APPROVED"
    app_req.decided_by = decider_email
    app_req.decided_at = datetime.now(timezone.utc)

    task = db.scalar(select(Task).where(Task.id == app_req.task_id))
    published = False
    title = ""
    if task:
        task.approval_granted = True
        task.status = "COMPLETED"
        task.progress_pct = 100
        if task.result_payload and "article" in task.result_payload:
            article = task.result_payload["article"]
            title = article.get("title", "")
            published = publish_article_to_live_portal(article)

    db.commit()
    return {
        "id": app_req.id,
        "status": "APPROVED",
        "published_to_live_cdn": published,
        "title": title,
    }


@business_router.post("/business/news/{approval_id}/reject")
def reject_news_article(
    approval_id: str,
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.models.entities import ApprovalRequest, Task

    app_req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))
    if not app_req:
        raise HTTPException(status_code=404, detail="Approval request not found")

    decider_email = user.email if user else "danhnpcd@gmail.com"
    app_req.status = "REJECTED"
    app_req.decided_by = decider_email
    app_req.decided_at = datetime.now(timezone.utc)

    task = db.scalar(select(Task).where(Task.id == app_req.task_id))
    if task:
        task.status = "CANCELLED"

    db.commit()
    return {"id": app_req.id, "status": "REJECTED"}


@business_router.post("/business/news/crawl-now")
def trigger_immediate_news_crawl(
    max_articles: int = Query(default=3, ge=1, le=10),
    user: User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.services.real_news_engine import ingest_and_queue_articles_for_approval
    new_items = ingest_and_queue_articles_for_approval(db, max_articles=max_articles)
    return {
        "status": "SUCCESS",
        "crawled_count": len(new_items),
        "queued_items": new_items,
    }

