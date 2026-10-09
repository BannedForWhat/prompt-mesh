# prompt-mesh

[![Platform](https://img.shields.io/badge/Platform-Arch_Linux-blue.svg?logo=arch-linux)](https://archlinux.org/)
[![Kernel](https://img.shields.io/badge/Kernel-6.x-green.svg?logo=linux)](https://kernel.org/)
[![Runtime](https://img.shields.io/badge/Runtime-asyncio_Zero--Copy-yellow.svg?logo=python)](https://python.org/)
[![Telemetry](https://img.shields.io/badge/IPC-POSIX_SHM_(mmap)-orange.svg)](https://github.com/BannedForWhat/amdgpu-sentinel)
[![Sandbox](https://img.shields.io/badge/Sandbox-systemd_cgroups_v2-black.svg?logo=systemd)](https://systemd.io/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Высокопроизводительный платформенный AI-шлюз и оркестратор мета-промптов (OpenAI-compatible Reverse Proxy), спроектированный для превентивной защиты локальной LLM-инфраструктуры от аппаратного переполнения видеопамяти и лавинообразного падения пропускной способности (TTM Eviction Storm).

Выступает единой точкой входа (порт `8000`) для редакторов кода (VS Code Continue.dev, Cursor, HTTP-клиенты) и динамически маршрутизирует запросы между моделями кластера `llama-server` с учетом физического состояния GPU.

---

## Архитектура системы

```text
       [ VS Code / Continue.dev / HTTP Clients ]
                           │
                           ▼ (порт 8000: /v1/chat/completions)
       +───────────────────────────────────────────────────────────+
       |                       prompt-mesh                         |
       |  (asyncio.start_server, RSS ~12.8 МБ, cgroups Max=128M)   |
       |                                                           |
       |   1. VRAM-Guard: mmap чтение /dev/shm/vram_guard (< 1 мкс)|
       |   2. Middle-Out Pruning: срез контекста при VRAM > 85%    |
       |   3. Intent Routing: микросекундная классификация (< 2 мс)|
       |   4. Circuit Breaker: автопереключение 32B -> 7B (<think>)|
       +─────────────┬─────────────────────────────┬───────────────+
                     │                             │
    (Легкий код, 56 t/s)                           │ (Сложная архитектура)
                     ▼                             ▼
       [ Qwen-7B (порт 8080) ]       [ DeepSeek-32B (порт 8082) ]
```

---

## Ключевые возможности

1. **Hardware-Aware Load Shedding (Связка с amdgpu-sentinel):**
   * Через POSIX Shared Memory (`/dev/shm/vram_guard`) шлюз за **< 100 наносекунд** без системных вызовов считывает состояние видеокарты AMD Radeon RX 6700 XT.
   * Если утилизация VRAM превышает 85% или взведен флаг `ttm_alert`, шлюз применяет **Middle-Out Pruning**, сохраняя системный промпт и последние реплики, но вырезая середину диалога, предотвращая падение пропускной способности шины PCIe в 12.2 раза.
2. **Защита от Threshold Decay (Recency Bias Anchors):**
   * При сжатии истории критические системные директивы (`Zero-Allocation`, `POSIX`, `noexcept`) автоматически дублируются в конец последнего сообщения пользователя.
3. **Active Slot Cancellation в llama-server:**
   * При отмене запроса пользователем в IDE шлюз немедленно разрывает TCP-сокет к бэкенду, заставляя `llama-server` прервать генерацию и освободить вычислительные ресурсы GPU.
4. **Адаптивный Circuit Breaker:**
   * При трех подряд таймаутах медленной модели (32B) контур размыкается, и трафик перенаправляется на быстрый Qwen-7B с динамической инъекцией тегов `<think>`.
5. **Сверхнизкий футпринт (RSS ~12.8 МБ):**
   * Отказ от тяжелых фреймворков (FastAPI/Pydantic) в пользу чистого `asyncio.start_server`.
   * Регулярная дефрагментация арен памяти glibc через `ctypes.CDLL('libc.so.6').malloc_trim(0)`.
   * Изоляция в `systemd --user` с лимитом `MemoryMax=128M`.

---

## Сборка и развертывание

### Предварительные требования
* Запущенный фоновый сервис телеметрии amdgpu-sentinel.
* Кластер llama-server на портах 8080, 8081, 8082.

### Установка сервиса
```bash
git clone https://github.com/BannedForWhat/prompt-mesh.git
cd prompt-mesh
make install

# Запуск службы в пользовательском сеансе
systemctl --user enable --now prompt-mesh.service

# Просмотр статуса и лимитов памяти cgroups v2
systemctl --user status prompt-mesh.service
```

---

## Наблюдаемость (Prometheus Metrics)

Шлюз отдает платформенные метрики на эндпоинте `GET http://127.0.0.1:8000/metrics`:

```text
# HELP prompt_mesh_requests_total Total requests processed
# TYPE prompt_mesh_requests_total counter
prompt_mesh_requests_total 42
prompt_mesh_pruned_total 3
prompt_mesh_routed_7b 35
prompt_mesh_routed_1_5b 5
prompt_mesh_routed_32b 2
prompt_mesh_cb_fallbacks 0
prompt_mesh_rss_mb 12.80
prompt_mesh_gpu_vram_pct 20.2
prompt_mesh_gpu_ttm_alert 0
```
