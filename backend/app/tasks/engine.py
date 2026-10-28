from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.registry import set_agent_status
from app.core.config import get_settings
from app.events.bus import emit_notification, event_bus
from app.models.entities import (
    Agent,
    ApprovalRequest,
    Checkpoint,
    SystemSetting,
    Task,
)
from app.models.enums import (
    AgentStatus,
    AutomationLevel,
    DataOrigin,
    EventType,
    NotificationType,
    TaskStatus,
)
from app.providers.adapters import ProviderError, ProviderRequest
from app.providers.manager import ModelRouter, NoAvailableModelError

DEFAULT_STEPS = [
    "Chuẩn hóa yêu cầu & kiểm tra quyền hạn",
    "Thực thi phân tích chuyên môn & tổng hợp giải pháp",
    "Kiểm định đầu ra & ghi nhận báo cáo hoàn tất",
]


class SideEffectLedger:
    """
    Tracks external side-effect invocations by idempotency token so retries or mid-task
    provider switches NEVER execute side-effect operations twice.
    """

    def __init__(self) -> None:
        self.execution_counts: dict[str, int] = {}

    def run_once(self, effect_key: str, callback: Callable[[], Any] | None = None) -> bool:
        if effect_key in self.execution_counts:
            return False
        self.execution_counts[effect_key] = 1
        if callback:
            callback()
        return True


side_effect_ledger = SideEffectLedger()


class TaskEngine:
    """
    Durable, checkpoint-driven Task Engine.
    - Progress percentage strictly equals verified completed_steps / total_steps.
    - Supports model failover mid-task from neutral JSON checkpoints.
    - Enforces Emergency Stop, Automation Levels, Budget thresholds, Approval workflow, and Idempotency.
    """

    def __init__(self, router: ModelRouter | None = None):
        self.router = router or ModelRouter()

    def _get_settings(self, db: Session) -> SystemSetting:
        return self.router.get_or_create_settings(db)

    def create_task(
        self,
        db: Session,
        *,
        title: str,
        description: str,
        agent_id: str,
        priority: int = 5,
        task_type: str = "general",
        planned_steps: list[str] | None = None,
        idempotency_key: str | None = None,
        has_side_effects: bool = False,
        requires_approval: bool = False,
        estimated_cost_usd: float = 0.0,
        project_id: str | None = None,
        parent_task_id: str | None = None,
        depth: int = 0,
        required_capabilities: dict[str, Any] | None = None,
    ) -> Task:
        settings_row = self._get_settings(db)
        app_cfg = get_settings()

        if settings_row.emergency_stop:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="EMERGENCY STOP đang kích hoạt. Hệ thống từ chối nhận nhiệm vụ mới để đảm bảo an toàn.",
            )

        if depth > app_cfg.MAX_WORKFLOW_DEPTH:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Vượt quá giới hạn độ sâu điều phối tối đa ({app_cfg.MAX_WORKFLOW_DEPTH}).",
            )

        if idempotency_key:
            existing = db.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
            if existing:
                return existing

        agent = db.scalar(select(Agent).where(Agent.id == agent_id))
        if not agent:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Agent '{agent_id}' không tồn tại.")

        active_count = db.scalar(
            select(func.count(Task.id)).where(
                Task.status.in_([TaskStatus.CREATED.value, TaskStatus.QUEUED.value, TaskStatus.RUNNING.value])
            )
        ) or 0
        if active_count >= app_cfg.MAX_WORKFLOW_TASKS * 3:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Hàng đợi nhiệm vụ đang đầy, vui lòng chờ các task hiện tại hoàn tất.",
            )

        # Automation level & high-cost approval checks
        if (
            settings_row.automation_level == AutomationLevel.LEVEL_1_OBSERVE.value
            and has_side_effects
        ):
            requires_approval = True

        if estimated_cost_usd >= settings_row.approval_threshold_usd:
            requires_approval = True

        steps = planned_steps if planned_steps and len(planned_steps) > 0 else list(DEFAULT_STEPS)
        initial_status = (
            TaskStatus.WAITING_FOR_APPROVAL.value
            if requires_approval
            else (TaskStatus.PAUSED.value if settings_row.system_paused else TaskStatus.QUEUED.value)
        )

        task = Task(
            title=title,
            description=description,
            agent_id=agent.id,
            project_id=project_id,
            parent_task_id=parent_task_id,
            depth=depth,
            priority=priority,
            task_type=task_type,
            required_capabilities=required_capabilities or {},
            status=initial_status,
            idempotency_key=idempotency_key,
            has_side_effects=has_side_effects,
            planned_steps=steps,
            completed_steps=0,
            total_steps=len(steps),
            progress_pct=0,
            estimated_cost_usd=estimated_cost_usd,
            requires_approval=requires_approval,
            approval_granted=not requires_approval,
        )
        db.add(task)
        db.flush()

        event_bus.publish(
            db,
            event_type=EventType.TASK_CREATED.value,
            entity_id=task.id,
            task_id=task.id,
            agent_id=agent.id,
            payload={
                "task_id": task.id,
                "title": task.title,
                "agent_id": agent.id,
                "status": task.status,
                "total_steps": task.total_steps,
            },
        )

        if requires_approval:
            approval = ApprovalRequest(
                task_id=task.id,
                agent_id=agent.id,
                action_type="SIDE_EFFECT_OR_BUDGET_THRESHOLD",
                reason=(
                    f"Nhiệm vụ '{task.title}' yêu cầu phê duyệt (chi phí ước tính ${estimated_cost_usd:.2f}, "
                    f"cấp tự động hóa {settings_row.automation_level}, tác dụng phụ={has_side_effects})."
                ),
                estimated_cost_usd=estimated_cost_usd,
                status="PENDING",
            )
            db.add(approval)
            db.flush()
            event_bus.publish(
                db,
                event_type=EventType.APPROVAL_REQUIRED.value,
                entity_id=approval.id,
                task_id=task.id,
                agent_id=agent.id,
                payload={"approval_id": approval.id, "task_id": task.id, "reason": approval.reason},
            )
            emit_notification(
                db,
                notif_type=NotificationType.APPROVAL_REQUIRED.value,
                title=f"Cần phê duyệt: {task.title}",
                message=approval.reason,
                task_id=task.id,
                agent_id=agent.id,
            )

        db.commit()
        db.refresh(task)
        return task

    def latest_checkpoint(self, db: Session, task_id: str) -> Checkpoint | None:
        return db.scalar(
            select(Checkpoint)
            .where(Checkpoint.task_id == task_id)
            .order_by(Checkpoint.step_index.desc())
            .limit(1)
        )

    async def execute_task(
        self,
        db: Session,
        task_id: str,
        *,
        step_override_behaviors: dict[int, str] | None = None,
        side_effect_callback: Callable[[str], None] | None = None,
    ) -> Task:
        task = db.scalar(select(Task).where(Task.id == task_id))
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if task.status == TaskStatus.COMPLETED.value:
            return task
        if task.status == TaskStatus.CANCELLED.value:
            return task

        settings_row = self._get_settings(db)
        if settings_row.emergency_stop or settings_row.system_paused:
            task.status = TaskStatus.PAUSED.value
            db.commit()
            return task

        if task.requires_approval and not task.approval_granted:
            task.status = TaskStatus.WAITING_FOR_APPROVAL.value
            db.commit()
            return task

        agent = db.scalar(select(Agent).where(Agent.id == task.agent_id))
        if not agent:
            raise HTTPException(status_code=404, detail="Assigned agent missing")

        # Load neutral state from latest checkpoint so switching models never loses or repeats steps
        ckpt = self.latest_checkpoint(db, task.id)
        step_outputs: list[dict[str, Any]] = list(ckpt.step_outputs_json) if ckpt else []
        committed_effects: list[str] = list(ckpt.side_effect_keys_committed) if ckpt else []
        start_step = (ckpt.step_index + 1) if ckpt else task.completed_steps

        task.status = TaskStatus.RUNNING.value
        task.attempt_count = (task.attempt_count or 0) + 1
        if not task.started_at:
            task.started_at = datetime.now(timezone.utc)
        set_agent_status(db, agent, AgentStatus.WORKING.value, task.id)
        event_bus.publish(
            db,
            event_type=EventType.TASK_STARTED.value,
            entity_id=task.id,
            task_id=task.id,
            agent_id=agent.id,
            payload={
                "task_id": task.id,
                "resumed_from_step": start_step,
                "total_steps": task.total_steps,
            },
        )
        db.commit()

        steps = task.planned_steps or list(DEFAULT_STEPS)
        total = max(1, len(steps))
        max_allowed_steps = min(total, agent.max_loops + 2)

        for idx in range(start_step, max_allowed_steps):
            # Re-check pause / cancel / emergency stop between steps
            db.refresh(task)
            settings_row = self._get_settings(db)
            if task.status in (TaskStatus.PAUSED.value, TaskStatus.CANCELLED.value) or settings_row.emergency_stop:
                if settings_row.emergency_stop and task.status == TaskStatus.RUNNING.value:
                    task.status = TaskStatus.PAUSED.value
                set_agent_status(db, agent, AgentStatus.PAUSED.value, task.id)
                db.commit()
                return task

            step_name = steps[idx]
            compact_prior = "\n".join(
                f"- Bước {o['step_index'] + 1} ({o['step_name']}): {o['output'][:220]}"
                for o in step_outputs[-3:]
            )
            user_prompt = (
                f"Nhiệm vụ: {task.title}\n"
                f"Chi tiết: {task.description}\n"
                f"Bước hiện tại ({idx + 1}/{total}): {step_name}\n"
                f"Kết quả trung lập các bước đã lưu:\n{compact_prior or '(Bước khởi đầu)'}"
            )

            req_caps = task.required_capabilities or {}
            override_behavior = (step_override_behaviors or {}).get(idx)

            try:
                # Reset per-step tried list while recording cumulative history on task
                resp, step_tried = await self.router.execute_with_fallback(
                    db,
                    request=ProviderRequest(
                        system_prompt=agent.system_prompt,
                        user_prompt=user_prompt,
                        model_id=agent.preferred_model_id or "auto",
                        require_json=bool(req_caps.get("require_json", False)),
                        max_tokens=min(1024, agent.token_limit),
                        timeout_seconds=float(min(agent.timeout_seconds, 45)),
                        metadata={
                            "step_name": step_name,
                            "step_index": idx,
                            "agent_role": agent.name,
                            "override_mock_behavior": override_behavior,
                        },
                    ),
                    task_id=task.id,
                    agent_id=agent.id,
                    agent_name=agent.name,
                    task_type=task.task_type,
                    require_tools=bool(req_caps.get("require_tools", False)),
                    require_vision=bool(req_caps.get("require_vision", False)),
                    min_context=int(req_caps.get("min_context", 1024)),
                    preferred_provider_id=agent.preferred_provider_id,
                    preferred_model_id=agent.preferred_model_id,
                )
            except NoAvailableModelError as no_mod:
                task.status = TaskStatus.WAITING_FOR_PROVIDER.value
                task.last_error = no_mod.reason
                errs = list(task.error_log or [])
                errs.append(
                    {
                        "step": idx,
                        "reason": no_mod.reason,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                task.error_log = errs
                set_agent_status(db, agent, AgentStatus.WAITING.value, task.id)
                event_bus.publish(
                    db,
                    event_type=EventType.TASK_PAUSED.value,
                    entity_id=task.id,
                    task_id=task.id,
                    agent_id=agent.id,
                    payload={
                        "task_id": task.id,
                        "status": task.status,
                        "completed_steps": task.completed_steps,
                        "reason": no_mod.reason,
                    },
                )
                emit_notification(
                    db,
                    notif_type=NotificationType.TASK_PAUSED.value,
                    title="Tạm dừng nhiệm vụ — Chờ bổ sung Provider/Quota",
                    message=(
                        "Không còn model phù hợp trong danh sách được cấu hình. "
                        "Nhiệm vụ đã được lưu checkpoint và tạm dừng. Vui lòng bổ sung provider hoặc chờ quota khôi phục."
                    ),
                    task_id=task.id,
                    agent_id=agent.id,
                )
                db.commit()
                return task
            except ProviderError as p_err:
                task.status = TaskStatus.FAILED.value
                task.last_error = p_err.message
                errs = list(task.error_log or [])
                errs.append(
                    {
                        "step": idx,
                        "category": p_err.category.value,
                        "reason": p_err.message,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                task.error_log = errs
                set_agent_status(db, agent, AgentStatus.ERROR.value, None)
                event_bus.publish(
                    db,
                    event_type=EventType.TASK_FAILED.value,
                    entity_id=task.id,
                    task_id=task.id,
                    agent_id=agent.id,
                    payload={"task_id": task.id, "error": p_err.message},
                )
                emit_notification(
                    db,
                    notif_type=NotificationType.ERROR.value,
                    title=f"Nhiệm vụ thất bại: {task.title}",
                    message=p_err.message,
                    task_id=task.id,
                    agent_id=agent.id,
                )
                db.commit()
                return task

            # Side-effect idempotency guard: commit side effect at most ONCE per step key
            effect_key = f"{task.idempotency_key or task.id}:step:{idx}"
            if task.has_side_effects and effect_key not in committed_effects:
                executed_now = side_effect_ledger.run_once(
                    effect_key,
                    callback=(lambda ek=effect_key: side_effect_callback(ek)) if side_effect_callback else None,
                )
                committed_effects.append(effect_key)
                if executed_now:
                    task.side_effect_committed = True

            step_record = {
                "step_index": idx,
                "step_name": step_name,
                "provider_id": resp.provider_id,
                "model_id": resp.model_id,
                "output": resp.content,
                "is_demo": resp.is_demo,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            step_outputs.append(step_record)

            # Update task metrics & verified checkpoint progress
            all_tried = list(dict.fromkeys([*(task.tried_models or []), *step_tried]))
            task.tried_models = all_tried
            task.current_provider_id = resp.provider_id
            task.current_model_id = resp.model_id
            task.input_tokens = (task.input_tokens or 0) + resp.input_tokens
            task.output_tokens = (task.output_tokens or 0) + resp.output_tokens
            task.is_demo = bool(task.is_demo or resp.is_demo)
            task.completed_steps = idx + 1
            task.progress_pct = int(round((task.completed_steps / total) * 100))

            new_ckpt = Checkpoint(
                task_id=task.id,
                step_index=idx,
                step_name=step_name,
                neutral_state_json={
                    "task_title": task.title,
                    "completed_steps": task.completed_steps,
                    "total_steps": total,
                    "last_model": f"{resp.provider_id}:{resp.model_id}",
                },
                step_outputs_json=step_outputs,
                side_effect_keys_committed=committed_effects,
                provider_id=resp.provider_id,
                model_id=resp.model_id,
            )
            db.add(new_ckpt)
            db.flush()

            event_bus.publish(
                db,
                event_type=EventType.TASK_PROGRESS.value,
                entity_id=task.id,
                task_id=task.id,
                agent_id=agent.id,
                payload={
                    "task_id": task.id,
                    "step_index": idx,
                    "step_name": step_name,
                    "completed_steps": task.completed_steps,
                    "total_steps": total,
                    "progress_pct": task.progress_pct,
                    "provider_id": resp.provider_id,
                    "model_id": resp.model_id,
                },
            )
            db.commit()

        # Finalize task completion
        task.status = TaskStatus.COMPLETED.value
        task.progress_pct = 100
        task.completed_at = datetime.now(timezone.utc)
        task.result_summary = step_outputs[-1]["output"] if step_outputs else "Hoàn thành."
        task.result_payload = {
            "steps": step_outputs,
            "final_provider": task.current_provider_id,
            "final_model": task.current_model_id,
            "is_demo": task.is_demo,
        }
        agent.memory_summary = f"Hoàn thành '{task.title}' qua {task.current_provider_id}/{task.current_model_id}."
        set_agent_status(db, agent, AgentStatus.IDLE.value, None)

        event_bus.publish(
            db,
            event_type=EventType.TASK_COMPLETED.value,
            entity_id=task.id,
            task_id=task.id,
            agent_id=agent.id,
            payload={
                "task_id": task.id,
                "title": task.title,
                "agent_id": agent.id,
                "provider_id": task.current_provider_id,
                "model_id": task.current_model_id,
                "is_demo": task.is_demo,
            },
        )
        emit_notification(
            db,
            notif_type=NotificationType.SUCCESS.value,
            title=f"Hoàn thành nhiệm vụ: {task.title}",
            message=f"{agent.name} đã hoàn tất 100% ({total}/{total} checkpoint) bằng {task.current_model_id}.",
            task_id=task.id,
            agent_id=agent.id,
        )
        db.commit()
        db.refresh(task)
        return task

    def pause_task(self, db: Session, task_id: str) -> Task:
        task = db.scalar(select(Task).where(Task.id == task_id))
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status in (TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value):
            return task
        task.status = TaskStatus.PAUSED.value
        agent = db.scalar(select(Agent).where(Agent.id == task.agent_id))
        if agent and agent.current_task_id == task.id:
            set_agent_status(db, agent, AgentStatus.PAUSED.value, task.id)
        event_bus.publish(
            db,
            event_type=EventType.TASK_PAUSED.value,
            entity_id=task.id,
            task_id=task.id,
            agent_id=task.agent_id,
            payload={"task_id": task.id, "status": task.status, "completed_steps": task.completed_steps},
        )
        db.commit()
        db.refresh(task)
        return task

    def cancel_task(self, db: Session, task_id: str) -> Task:
        task = db.scalar(select(Task).where(Task.id == task_id))
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status == TaskStatus.COMPLETED.value:
            return task
        task.status = TaskStatus.CANCELLED.value
        agent = db.scalar(select(Agent).where(Agent.id == task.agent_id))
        if agent and agent.current_task_id == task.id:
            set_agent_status(db, agent, AgentStatus.IDLE.value, None)
        db.commit()
        db.refresh(task)
        return task
