import time
from enum import Enum
from typing import Optional

class CircuitState(Enum):
    CLOSED = "CLOSED"       # Штатный режим: запросы идут напрямую
    OPEN = "OPEN"           # Аварийный режим: запросы перенаправляются на фоллбэк
    HALF_OPEN = "HALF_OPEN" # Пробный режим: проверяем оживание сервиса

class CircuitBreaker:
    """
    Конечный автомат защиты от падений и зависаний моделей (порт 8082 -> 8080).
    Предотвращает бесконечные зависания при перегрузке медленных моделей.
    """
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_sec: float = 30.0
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_state_change = time.monotonic()

    def can_execute(self) -> bool:
        """Проверка, разрешено ли отправлять запрос на основной сервис."""
        now = time.monotonic()
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if now - self.last_state_change > self.recovery_timeout_sec:
                self.state = CircuitState.HALF_OPEN
                self.last_state_change = now
                return True
            return False

        if self.state == CircuitState.HALF_OPEN:
            return True

        return False

    def record_success(self) -> None:
        """Фиксация успешного ответа: сброс счетчика сбоев."""
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_state_change = time.monotonic()

    def record_failure(self) -> None:
        """Фиксация таймаута или ошибки 5xx."""
        self.failure_count += 1
        now = time.monotonic()
        if self.state == CircuitState.HALF_OPEN or self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.last_state_change = now

    def inject_fallback_thinking(self, messages: list) -> list:
        """
        Мета-промпт инъекция: при падении 32B запросы уходят на 7B,
        но снабжаются директивой принудительного пошагового мышления.
        """
        augmented = [dict(m) for m in messages]
        for m in reversed(augmented):
            if m.get("role") == "user":
                orig = m.get("content", "")
                m["content"] = (
                    "<think>\n"
                    "Think step-by-step applying first principles and defensive systems design before answering.\n"
                    "</think>\n\n" + orig
                )
                break
        return augmented

if __name__ == "__main__":
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_sec=1.0)
    print("[CIRCUIT BREAKER TEST]")
    print(f"  Стартовое состояние: {cb.state.value} (can_execute={cb.can_execute()})")

    cb.record_failure()
    print(f"  Сбой 1: {cb.state.value} (ошибок: {cb.failure_count})")

    cb.record_failure()
    print(f"  Сбой 2 (порог достигнут): {cb.state.value} (can_execute={cb.can_execute()})")

    # Проверка работы фоллбэк-инъекции
    test_msgs = [{"role": "user", "content": "Explain POSIX mutex"}]
    injected = cb.inject_fallback_thinking(test_msgs)
    print("\n  Модифицированный промпт при фоллбэке:")
    print(f"  '{injected[0]['content'][:90]}...'")
