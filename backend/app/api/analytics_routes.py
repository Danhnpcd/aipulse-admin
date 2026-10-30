import hashlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.entities import RealAnalyticsEvent
from app.security.auth import api_rate_limiter

analytics_router = APIRouter(prefix="/api/analytics", tags=["Real Traffic & Reader Analytics"], dependencies=[Depends(api_rate_limiter)])


class TrackViewRequest(BaseModel):
    url_path: str = Field(default="/", max_length=256)
    article_id: int | None = None
    project_slug: str | None = None
    referrer: str | None = None
    device_type: str = Field(default="MOBILE", pattern="^(MOBILE|DESKTOP|TABLET)$")
    dwell_seconds: int = Field(default=0, ge=0, le=7200)


class TrackInteractionRequest(BaseModel):
    event_type: str = Field(..., pattern="^(LIKE|SHARE|DEAL_CLICK|AFFILIATE_CLICK|MODAL_OPEN)$")
    url_path: str = Field(default="/", max_length=256)
    article_id: int | None = None
    project_slug: str | None = None


def _get_visitor_hash(request: Request) -> str:
    ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for") or (request.client.host if request.client else "unknown")
    ua = request.headers.get("user-agent") or "unknown"
    seed = f"{ip.split(',')[0].strip()}:{ua[:64]}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


@analytics_router.post("/track-view")
def track_real_view(
    payload: TrackViewRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    vis_hash = _get_visitor_hash(request)
    ev_type = "ARTICLE_READ" if payload.article_id else "PAGE_VIEW"

    ev = RealAnalyticsEvent(
        event_type=ev_type,
        url_path=payload.url_path,
        article_id=payload.article_id,
        project_slug=payload.project_slug or "aipulse-news",
        visitor_hash=vis_hash,
        referrer=payload.referrer[:250] if payload.referrer else None,
        device_type=payload.device_type,
        dwell_seconds=payload.dwell_seconds,
    )
    db.add(ev)
    db.commit()

    # Get updated total real views for this article
    art_views = 0
    if payload.article_id:
        art_views = db.scalar(
            select(func.count(RealAnalyticsEvent.id)).where(
                RealAnalyticsEvent.article_id == payload.article_id,
                RealAnalyticsEvent.event_type == "ARTICLE_READ",
            )
        ) or 0

    return {
        "ok": True,
        "recorded": True,
        "event_type": ev_type,
        "article_id": payload.article_id,
        "article_total_views": int(art_views),
    }


@analytics_router.post("/track-interaction")
def track_real_interaction(
    payload: TrackInteractionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    vis_hash = _get_visitor_hash(request)
    ev = RealAnalyticsEvent(
        event_type=payload.event_type,
        url_path=payload.url_path,
        article_id=payload.article_id,
        project_slug=payload.project_slug or "aipulse-news",
        visitor_hash=vis_hash,
    )
    db.add(ev)
    db.commit()

    total_likes = 0
    if payload.article_id and payload.event_type == "LIKE":
        total_likes = db.scalar(
            select(func.count(RealAnalyticsEvent.id)).where(
                RealAnalyticsEvent.article_id == payload.article_id,
                RealAnalyticsEvent.event_type == "LIKE",
            )
        ) or 0

    return {"ok": True, "event_type": payload.event_type, "article_total_likes": int(total_likes)}


@analytics_router.get("/overview")
def get_analytics_overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Returns actual measured readers and interactions (zero fabricated data)."""
    total_views = db.scalar(
        select(func.count(RealAnalyticsEvent.id)).where(
            RealAnalyticsEvent.event_type.in_(["PAGE_VIEW", "ARTICLE_READ"])
        )
    ) or 0

    unique_visitors = db.scalar(
        select(func.count(func.distinct(RealAnalyticsEvent.visitor_hash)))
    ) or 0

    total_likes = db.scalar(
        select(func.count(RealAnalyticsEvent.id)).where(RealAnalyticsEvent.event_type == "LIKE")
    ) or 0

    deal_clicks = db.scalar(
        select(func.count(RealAnalyticsEvent.id)).where(RealAnalyticsEvent.event_type == "DEAL_CLICK")
    ) or 0

    device_counts = db.execute(
        select(RealAnalyticsEvent.device_type, func.count(RealAnalyticsEvent.id))
        .group_by(RealAnalyticsEvent.device_type)
    ).all()

    top_articles = db.execute(
        select(RealAnalyticsEvent.article_id, func.count(RealAnalyticsEvent.id).label("cnt"))
        .where(RealAnalyticsEvent.article_id.is_not(None))
        .group_by(RealAnalyticsEvent.article_id)
        .order_by(func.count(RealAnalyticsEvent.id).desc())
        .limit(5)
    ).all()

    return {
        "total_real_views": int(total_views),
        "total_unique_visitors": int(unique_visitors),
        "total_likes": int(total_likes),
        "deal_conversions_clicks": int(deal_clicks),
        "devices": {k: int(v) for k, v in device_counts},
        "top_articles": [{"article_id": r[0], "views": int(r[1])} for r in top_articles if r[0]],
    }
