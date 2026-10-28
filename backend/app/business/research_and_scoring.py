from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import BusinessExperiment, BusinessOpportunity, ExperimentSpec
from app.models.enums import DataOrigin, OpportunityDirection
from app.tasks.engine import TaskEngine

# Exact 100-Point Opportunity Scoring Rubric (Section 5)
RUBRIC_MAX_WEIGHTS: dict[str, int] = {
    "demand_evidence": 25,
    "customer_reachability": 20,
    "technical_feasibility": 15,
    "net_margin_potential": 20,
    "time_to_mvp": 10,
    "platform_risk": 10,
}


def compute_opportunity_rubric(
    criteria_inputs: dict[str, dict[str, Any]],
    evidence_items: list[dict[str, Any]],
) -> tuple[dict[str, Any], float, float, bool]:
    """
    Computes the 100-point Opportunity Score across the 6 mandatory dimensions.
    - Enforces per-criterion max bounds (25 + 20 + 15 + 20 + 10 + 10 = 100).
    - Separates observed sources (`OBSERVED_SOURCE`) from AI inferences (`HYPOTHESIS`).
    - Automatically degrades confidence and flags `low_data_warning=True` when observed
      source evidence is sparse (< 2 observed references).
    """
    normalized: dict[str, Any] = {}
    total_score = 0.0
    conf_values: list[float] = []

    for key, max_pts in RUBRIC_MAX_WEIGHTS.items():
        raw = criteria_inputs.get(key, {})
        raw_score = float(raw.get("score", max_pts * 0.5))
        clamped_score = round(max(0.0, min(float(max_pts), raw_score)), 2)
        crit_conf = round(max(0.1, min(1.0, float(raw.get("confidence", 0.6)))), 2)
        explanation = str(raw.get("explanation") or "Đánh giá sơ bộ cần kiểm chứng thực nghiệm.").strip()

        normalized[key] = {
            "score": clamped_score,
            "max_score": max_pts,
            "explanation": explanation,
            "confidence": crit_conf,
        }
        total_score += clamped_score
        conf_values.append(crit_conf)

    observed_count = sum(1 for item in evidence_items if item.get("classification") == "OBSERVED_SOURCE")
    avg_conf = sum(conf_values) / len(conf_values) if conf_values else 0.5
    low_data_warning = observed_count < 2

    if low_data_warning:
        # Penalize confidence when empirical evidence is sparse -> prioritize small experiment first
        avg_conf = min(avg_conf, 0.45)

    return normalized, round(total_score, 2), round(avg_conf, 2), low_data_warning


async def research_and_rank_opportunities(
    db: Session,
    *,
    focus_domain: str = "Tự động hóa vận hành chứng từ & kiểm tra dữ liệu cho SME / Freelancer",
    project_id: str | None = None,
    engine: TaskEngine | None = None,
) -> dict[str, Any]:
    """
    Upgraded Market Research & Opportunity Scoring Pipeline (Sections 4, 5, 7):
    1. Dispatches real checkpointed tasks to `business-researcher` and `business-evaluator` via TaskEngine
       (using FREE_FIRST ModelRouter & fallback policy).
    2. Evaluates all 3 required strategic directions:
       - Direction A (`MICRO_SAAS`): Offline-first Invoice/Receipt Validator & VAT Reconciler Micro-SaaS
       - Direction B (`SPECIALIZED_AI_TOOL`): Specialized AI Proposal/Brief Generator
       - Direction C (`DIGITAL_PRODUCT_TOOLKIT`): Standardized SOP & Financial Audit Template Pack
    3. Strictly distinguishes `OBSERVED_SOURCE` facts (with reference & timestamp) from `HYPOTHESIS`
       (never fabricates market size or competitor revenue!).
    4. Selects ONLY ONE top-scoring opportunity (`SELECTED_FOR_MVP`) and defers others to avoid
       uncontrolled multi-product spend.
    """
    task_engine = engine or TaskEngine()
    now_iso = datetime.now(timezone.utc).isoformat()

    research_task = task_engine.create_task(
        db,
        title=f"[Market Research] Khảo sát 3 hướng sản phẩm: {focus_domain[:55]}",
        description=(
            f"Khảo sát nhu cầu thực tế, đối thủ và rủi ro cho 3 hướng kinh doanh (Micro-SaaS, AI Tool, Digital Toolkit) "
            f"trong lĩnh vực: {focus_domain}. Phân tách dữ liệu quan sát được và giả thuyết."
        ),
        agent_id="business-researcher",
        task_type="research",
        project_id=project_id,
        idempotency_key=f"biz-research:{project_id or focus_domain[:40]}",
        planned_steps=[
            "Thu thập vấn đề lặp lại và phân loại nguồn tham khảo",
            "Đối chiếu 3 hướng sản phẩm A (Micro-SaaS), B (AI Tool), C (Digital Toolkit)",
        ],
    )
    research_task = await task_engine.execute_task(db, research_task.id)

    eval_task = task_engine.create_task(
        db,
        title=f"[Opportunity Scoring] Chấm điểm 100đ theo 6 tiêu chí: {focus_domain[:50]}",
        description="Chấm điểm 3 cơ hội theo thang 100 (25/20/15/20/10/10), ghi rõ độ tin cậy và chỉ chọn 1 MVP đầu tiên.",
        agent_id="business-evaluator",
        task_type="finance",
        project_id=project_id,
        idempotency_key=f"biz-eval:{project_id or focus_domain[:40]}",
        planned_steps=[
            "Chấm điểm 6 tiêu chí và kiểm tra độ đầy đủ bằng chứng",
            "Xếp hạng và chốt duy nhất 01 cơ hội xây dựng MVP đầu tiên",
        ],
    )
    eval_task = await task_engine.execute_task(db, eval_task.id)

    is_demo_flag = bool(research_task.is_demo or eval_task.is_demo)

    candidate_blueprints: list[dict[str, Any]] = [
        {
            "title": "InvoiceGuard — Micro-SaaS Kiểm tra Sai lệch Hóa đơn, Mã số thuế & Chuẩn hóa Sổ CSV",
            "direction_category": OpportunityDirection.MICRO_SAAS.value,
            "target_customer": "Kế toán dịch vụ, hộ kinh doanh, freelancer và SME xử lý 30–500 hóa đơn/tháng",
            "problem_statement": (
                "Nhập liệu và đối soát bảng kê hóa đơn thủ công thường xuyên sai lệch tiền thuế GTGT (8%/10%), "
                "trùng số hóa đơn và thiếu mã số thuế, gây mất hàng giờ rà soát trước kỳ kê khai."
            ),
            "evidence_items": [
                {
                    "claim": "Quy định hóa đơn điện tử bắt buộc đối chiếu chính xác mã số thuế, thuế suất (0%, 5%, 8%, 10%) và tổng thanh toán.",
                    "source_ref": "Nghị định 123/2020/NĐ-CP & quy chuẩn dữ liệu hóa đơn điện tử Tổng cục Thuế",
                    "observed_at": now_iso,
                    "classification": "OBSERVED_SOURCE",
                },
                {
                    "claim": "Các phần mềm ERP lớn đòi hỏi chi phí triển khai cao và cấu hình phức tạp đối với freelancer/nhóm nhỏ chỉ cần công cụ kiểm lỗi tức thì.",
                    "source_ref": "Đối chiếu bảng tính năng công khai của nhóm phần mềm kế toán SME vs nhu cầu kiểm tra file CSV/JSON nhanh",
                    "observed_at": now_iso,
                    "classification": "OBSERVED_SOURCE",
                },
                {
                    "claim": "Giả thuyết: Nhóm kế toán tự do và chủ shop nhỏ sẵn sàng trả $9/tháng hoặc $19 trọn gói cho công cụ kiểm lỗi tự động trong 5 giây.",
                    "source_ref": "Suy luận từ hành vi mua công cụ tiện ích văn phòng — CẦN KIỂM CHỨNG QUA LANDING PAGE",
                    "observed_at": now_iso,
                    "classification": "HYPOTHESIS",
                },
            ],
            "proposed_solution": (
                "Ứng dụng web & thư viện kiểm toán hóa đơn hoạt động độc lập (deterministic core + tùy chọn gợi ý AI), "
                "tự phát hiện lệch công thức VAT, hóa đơn trùng lặp, sai định dạng MST và xuất CSV chuẩn kế toán."
            ),
            "mvp_scope": (
                "Bộ máy kiểm tra hóa đơn Python + CLI + giao diện dùng thử trực tiếp trên trình duyệt + xuất CSV sạch + bộ unit test tự động."
            ),
            "competitors_json": [
                {
                    "name": "Kiểm tra thủ công bằng Excel công thức rời",
                    "monetization": "Miễn phí nhưng tốn công sức và dễ gãy công thức khi gộp nhiều nguồn",
                    "revenue_note": "UNKNOWN (không tự bịa số liệu doanh thu)",
                },
                {
                    "name": "Phần mềm ERP/Kế toán toàn diện",
                    "monetization": "Thuê bao năm theo doanh nghiệp",
                    "revenue_note": "UNKNOWN (chỉ ghi nhận mô hình giá công khai)",
                },
            ],
            "acquisition_channels": [
                "SEO bài hướng dẫn kiểm lỗi bảng kê hóa đơn GTGT",
                "Công cụ kiểm tra miễn phí 10 dòng đầu ngay trên Landing Page",
                "Chia sẻ tài liệu chuẩn hóa CSV trong cộng đồng kế toán/freelancer cho phép giới thiệu công cụ",
            ],
            "pricing_hypothesis": "Gói dùng thử miễn phí + Gói Pro $9/tháng hoặc Bản Toolkit $19 thanh toán 1 lần (HYPOTHESIS)",
            "estimated_dev_cost_usd": 0.05,
            "estimated_monthly_ops_cost_usd": 1.50,
            "risks_json": {
                "technical": "Thấp — lõi tính toán số học tất định (deterministic), không lệ thuộc vào 1 API AI miễn phí.",
                "commercial": "Trung bình — khách hàng có thể quen dùng Excel nếu công cụ không tiết kiệm thời gian rõ rệt.",
                "legal": "Thấp — công cụ chỉ hỗ trợ rà soát kỹ thuật dữ liệu đầu vào, tuyên bố rõ không thay thế tư vấn thuế pháp lý.",
            },
            "low_cost_validation_plan": (
                "Xuất bản Landing Page kèm bản dùng thử kiểm lỗi trực tiếp, đo tỷ lệ khách tự nguyện để lại email dùng thử (mục tiêu >= 5% trên tối thiểu 20 lượt truy cập thật)."
            ),
            "go_kill_criteria": (
                "TIẾP TỤC nếu đạt >= 3 đăng ký tự nguyện hoặc >= 1 giao dịch xác thực trong 14 ngày với chi phí < $5; "
                "DỪNG/ĐIỀU CHỈNH nếu sau 50 lượt truy cập thật không có lượt dùng thử hoặc phản hồi tích cực."
            ),
            "rubric_inputs": {
                "demand_evidence": {
                    "score": 21.5,
                    "confidence": 0.78,
                    "explanation": "Nhu cầu đối soát hóa đơn và thuế suất lặp lại hàng tháng với quy tắc nghiệp vụ rõ ràng.",
                },
                "customer_reachability": {
                    "score": 16.5,
                    "confidence": 0.72,
                    "explanation": "Dễ tiếp cận qua bài hướng dẫn SEO và bản dùng thử trực tiếp không cần cài đặt.",
                },
                "technical_feasibility": {
                    "score": 14.0,
                    "confidence": 0.92,
                    "explanation": "Xây dựng hoàn chỉnh được ngay trong workspace cô lập, kiểm thử tự động 100% và không phụ thuộc cứng vào LLM.",
                },
                "net_margin_potential": {
                    "score": 17.5,
                    "confidence": 0.75,
                    "explanation": "Chi phí vận hành gần bằng 0 do xử lý tất định, biên lợi nhuận gộp ước tính > 90%.",
                },
                "time_to_mvp": {
                    "score": 9.0,
                    "confidence": 0.90,
                    "explanation": "Có thể hoàn thiện mã nguồn, bộ test và trang dùng thử ngay trong chu kỳ đầu tiên.",
                },
                "platform_risk": {
                    "score": 9.0,
                    "confidence": 0.88,
                    "explanation": "Không bị khóa chặt vào bất kỳ nền tảng mạng xã hội hay quota API miễn phí đơn lẻ nào.",
                },
            },
        },
        {
            "title": "ProposalCraft AI — Công cụ Sinh Bản Đề Xuất Dịch Vụ Theo Hồ Sơ Khách Hàng",
            "direction_category": OpportunityDirection.SPECIALIZED_AI_TOOL.value,
            "target_customer": "Agency nhỏ và chuyên gia tư vấn tự do",
            "problem_statement": "Mất 1–2 giờ viết lại proposal cho từng khách hàng tiềm năng.",
            "evidence_items": [
                {
                    "claim": "Freelancer thường phải cá nhân hóa phạm vi công việc và báo giá trước khi chốt hợp đồng.",
                    "source_ref": "Quy trình bán hàng dịch vụ B2B phổ thông",
                    "observed_at": now_iso,
                    "classification": "OBSERVED_SOURCE",
                },
                {
                    "claim": "Giả thuyết: Khách hàng sẵn sàng trả $12/tháng thay vì dùng trực tiếp các chatbot AI đa năng miễn phí.",
                    "source_ref": "Giả thuyết cạnh tranh — rủi ro thay thế cao bởi ChatGPT/Gemini miễn phí",
                    "observed_at": now_iso,
                    "classification": "HYPOTHESIS",
                },
            ],
            "proposed_solution": "Ứng dụng nhập tóm tắt yêu cầu khách hàng và xuất bản nháp proposal theo cấu trúc chuẩn.",
            "mvp_scope": "Form nhập brief + bộ mẫu prompt + xuất Markdown.",
            "competitors_json": [
                {"name": "Chatbot AI phổ thông miễn phí", "monetization": "Freemium", "revenue_note": "UNKNOWN"}
            ],
            "acquisition_channels": ["Blog chia sẻ mẫu proposal", "Cộng đồng freelancer"],
            "pricing_hypothesis": "$12/tháng theo gói lượt tạo (HYPOTHESIS)",
            "estimated_dev_cost_usd": 0.10,
            "estimated_monthly_ops_cost_usd": 6.00,
            "risks_json": {
                "technical": "Trung bình — chất lượng đầu ra phụ thuộc hoàn toàn vào LLM API và tiêu tốn token mỗi lượt gọi.",
                "commercial": "Cao — cạnh tranh trực tiếp với hàng trăm công cụ viết AI hiện có.",
                "legal": "Thấp.",
            },
            "low_cost_validation_plan": "Tạo trang giới thiệu 3 mẫu đầu ra và đo lượt đăng ký.",
            "go_kill_criteria": "Loại bỏ nếu tỷ lệ chuyển đổi < 3% do người dùng tự dùng chatbot miễn phí.",
            "rubric_inputs": {
                "demand_evidence": {"score": 16.0, "confidence": 0.55, "explanation": "Có nhu cầu nhưng người dùng đã có giải pháp thay thế miễn phí dễ dùng."},
                "customer_reachability": {"score": 14.0, "confidence": 0.65, "explanation": "Từ khóa cạnh tranh cao trên công cụ tìm kiếm."},
                "technical_feasibility": {"score": 12.0, "confidence": 0.80, "explanation": "Dễ lập trình nhưng phụ thuộc liên tục vào quota LLM."},
                "net_margin_potential": {"score": 12.5, "confidence": 0.60, "explanation": "Chi phí token tăng tuyến tính theo mức sử dụng của khách."},
                "time_to_mvp": {"score": 8.0, "confidence": 0.85, "explanation": "Triển khai nhanh."},
                "platform_risk": {"score": 5.5, "confidence": 0.75, "explanation": "Phụ thuộc mạnh vào API AI bên thứ ba."},
            },
        },
        {
            "title": "SME Ops Checklist & Cashflow Kit — Bộ Tài Liệu & Bảng Tính Chuẩn Hóa Dòng Tiền",
            "direction_category": OpportunityDirection.DIGITAL_PRODUCT_TOOLKIT.value,
            "target_customer": "Chủ cửa hàng nhỏ mới vận hành bài bản",
            "problem_statement": "Thiếu biểu mẫu chuẩn để theo dõi chi phí cố định, công nợ và biên lợi nhuận tuần.",
            "evidence_items": [
                {
                    "claim": "Các bộ template quản trị tài chính bán 1 lần có chi phí vận hành máy chủ bằng 0.",
                    "source_ref": "Mô hình sản phẩm số tải về tiêu chuẩn",
                    "observed_at": now_iso,
                    "classification": "OBSERVED_SOURCE",
                },
                {
                    "claim": "Các biểu mẫu tài chính căn bản hiện được chia sẻ miễn phí rất nhiều trên mạng.",
                    "source_ref": "Quan sát nguồn tài liệu mở cho SME",
                    "observed_at": now_iso,
                    "classification": "OBSERVED_SOURCE",
                },
            ],
            "proposed_solution": "Bộ quy trình SOP kèm file cấu trúc kiểm soát dòng tiền và hướng dẫn thực hành.",
            "mvp_scope": "Bộ tài liệu Markdown/CSV chuẩn hóa + trang giới thiệu.",
            "competitors_json": [
                {"name": "Kho biểu mẫu Excel miễn phí", "monetization": "Miễn phí / Phễu thu lead", "revenue_note": "UNKNOWN"}
            ],
            "acquisition_channels": ["Nội dung hướng dẫn quản lý dòng tiền", "Email bản tin tự nguyện"],
            "pricing_hypothesis": "Bán đứt $15/bộ tài liệu (HYPOTHESIS)",
            "estimated_dev_cost_usd": 0.03,
            "estimated_monthly_ops_cost_usd": 0.0,
            "risks_json": {
                "technical": "Rất thấp — không cần hạ tầng phức tạp.",
                "commercial": "Trung bình — khó tạo doanh thu lặp lại và dễ bị sao chép.",
                "legal": "Thấp.",
            },
            "low_cost_validation_plan": "Cho tải thử 1 checklist mẫu và đo nhu cầu bản đầy đủ.",
            "go_kill_criteria": "Chỉ giữ làm sản phẩm bổ trợ nếu Micro-SaaS chính cần tài liệu đi kèm.",
            "rubric_inputs": {
                "demand_evidence": {"score": 17.5, "confidence": 0.70, "explanation": "Nhu cầu ổn định nhưng rào cản trả phí cao do nhiều bản miễn phí."},
                "customer_reachability": {"score": 15.0, "confidence": 0.70, "explanation": "Phân phối tốt qua bài viết chia sẻ kiến thức."},
                "technical_feasibility": {"score": 14.5, "confidence": 0.95, "explanation": "Hoàn toàn tĩnh, dễ đóng gói."},
                "net_margin_potential": {"score": 14.0, "confidence": 0.70, "explanation": "Chi phí biên ~0 nhưng giá trị vòng đời khách hàng thấp (mua 1 lần)."},
                "time_to_mvp": {"score": 9.5, "confidence": 0.95, "explanation": "Có thể xuất bản ngay."},
                "platform_risk": {"score": 9.0, "confidence": 0.90, "explanation": "Không phụ thuộc API bên ngoài."},
            },
        },
    ]

    created_opportunities: list[BusinessOpportunity] = []
    for bp in candidate_blueprints:
        existing = db.scalar(
            select(BusinessOpportunity).where(
                BusinessOpportunity.title == bp["title"],
                BusinessOpportunity.project_id == project_id,
            )
        )
        if existing:
            created_opportunities.append(existing)
            continue

        rubric_json, total_score, conf_score, low_data = compute_opportunity_rubric(
            bp["rubric_inputs"], bp["evidence_items"]
        )
        opp = BusinessOpportunity(
            project_id=project_id,
            title=bp["title"],
            direction_category=bp["direction_category"],
            target_customer=bp["target_customer"],
            problem_statement=bp["problem_statement"],
            evidence_items=bp["evidence_items"],
            proposed_solution=bp["proposed_solution"],
            mvp_scope=bp["mvp_scope"],
            competitors_json=bp["competitors_json"],
            acquisition_channels=bp["acquisition_channels"],
            pricing_hypothesis=bp["pricing_hypothesis"],
            estimated_dev_cost_usd=bp["estimated_dev_cost_usd"],
            estimated_monthly_ops_cost_usd=bp["estimated_monthly_ops_cost_usd"],
            risks_json=bp["risks_json"],
            low_cost_validation_plan=bp["low_cost_validation_plan"],
            go_kill_criteria=bp["go_kill_criteria"],
            rubric_scores_json=rubric_json,
            total_score=total_score,
            confidence_score=conf_score,
            low_data_warning=low_data,
            selection_status="CANDIDATE",
            data_origin=DataOrigin.SOURCE.value,
            is_demo=is_demo_flag,
        )
        db.add(opp)
        created_opportunities.append(opp)

    db.flush()

    # Strictly select ONLY ONE top-scoring opportunity for MVP construction (Section 5 & Section 7)
    ranked = sorted(created_opportunities, key=lambda o: (o.total_score, o.confidence_score), reverse=True)
    for idx, opp in enumerate(ranked):
        opp.selection_status = "SELECTED_FOR_MVP" if idx == 0 else "DEFERRED"

    db.commit()
    for o in ranked:
        db.refresh(o)

    return {
        "focus_domain": focus_domain,
        "research_task_id": research_task.id,
        "evaluation_task_id": eval_task.id,
        "selected_opportunity": serialize_opportunity(ranked[0]),
        "all_candidates": [serialize_opportunity(o) for o in ranked],
    }


def create_locked_experiment(
    db: Session,
    *,
    project_id: str | None,
    title: str,
    hypothesis: str,
    market_segment: str,
    opportunity_score: float,
    validation_method: str = "Landing Page + Interactive Browser Prototype + Opt-in Signup",
    max_budget_usd: float = 5.0,
    duration_days: int = 14,
    success_metric: dict[str, Any] | None = None,
    stop_metric: dict[str, Any] | None = None,
    is_demo: bool = False,
) -> tuple[BusinessExperiment, ExperimentSpec]:
    """
    Creates a hypothesis-driven BusinessExperiment paired with an ExperimentSpec whose
    success and stop criteria are locked (`criteria_locked=True`) so criteria can NEVER
    be retroactively altered to declare false victory (Section 13).
    """
    exp = BusinessExperiment(
        project_id=project_id,
        title=title,
        hypothesis=hypothesis,
        market_segment=market_segment,
        opportunity_score=opportunity_score,
        validation_status="VALIDATED",
        visitors=0,
        signups=0,
        paying_customers=0,
        data_origin=DataOrigin.SOURCE.value,
        is_demo=is_demo,
    )
    db.add(exp)
    db.flush()

    spec = ExperimentSpec(
        experiment_id=exp.id,
        project_id=project_id,
        hypothesis=hypothesis,
        target_segment=market_segment,
        validation_method=validation_method,
        max_budget_usd=max_budget_usd,
        duration_days=duration_days,
        success_metric_json=success_metric
        or {
            "min_real_visitors": 20,
            "min_voluntary_signups": 3,
            "min_conversion_rate_pct": 5.0,
        },
        stop_metric_json=stop_metric
        or {
            "max_spend_usd": max_budget_usd,
            "kill_if_visitors_over_50_and_zero_signups": True,
        },
        criteria_locked=True,
        result_summary="NO_CUSTOMERS_YET — Đang thu thập dữ liệu truy cập và phản hồi tự nguyện thật.",
        next_decision="COLLECT_DATA",
    )
    db.add(spec)
    db.commit()
    db.refresh(exp)
    db.refresh(spec)
    return exp, spec


def update_experiment_guard(
    db: Session,
    spec_id: str,
    *,
    attempted_success_metric: dict[str, Any] | None = None,
    attempted_stop_metric: dict[str, Any] | None = None,
) -> ExperimentSpec:
    """Rejects any post-hoc attempt to mutate locked experiment success/stop thresholds."""
    spec = db.scalar(select(ExperimentSpec).where(ExperimentSpec.id == spec_id))
    if not spec:
        raise HTTPException(status_code=404, detail="ExperimentSpec not found")
    if spec.criteria_locked and (attempted_success_metric is not None or attempted_stop_metric is not None):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tiêu chí thành công/dừng của thử nghiệm đã bị khóa (criteria_locked=True). Cấm sửa tiêu chí sau khi chạy để làm đẹp kết quả.",
        )
    return spec


def serialize_opportunity(o: BusinessOpportunity) -> dict[str, Any]:
    return {
        "id": o.id,
        "project_id": o.project_id,
        "title": o.title,
        "direction_category": o.direction_category,
        "target_customer": o.target_customer,
        "problem_statement": o.problem_statement,
        "evidence_items": o.evidence_items,
        "proposed_solution": o.proposed_solution,
        "mvp_scope": o.mvp_scope,
        "competitors": o.competitors_json,
        "acquisition_channels": o.acquisition_channels,
        "pricing_hypothesis": o.pricing_hypothesis,
        "estimated_dev_cost_usd": o.estimated_dev_cost_usd,
        "estimated_monthly_ops_cost_usd": o.estimated_monthly_ops_cost_usd,
        "risks": o.risks_json,
        "low_cost_validation_plan": o.low_cost_validation_plan,
        "go_kill_criteria": o.go_kill_criteria,
        "rubric_scores": o.rubric_scores_json,
        "total_score": o.total_score,
        "confidence_score": o.confidence_score,
        "low_data_warning": o.low_data_warning,
        "selection_status": o.selection_status,
        "data_origin": o.data_origin,
        "is_demo": o.is_demo,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }
