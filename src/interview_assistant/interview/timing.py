"""持久化进度中的有效计时；暂停不计入场次和目标预算，不改动历史问答。"""

from datetime import datetime

from interview_assistant.database import utc_now
from interview_assistant.interview.schemas import ObjectiveProgress, SessionProgress
from interview_assistant.planner.schemas import PlanRuntime


def active_seconds(
    started_at: datetime, ended_at: datetime | None, status: str,
    updated_at: datetime, progress: SessionProgress, *, now: datetime | None = None,
) -> int:
    end = ended_at or now or utc_now()
    paused = progress.paused_seconds
    # 旧的已暂停场次没有 paused_at，首次使用时以最后状态变更时间保守兼容。
    pause_start = progress.paused_at or (updated_at if status == "paused" else None)
    if pause_start is not None:
        paused += max(0, (end - pause_start).total_seconds())
    return max(0, int((end - started_at).total_seconds() - paused))


def close_pause(progress: SessionProgress, now: datetime, fallback: datetime) -> None:
    start = progress.paused_at or fallback
    progress.paused_seconds += max(0, (now - start).total_seconds())
    progress.paused_at = None


def objective_seconds(state: ObjectiveProgress, elapsed: int) -> int:
    current = max(0, elapsed - state.started_elapsed_sec) if state.started_elapsed_sec is not None else 0
    return state.elapsed_sec + current


def settle_objectives(progress: SessionProgress, elapsed: int, next_id: str | None = None) -> None:
    for state in progress.objectives:
        if state.id != next_id and state.started_elapsed_sec is not None:
            state.elapsed_sec = objective_seconds(state, elapsed)
            state.started_elapsed_sec = None
    if next_id is not None:
        state = progress.state(next_id)
        if state.started_elapsed_sec is None:
            state.started_elapsed_sec = elapsed


def source_seconds(runtime: PlanRuntime, progress: SessionProgress, elapsed: int) -> dict[str, int]:
    spent = {"project": 0, "question_bank": 0}
    for objective in runtime.objectives:
        spent[objective.source] += objective_seconds(progress.state(objective.id), elapsed)
    return spent
