import re
from typing import Dict, Any, List, Tuple

class ModelEndpoints:
    PORT_7B_CHAT  = 8080  # Qwen2.5-Coder-7B (быстрый код, 56 t/s)
    PORT_1_5B_FIM = 8081  # Qwen2.5-Coder-1.5B (автокомплит / FIM)
    PORT_32B_DEEP = 8082  # DeepSeek-R1-32B (глубокие рассуждения)

class IntentRouter:
    """
    Эвристический микросекундный классификатор намерений (< 1 мс).
    Не требует обращения к LLM или эмбеддингам.
    """
    def __init__(self) -> None:
        # Регулярные выражения для архитектурных и фундаментальных задач
        self._deep_reasoning_pattern = re.compile(
            r"(первые\s+принцип|first-principles|декомпозиц|архитектур|deadlock|race\s+condition|"
            r"моделирован\w+\s+угроз|threat\s+model|proof|докажи|спроектируй|почему\s+происходит|"
            r"kernel\s+panic|memory\s+barrier|seqlock|rcu)",
            re.IGNORECASE
        )

    def calculate_code_density(self, text: str) -> float:
        """Подсчет доли синтаксических конструкций кода в тексте."""
        if not text:
            return 0.0
        code_markers = [
            "{", "}", "(", ")", ";", "->", "::", "=>", "==", "!=",
            "def ", "class ", "fn ", "import ", "return ", "struct ", "noexcept"
        ]
        hits = sum(text.count(marker) for marker in code_markers)
        return hits / max(1, len(text.split()))

    def classify_request(
        self, 
        path: str, 
        body: Dict[str, Any]
    ) -> Tuple[int, str]:
        """
        Определение целевого порта:
        Возвращает: (порт, обоснование_маршрутизации).
        """
        # 1. Проверка FIM / Tab Autocomplete
        if path.endswith("/completions") and "prompt" in body:
            prompt_str = str(body.get("prompt", ""))
            if "<|fim_prefix|>" in prompt_str or "<|fim_middle|>" in prompt_str:
                return ModelEndpoints.PORT_1_5B_FIM, "FIM_AUTOCOMPLETE"

        messages: List[Dict[str, Any]] = body.get("messages", [])
        if not messages:
            return ModelEndpoints.PORT_7B_CHAT, "DEFAULT_FALLBACK"

        # Извлекаем последнее сообщение пользователя
        last_user_prompt = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_user_prompt = str(msg.get("content", ""))
                break

        # 2. Проверка на сложный архитектурный анализ (Deep Reasoning)
        if self._deep_reasoning_pattern.search(last_user_prompt):
            return ModelEndpoints.PORT_32B_DEEP, "DEEP_REASONING_MATCH"

        # 3. Проверка на плотность кода
        density = self.calculate_code_density(last_user_prompt)
        if density > 0.15:
            return ModelEndpoints.PORT_7B_CHAT, f"CODE_GENERATION (density: {density:.2f})"

        # 4. По умолчанию: быстрый чат на 7B
        return ModelEndpoints.PORT_7B_CHAT, "GENERAL_CHAT"

if __name__ == "__main__":
    router = IntentRouter()
    
    test_cases = [
        {"path": "/v1/completions", "body": {"prompt": "<|fim_prefix|>def add(a, b):<|fim_suffix|><|fim_middle|>"}},
        {"path": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "Проведи декомпозицию ядра Linux на основе первых принципов"}]}},
        {"path": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "class Worker { public: void run() noexcept; };"}]}},
        {"path": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "Привет! Как настроить ssh-ключи на Arch Linux?"}]}}
    ]

    print("[INTENT ROUTER TEST]")
    for tc in test_cases:
        port, reason = router.classify_request(tc["path"], tc["body"])
        prompt_snippet = str(tc["body"].get("prompt") or tc["body"]["messages"][0]["content"])[:50]
        print(f"  -> Промпт: '{prompt_snippet}...'")
        print(f"     Целевой порт: {port} | Правило: {reason}\n")
