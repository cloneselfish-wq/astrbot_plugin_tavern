from __future__ import annotations

import time
from dataclasses import asdict, dataclass


_GENERATION_STAGE_LABELS = {
    "prepare_context": "整理本轮线索与角色状态",
    "planning": "规划故事推进",
    "story_generation": "生成故事进展",
    "generate_resolution": "结算本轮行动",
    "generate_narrative": "生成故事正文",
    "generate_choices": "准备下一步行动",
    "repair_or_validate": "检查故事一致性",
    "targeted_quality_repair": "修正故事内容",
    "dice_locked": "保存检定结果",
    "ready_to_commit": "保存本轮结果",
    "commit_and_deliver": "发送本轮结果",
    "generating": "处理本轮行动",
}


def player_generation_stage_label(stage: object) -> str:
    """把内部生成阶段转换为玩家可见中文；未知阶段绝不原样外显。"""

    return _GENERATION_STAGE_LABELS.get(
        str(stage or "").strip().lower(),
        "处理本轮行动",
    )


class GenerationBudgetExceeded(RuntimeError):
    """The whole-turn model budget is exhausted before a safe commit."""

    status_code = 400

    def __init__(
        self,
        message: str = (
            "本轮故事生成超过时间预算。系统未提交半个回合，"
            "已保留本轮操作记录；请稍后重试。"
        ),
    ) -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StageRecord:
    stage: str
    provider_id: str
    attempt: int
    elapsed: float
    remaining_seconds: float
    result: str


class TurnGenerationBudget:
    def __init__(
        self,
        *,
        total_seconds: float,
        max_calls: int,
        per_call_seconds: float,
        max_fallbacks: int,
        repair_budget: int,
        reserve_seconds: float,
    ) -> None:
        self.deadline = time.monotonic() + max(0.0, float(total_seconds))
        self.remaining_calls = max(0, int(max_calls))
        self.per_call_seconds = max(0.0, float(per_call_seconds))
        self.remaining_fallbacks = max(0, int(max_fallbacks))
        self.remaining_repairs = max(0, int(repair_budget))
        self.reserve_seconds = max(0.0, float(reserve_seconds))
        self.stage = "prepare_context"
        self.stages: list[StageRecord] = []
        self.started = time.monotonic()

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def per_call_timeout(self) -> float:
        usable = self.remaining_seconds() - self.reserve_seconds
        return max(0.0, min(self.per_call_seconds, usable))

    def begin_stage(self, stage: str) -> None:
        """Expose the active player-safe stage before a blocking model call."""

        self.stage = str(stage or "generating")

    def consume_call(self) -> None:
        if self.remaining_calls <= 0 or self.per_call_timeout() <= 0:
            raise GenerationBudgetExceeded()
        self.remaining_calls -= 1

    def consume_fallback(self) -> bool:
        if self.remaining_fallbacks <= 0:
            return False
        self.remaining_fallbacks -= 1
        return True

    def consume_repair(self) -> bool:
        if self.remaining_repairs <= 0:
            return False
        self.remaining_repairs -= 1
        return True

    def record(
        self,
        *,
        stage: str,
        provider_id: str = "",
        attempt: int = 0,
        result: str,
    ) -> None:
        self.stage = stage
        self.stages.append(
            StageRecord(
                stage=stage,
                provider_id=str(provider_id or ""),
                attempt=max(0, int(attempt)),
                elapsed=round(time.monotonic() - self.started, 3),
                remaining_seconds=round(self.remaining_seconds(), 2),
                result=str(result),
            )
        )

    def safe_records(self) -> list[dict[str, object]]:
        return [asdict(item) for item in self.stages]
