"""计数式熔断器（Circuit Breaker）

语义约定：统计的是 retry 耗尽后的最终失败（fallback_used 或异常上抛），
与 retry.py 的微观重试分层——重试管单次调用内恢复，熔断管持续失败的快速失败。

状态机 CLOSED → OPEN → HALF_OPEN：
- CLOSED：正常放行；连续失败达阈值 → OPEN
- OPEN：冷却期内快速失败（allow_request 返回 False）；冷却结束 → HALF_OPEN
- HALF_OPEN：放行探测；成功 → CLOSED 并清零，失败 → 重新 OPEN

单事件循环内调用（asyncio 单线程），不加锁。
"""
import time


class CircuitBreaker:
    def __init__(self, name: str = "", failure_threshold: int = 5, cooldown_seconds: float = 30.0):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        # 时间驱动：OPEN 冷却到期自动转 HALF_OPEN
        if self._state == "open" and time.monotonic() - self._opened_at >= self.cooldown_seconds:
            self._state = "half_open"
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow_request(self) -> bool:
        return self.state in ("closed", "half_open")

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = "closed"

    def record_failure(self) -> None:
        if self.state == "half_open":
            # 探测失败：立即重新熔断
            self._open()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._open()

    def reset(self) -> None:
        self._state = "closed"
        self._consecutive_failures = 0
        self._opened_at = 0.0

    def _open(self) -> None:
        self._state = "open"
        self._opened_at = time.monotonic()
        self._consecutive_failures = 0
