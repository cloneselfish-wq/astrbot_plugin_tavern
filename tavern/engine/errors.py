from .shared import *

class TavernEngineError(RuntimeError):
    pass


class TavernStoryGenerationError(TavernEngineError):
    """Story generation failed before commit, with a safe player diagnosis."""

    def __init__(
        self,
        message: str,
        *,
        failure_kinds: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.failure_kinds = tuple(
            dict.fromkeys(
                str(item or "").strip()
                for item in failure_kinds
                if str(item or "").strip()
            )
        )

    @property
    def player_reason(self) -> str:
        kinds = set(self.failure_kinds)
        if {"client_closed", "timeout"}.issubset(kinds):
            return (
                "一个叙事模型的连接在请求期间失效，后续模型又未在"
                "等待时间内返回完整正文。"
            )
        if "client_closed" in kinds:
            return (
                "叙事模型客户端已经失效；这通常发生在模型配置热重载"
                "或 AstrBot 正在重启时。"
            )
        if "timeout" in kinds:
            return (
                "叙事模型已经建立连接，但未在本轮单次等待时间内返回"
                "完整正文。"
            )
        if "invalid_response" in kinds:
            return "叙事模型返回了内容，但正文或行动选项没有通过一致性校验。"
        if "unavailable" in kinds:
            return "当前没有可用的叙事模型，且模型链中没有可继续尝试的备用模型。"
        return "叙事模型本次没有完成可安全提交的故事结果。"


def story_generation_failure_message(
    exc: TavernStoryGenerationError,
    *,
    operation: str = "结算本轮行动",
) -> PlayerMessage:
    """Build one privacy-safe failure card for every story entry point."""

    return PlayerMessage.dynamic(
        title="故事生成未完成",
        summary=f"失败操作：{str(operation or '结算本轮行动').rstrip('。')}。",
        sections=(
            f"原因：{exc.player_reason}",
            (
                "自动处理：本轮没有提交，世界状态没有改变；已锁定的"
                "行动、表决和检定仍可恢复。"
            ),
            (
                "若再次失败，请先在 WebUI 的“模型与回复”运行健康检查，"
                "并配置至少一个备用模型。"
            ),
        ),
        actions=("/团 重试本轮", "/团 当前"),
        source="story_generation_failure",
    )


def _provider_failure_kind(exc: BaseException) -> str:
    text = f"{type(exc).__name__} {exc}".lower()
    if (
        "client has been closed" in text
        or "client is closed" in text
        or "closed client" in text
    ):
        return "client_closed"
    if isinstance(exc, TimeoutError) or "timeout" in text or "超时" in text:
        return "timeout"
    return "unavailable"


def _provider_failure_label(kind: str) -> str:
    return {
        "client_closed": "模型客户端已关闭",
        "timeout": "模型请求超时",
        "unavailable": "模型暂不可用",
    }.get(str(kind or ""), "模型暂不可用")


class TavernBusyError(TavernEngineError):
    pass


class TavernOperationCancelled(TavernEngineError):
    """A durable cancellation won the race against generation/commit."""


class TavernPlayerDisabledError(TavernEngineError):
    pass


class TavernTurnOrderError(TavernEngineError):
    def __init__(
        self,
        message: str,
        *,
        turn: Mapping[str, Any],
        joined: bool = False,
    ) -> None:
        super().__init__(message)
        self.turn = dict(turn)
        self.joined = bool(joined)


@dataclass(frozen=True, slots=True)
class EngineReply:
    text: str
    session: dict[str, Any]
    dice: DiceResult | None = None
    ooc: bool = False
    turn: dict[str, Any] | None = None
    story_text: str = ""
    turn_text: str = ""
    messages: tuple[PlayerMessage, ...] = ()
    message_bundle: TurnMessageBundle | None = None


ProgressPayload = PlayerMessage | str
ProgressCallback = Callable[[ProgressPayload], Any]


__all__ = [name for name in globals() if not name.startswith("__")]
