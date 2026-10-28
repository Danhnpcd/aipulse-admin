from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import (
    AIModel,
    Agent,
    BusinessExperiment,
    CostRecord,
    Project,
    Provider,
    RevenueRecord,
    SystemSetting,
    Task,
    TaskEvent,
    UsageRecord,
)
from app.models.enums import EventType, TaskStatus


def get_system_status_report(db: Session) -> dict[str, Any]:
    settings_row = db.scalar(select(SystemSetting).where(SystemSetting.id == "singleton"))
    now = datetime.now(timezone.utc)
    uptime_seconds = 0
    if settings_row and settings_row.started_at:
        started = settings_row.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        uptime_seconds = max(0, int((now - started).total_seconds()))

    agents = db.scalars(select(Agent)).all()
    active_agents = sum(1 for a in agents if a.status == "WORKING")

    running_tasks = db.scalar(select(func.count(Task.id)).where(Task.status == TaskStatus.RUNNING.value)) or 0
    failed_tasks = db.scalar(select(func.count(Task.id)).where(Task.status == TaskStatus.FAILED.value)) or 0
    waiting_tasks = (
        db.scalar(
            select(func.count(Task.id)).where(
                Task.status.in_(
                    [
                        TaskStatus.QUEUED.value,
                        TaskStatus.CREATED.value,
                        TaskStatus.WAITING_FOR_PROVIDER.value,
                        TaskStatus.WAITING_FOR_APPROVAL.value,
                        TaskStatus.PAUSED.value,
                    ]
                )
            )
        )
        or 0
    )
    completed_tasks = db.scalar(select(func.count(Task.id)).where(Task.status == TaskStatus.COMPLETED.value)) or 0

    latest_err_provider = db.scalar(
        select(Provider).where(Provider.last_error.is_not(None)).order_by(Provider.last_checked_at.desc()).limit(1)
    )

    return {
        "active_agents": active_agents,
        "total_agents": len(agents),
        "running_tasks": running_tasks,
        "failed_tasks": failed_tasks,
        "waiting_tasks": waiting_tasks,
        "completed_tasks": completed_tasks,
        "uptime_seconds": uptime_seconds,
        "demo_mode": bool(settings_row.demo_mode) if settings_row else True,
        "automation_level": settings_row.automation_level if settings_row else "LEVEL_2_SANDBOX",
        "routing_mode": settings_row.routing_mode if settings_row else "FREE_FIRST",
        "system_paused": bool(settings_row.system_paused) if settings_row else False,
        "emergency_stop": bool(settings_row.emergency_stop) if settings_row else False,
        "latest_provider_error": (
            {
                "provider_id": latest_err_provider.id,
                "error": latest_err_provider.last_error,
                "checked_at": latest_err_provider.last_checked_at.isoformat()
                if latest_err_provider.last_checked_at
                else None,
            }
            if latest_err_provider
            else None
        ),
        "updated_at": now.isoformat(),
    }


def get_usage_analytics(db: Session) -> dict[str, Any]:
    total_calls = db.scalar(select(func.count(UsageRecord.id))) or 0
    successful_calls = db.scalar(select(func.count(UsageRecord.id)).where(UsageRecord.success.is_(True))) or 0
    failed_calls = total_calls - successful_calls
    in_tokens = db.scalar(select(func.coalesce(func.sum(UsageRecord.input_tokens), 0))) or 0
    out_tokens = db.scalar(select(func.coalesce(func.sum(UsageRecord.output_tokens), 0))) or 0

    by_model_rows = db.execute(
        select(
            UsageRecord.provider_id,
            UsageRecord.model_id,
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0),
            func.sum(func.cast(UsageRecord.success.is_(False), UsageRecord.latency_ms.type)),
        ).group_by(UsageRecord.provider_id, UsageRecord.model_id)
    ).all()

    models = db.scalars(select(AIModel)).all()
    quota_overview = [
        {
            "provider_id": m.provider_id,
            "model_id": m.model_id,
            "tier": m.tier,
            "quota_status": m.quota_status,
            "quota_confidence": m.quota_confidence,
            "quota_remaining": m.quota_remaining,
            "last_checked_at": m.last_checked_at.isoformat() if m.last_checked_at else None,
        }
        for m in models
    ]

    switch_events = db.scalars(
        select(TaskEvent)
        .where(TaskEvent.event_type == EventType.PROVIDER_SWITCHED.value)
        .order_by(TaskEvent.seq.desc())
        .limit(25)
    ).all()

    error_providers = db.scalars(
        select(Provider).where(Provider.error_count > 0).order_by(Provider.error_count.desc())
    ).all()

    return {
        "total_calls": total_calls,
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "total_input_tokens": int(in_tokens),
        "total_output_tokens": int(out_tokens),
        "by_model": [
            {
                "provider_id": r[0],
                "model_id": r[1],
                "calls": int(r[2]),
                "input_tokens": int(r[3]),
                "output_tokens": int(r[4]),
                "failures": int(r[5] or 0),
            }
            for r in by_model_rows
        ],
        "quota_overview": quota_overview,
        "switch_history": [
            {
                "event_id": ev.event_id,
                "timestamp": ev.timestamp.isoformat() if ev.timestamp else None,
                **ev.payload,
            }
            for ev in switch_events
        ],
        "frequent_error_providers": [
            {
                "provider_id": p.id,
                "name": p.name,
                "error_count": p.error_count,
                "health_status": p.health_status,
                "last_error": p.last_error,
            }
            for p in error_providers
        ],
        "source": "SQLite/PostgreSQL Usage Ledger",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_cost_analytics(db: Session) -> dict[str, Any]:
    settings_row = db.scalar(select(SystemSetting).where(SystemSetting.id == "singleton"))
    ai_cost_prod = (
        db.scalar(
            select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(
                CostRecord.category == "AI_API", CostRecord.is_demo.is_(False)
            )
        )
        or 0.0
    )
    ai_cost_demo = (
        db.scalar(
            select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(
                CostRecord.category == "AI_API", CostRecord.is_demo.is_(True)
            )
        )
        or 0.0
    )
    infra_cost = (
        db.scalar(
            select(func.coalesce(func.sum(CostRecord.amount_usd), 0.0)).where(
                CostRecord.category == "INFRASTRUCTURE"
            )
        )
        or 0.0
    )

    by_model = db.execute(
        select(
            CostRecord.provider_id,
            CostRecord.model_id,
            CostRecord.cost_confidence,
            CostRecord.is_demo,
            func.coalesce(func.sum(CostRecord.amount_usd), 0.0),
        ).group_by(CostRecord.provider_id, CostRecord.model_id, CostRecord.cost_confidence, CostRecord.is_demo)
    ).all()

    return {
        "ai_cost_production_usd": round(float(ai_cost_prod), 6),
        "ai_cost_demo_simulated_usd": round(float(ai_cost_demo), 6),
        "infrastructure_cost_usd": round(float(infra_cost), 4),
        "daily_budget_usd": settings_row.daily_budget_usd if settings_row else 5.0,
        "monthly_budget_usd": settings_row.monthly_budget_usd if settings_row else 50.0,
        "by_model": [
            {
                "provider_id": r[0] or "system",
                "model_id": r[1] or "infra",
                "cost_confidence": r[2],
                "is_demo": bool(r[3]),
                "amount_usd": round(float(r[4]), 6),
            }
            for r in by_model
        ],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_business_analytics(db: Session) -> dict[str, Any]:
    """
    Strictly separates production verified revenue from test/sandbox/demo entries.
    Never fabricates production revenue!
    """
    settings_row = db.scalar(select(SystemSetting).where(SystemSetting.id == "singleton"))
    experiments = db.scalars(select(BusinessExperiment).order_by(BusinessExperiment.created_at.desc())).all()
    projects = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
    revenues = db.scalars(select(RevenueRecord).order_by(RevenueRecord.recorded_at.desc())).all()

    # Confirmed production revenue strictly excludes test payments AND demo records
    confirmed_prod_rows = [
        r for r in revenues if r.verified and not r.is_test_payment and not r.is_demo
    ]
    demo_or_test_rows = [r for r in revenues if r.is_test_payment or r.is_demo]

    gross_confirmed = sum(r.gross_amount_usd for r in confirmed_prod_rows)
    net_confirmed = sum(r.net_received_usd for r in confirmed_prod_rows)
    refunds_confirmed = sum(r.refund_amount_usd for r in confirmed_prod_rows)

    demo_gross = sum(r.gross_amount_usd for r in demo_or_test_rows)

    costs = get_cost_analytics(db)
    operating_cost = costs["ai_cost_production_usd"] + costs["infrastructure_cost_usd"]
    estimated_profit = net_confirmed - refunds_confirmed - operating_cost

    return {
        "demo_mode_active": bool(settings_row.demo_mode) if settings_row else True,
        "ideas_researched": sum(1 for e in experiments if e.validation_status in ("RESEARCHED", "VALIDATED", "BUILDING", "LIVE")),
        "ideas_validated": sum(1 for e in experiments if e.validation_status in ("VALIDATED", "BUILDING", "LIVE")),
        "products_in_development": sum(1 for p in projects if p.stage in ("RESEARCH", "DEVELOPMENT", "QA")),
        "products_live_or_sandbox": sum(1 for p in projects if p.stage in ("SANDBOX_READY", "LIVE")),
        "total_visitors": sum(e.visitors for e in experiments if not e.is_demo),
        "demo_visitors": sum(e.visitors for e in experiments if e.is_demo),
        "total_signups": sum(e.signups for e in experiments if not e.is_demo),
        "demo_signups": sum(e.signups for e in experiments if e.is_demo),
        "paying_customers_confirmed": sum(e.paying_customers for e in experiments if not e.is_demo),
        "financials": {
            "confirmed_gross_revenue_usd": round(gross_confirmed, 2),
            "confirmed_net_received_usd": round(net_confirmed, 2),
            "confirmed_refunds_usd": round(refunds_confirmed, 2),
            "sandbox_or_test_payments_usd": round(demo_gross, 2),
            "operating_cost_usd": round(operating_cost, 4),
            "estimated_profit_usd": round(estimated_profit, 4),
        },
        "experiments": [
            {
                "id": e.id,
                "title": e.title,
                "market_segment": e.market_segment,
                "opportunity_score": e.opportunity_score,
                "validation_status": e.validation_status,
                "visitors": e.visitors,
                "signups": e.signups,
                "paying_customers": e.paying_customers,
                "data_origin": e.data_origin,
                "is_demo": e.is_demo,
            }
            for e in experiments
        ],
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "slug": p.slug,
                "stage": p.stage,
                "status": p.status,
                "opportunity_score": p.opportunity_score,
                "qa_passed": p.qa_passed,
                "has_sandbox_preview": bool(p.sandbox_html),
                "data_origin": p.data_origin,
                "is_demo": p.is_demo,
            }
            for p in projects
        ],
        "revenue_ledger": [
            {
                "id": r.id,
                "description": r.description,
                "gross_amount_usd": r.gross_amount_usd,
                "net_received_usd": r.net_received_usd,
                "refund_amount_usd": r.refund_amount_usd,
                "is_test_payment": r.is_test_payment,
                "is_demo": r.is_demo,
                "verified": r.verified,
                "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
            }
            for r in revenues
        ],
        "autonomous_engine": _build_autonomous_room_telemetry(db, costs),
    }


def _build_autonomous_room_telemetry(db: Session, costs: dict[str, Any]) -> dict[str, Any]:
    from app.business.monetization_and_economics import (
        calculate_unit_economics,
        get_customer_validation_summary,
    )
    from app.business.research_and_scoring import serialize_opportunity
    from app.models.entities import (
        BusinessOpportunity,
        DeploymentRecord,
        MarketingCampaignItem,
        OrchestratorDecision,
        ProductArtifact,
    )

    non_demo_project = db.scalar(
        select(Project).where(Project.is_demo.is_(False)).order_by(Project.created_at.desc()).limit(1)
    )
    active_proj = non_demo_project or db.scalar(select(Project).order_by(Project.created_at.desc()).limit(1))

    opportunities = db.scalars(
        select(BusinessOpportunity).order_by(BusinessOpportunity.total_score.desc()).limit(6)
    ).all()
    products = db.scalars(select(ProductArtifact).order_by(ProductArtifact.created_at.desc()).limit(5)).all()
    deployments = db.scalars(select(DeploymentRecord).order_by(DeploymentRecord.deployed_at.desc()).limit(5)).all()
    mkt_items = db.scalars(
        select(MarketingCampaignItem).order_by(MarketingCampaignItem.created_at.desc()).limit(8)
    ).all()
    decisions = db.scalars(
        select(OrchestratorDecision).order_by(OrchestratorDecision.created_at.desc()).limit(12)
    ).all()
    blocking_tasks = db.scalars(
        select(Task)
        .where(
            Task.status.in_(
                [
                    TaskStatus.FAILED.value,
                    TaskStatus.WAITING_FOR_PROVIDER.value,
                    TaskStatus.WAITING_FOR_APPROVAL.value,
                ]
            )
        )
        .order_by(Task.updated_at.desc())
        .limit(6)
    ).all()

    daily_budget = float(costs.get("daily_budget_usd", 5.0))
    remaining_budget = round(max(0.0, daily_budget - float(costs.get("ai_cost_production_usd", 0.0))), 4)

    return {
        "active_project": (
            {
                "id": active_proj.id,
                "name": active_proj.name,
                "slug": active_proj.slug,
                "stage": active_proj.stage,
                "status": active_proj.status,
                "qa_passed": active_proj.qa_passed,
                "is_demo": active_proj.is_demo,
            }
            if active_proj
            else None
        ),
        "remaining_daily_budget_usd": remaining_budget,
        "blocking_issues": [
            {"task_id": t.id, "title": t.title, "status": t.status, "error": t.last_error}
            for t in blocking_tasks
        ],
        "opportunities": [serialize_opportunity(o) for o in opportunities],
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "slug": p.slug,
                "version": p.version,
                "build_status": p.build_status,
                "test_passed": p.test_passed,
                "test_total": p.test_total,
                "test_failures": p.test_failures,
                "files_count": len(p.files_manifest_json or []),
                "landing_page_url": p.landing_page_url,
            }
            for p in products
        ],
        "deployments": [
            {
                "id": d.id,
                "version": d.version,
                "environment": d.environment,
                "status": d.status,
                "rollback_version": d.rollback_version,
            }
            for d in deployments
        ],
        "marketing_items": [
            {
                "id": m.id,
                "channel": m.channel,
                "title": m.title,
                "publish_status": m.publish_status,
                "impressions": m.impressions,
                "conversions": m.conversions,
            }
            for m in mkt_items
        ],
        "decisions": [
            {
                "id": d.id,
                "stage_from": d.stage_from,
                "stage_to": d.stage_to,
                "decision_type": d.decision_type,
                "rationale": d.rationale,
                "outcome": d.execution_outcome,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in decisions
        ],
        "customer_validation": get_customer_validation_summary(db),
        "unit_economics": calculate_unit_economics(db),
    }
