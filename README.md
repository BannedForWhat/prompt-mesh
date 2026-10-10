# prompt-mesh

[![Platform](https://img.shields.io/badge/Platform-Arch_Linux-blue.svg?logo=arch-linux)](https://archlinux.org/)
[![Kernel](https://img.shields.io/badge/Kernel-6.x-green.svg?logo=linux)](https://kernel.org/)
[![Runtime](https://img.shields.io/badge/Runtime-Python_asyncio-yellow.svg?logo=python)](https://python.org/)
[![IPC](https://img.shields.io/badge/IPC-POSIX_SHM_(mmap)-orange.svg)](https://github.com/BannedForWhat/amdgpu-sentinel)
[![Sandbox](https://img.shields.io/badge/Sandbox-systemd_cgroups_v2-black.svg?logo=systemd)](https://systemd.io/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

L7-шлюз и обратный прокси для локального кластера llama.cpp с поддержкой спецификации OpenAI API (`/v1/chat/completions`, `/v1/completions`).

Принимает запросы от редакторов кода (VS Code Continue.dev, Cursor), отслеживает объем видеопамяти через разделяемую память amdgpu-sentinel и усекает контекст при риске вытеснения данных в системную память хоста.

## Архитектура

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
       (генерация кода, 56 t/s)                    │ (глубокий анализ)
                     ▼                             ▼
       [ Qwen-7B (порт 8080) ]       [ DeepSeek-32B (порт 8082) ]
```

## Функциональность

1. **Контроль памяти видеокарты:** шлюз считывает структуры из `/dev/shm/vram_guard` через `mmap` без системных вызовов ввода-вывода. При утилизации VRAM выше 85% или флаге `ttm_alert` включается алгоритм **Middle-Out Pruning**: системный промпт и последние сообщения сохраняются, а центральная часть истории удаляется.
2. **Сохранение инструкций (Recency Bias):** ключевые директивы системного промпта (`C++17`, `POSIX`, `Zero-Allocation`) дублируются в конец последнего сообщения пользователя для предотвращения потери контекста (Threshold Decay).
3. **Отмена задач на бэкенде:** при разрыве клиентского соединения шлюз закрывает TCP-сокет к llama-server, что прерывает декодирование на стороне сервера, и отправляет запрос `POST /slots/{id}?action=erase` для очистки KV-кэша.
4. **Маршрутизация и Circuit Breaker:** запросы распределяются по портам: автодополнение — 8081 (1.5B), код — 8080 (7B), системные задачи — 8082 (32B). При трех последовательных таймаутах на порту 8082 автомат перенаправляет трафик на порт 8080 с добавлением тегов `<think>`.
5. **Потребление памяти:** шлюз написан на `asyncio.start_server` без сторонних веб-фреймворков. Вызовы `malloc_trim(0)` удерживают процесс в пределах 13–26 МБ RSS при лимите cgroups v2 `MemoryMax=128M`.

## Установка и запуск

Требования: запущенный сервис [amdgpu-sentinel](https://github.com/BannedForWhat/amdgpu-sentinel) и инстансы llama-server на портах 8080, 8081, 8082.

```bash
git clone https://github.com/BannedForWhat/prompt-mesh.git
cd prompt-mesh
make install

# Запуск службы в сеансе пользователя
systemctl --user enable --now prompt-mesh.service
systemctl --user status prompt-mesh.service
```

## Метрики Prometheus

Эндпоинт `GET http://127.0.0.1:8000/metrics` отдает статистику работы:

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
