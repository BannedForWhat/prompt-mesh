import asyncio
import json
import socket
import ctypes
import os
import time
from typing import Dict, Any, Tuple

from shm_reader import ShmGpuReader
from pruner import middle_out_prune
from router import IntentRouter, ModelEndpoints
from circuit import CircuitBreaker

# ---------------------------------------------------------------------------
# Glibc Heap Memory Watchdog (защита от cgroups MemoryMax=128M)
# ---------------------------------------------------------------------------
try:
    _libc = ctypes.CDLL("libc.so.6")
    _has_malloc_trim = hasattr(_libc, "malloc_trim")
except Exception:
    _has_malloc_trim = False

def get_current_rss_mb() -> float:
    try:
        with open("/proc/self/statm", "r") as f:
            rss_pages = int(f.read().split()[1])
            return (rss_pages * os.sysconf("SC_PAGE_SIZE")) / (1024 * 1024)
    except Exception:
        return 0.0

def trim_glibc_heap() -> None:
    if _has_malloc_trim:
        _libc.malloc_trim(0)

async def memory_watchdog_task(threshold_mb: float = 70.0, interval_sec: float = 15.0) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        rss = get_current_rss_mb()
        if rss > threshold_mb:
            await asyncio.to_thread(trim_glibc_heap)

# ---------------------------------------------------------------------------
# Платформенный шлюз prompt-mesh
# ---------------------------------------------------------------------------
class PromptMeshGateway:
    def __init__(self, host: str = "127.0.0.1", port: int = 8000) -> None:
        self.host = host
        self.port = port
        self.shm_reader = ShmGpuReader()
        self.router = IntentRouter()
        self.cb_32b = CircuitBreaker(failure_threshold=3, recovery_timeout_sec=30.0)
        
        # Метрики наблюдаемости
        self.total_requests = 0
        self.pruned_requests = 0
        self.routed_7b = 0
        self.routed_1_5b = 0
        self.routed_32b = 0
        self.cb_fallbacks = 0

    async def start(self) -> None:
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        asyncio.create_task(memory_watchdog_task())
        print(f"[PROMPT-MESH] Gateway active on http://{self.host}:{self.port}")
        print(f"[PROMPT-MESH] OpenAI API endpoint: http://{self.host}:{self.port}/v1/chat/completions")
        print(f"[PROMPT-MESH] Metrics endpoint:    http://{self.host}:{self.port}/metrics")
        async with server:
            await server.serve_forever()

    async def handle_client(
        self, 
        client_reader: asyncio.StreamReader, 
        client_writer: asyncio.StreamWriter
    ) -> None:
        # Установка TCP_NODELAY для микросекундной отдачи первого токена
        client_sock = client_writer.transport.get_extra_info("socket")
        if client_sock is not None:
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        upstream_writer: asyncio.StreamWriter = None
        try:
            # 1. Чтение HTTP-заголовков запроса
            header_lines = []
            while True:
                line = await client_reader.readline()
                if not line or line == b"\r\n":
                    break
                header_lines.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))

            if not header_lines:
                client_writer.close()
                await client_writer.wait_closed()
                return

            request_line = header_lines[0].split()
            if len(request_line) < 2:
                client_writer.close()
                return

            method, path = request_line[0], request_line[1]

            # 2. Обработка служебных эндпоинтов
            if method == "GET" and path == "/health":
                await self.send_json_response(client_writer, 200, {"status": "ok", "service": "prompt-mesh"})
                return

            if method == "GET" and path == "/metrics":
                await self.send_metrics_response(client_writer)
                return

            if method != "POST":
                await self.send_json_response(client_writer, 405, {"error": "Method Not Allowed"})
                return

            # Чтение Content-Length
            content_length = 0
            for h in header_lines[1:]:
                if h.lower().startswith("content-length:"):
                    content_length = int(h.split(":")[1].strip())
                    break

            body_bytes = await client_reader.readexactly(content_length) if content_length > 0 else b"{}"
            body_json = json.loads(body_bytes.decode("utf-8", errors="replace"))

            # 3. Hardware-Aware Load Shedding (VRAM-Guard)
            gpu_state = self.shm_reader.read_state()
            if gpu_state and (gpu_state.ttm_alert or gpu_state.vram_pct > 85.0):
                if "messages" in body_json:
                    pruned_msgs, was_pruned, _ = middle_out_prune(body_json["messages"], max_allowed_tokens=4096)
                    if was_pruned:
                        body_json["messages"] = pruned_msgs
                        self.pruned_requests += 1

            # 4. Классификация намерений и Circuit Breaker
            target_port, reason = self.router.classify_request(path, body_json)
            
            # Маршрутизация 32B с защитой Circuit Breaker
            if target_port == ModelEndpoints.PORT_32B_DEEP:
                if not self.cb_32b.can_execute():
                    # Фоллбэк на 7B с инъекцией принудительного мышления
                    target_port = ModelEndpoints.PORT_7B_CHAT
                    self.cb_fallbacks += 1
                    if "messages" in body_json:
                        body_json["messages"] = self.cb_32b.inject_fallback_thinking(body_json["messages"])
                else:
                    self.routed_32b += 1

            if target_port == ModelEndpoints.PORT_7B_CHAT:
                self.routed_7b += 1
            elif target_port == ModelEndpoints.PORT_1_5B_FIM:
                self.routed_1_5b += 1

            self.total_requests += 1

            # 5. Проксирование на целевой порт llama-server
            new_body_bytes = json.dumps(body_json).encode("utf-8")
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", target_port),
                timeout=2.0
            )

            # Отправка HTTP-запроса на бэкенд
            req_headers = (
                f"POST {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{target_port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(new_body_bytes)}\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("utf-8")
            upstream_writer.write(req_headers + new_body_bytes)
            await upstream_writer.drain()

            # 6. Чтение ответа и сквозной SSE-стриминг без буферизации строк
            while not upstream_reader.at_eof():
                chunk = await upstream_reader.read(1024)
                if not chunk:
                    break
                client_writer.write(chunk)
                await client_writer.drain()

            if target_port == ModelEndpoints.PORT_32B_DEEP:
                self.cb_32b.record_success()

        except asyncio.CancelledError:
            # Active Cancellation: клиент нажал Cancel в VS Code
            if upstream_writer is not None and not upstream_writer.is_closing():
                upstream_writer.close()
                await upstream_writer.wait_closed()
            raise

        except Exception as e:
            if target_port == ModelEndpoints.PORT_32B_DEEP:
                self.cb_32b.record_failure()
            try:
                await self.send_json_response(client_writer, 502, {"error": "Upstream error", "details": str(e)})
            except Exception:
                pass

        finally:
            if upstream_writer is not None and not upstream_writer.is_closing():
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except Exception:
                    pass

            if not client_writer.is_closing():
                client_writer.close()
                try:
                    await client_writer.wait_closed()
                except Exception:
                    pass

    async def send_json_response(self, writer: asyncio.StreamWriter, status: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        resp = (
            f"HTTP/1.1 {status} OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body
        writer.write(resp)
        await writer.drain()

    async def send_metrics_response(self, writer: asyncio.StreamWriter) -> None:
        gpu = self.shm_reader.read_state()
        vram_pct = gpu.vram_pct if gpu else 0.0
        ttm_alert = 1 if (gpu and gpu.ttm_alert) else 0
        rss_mb = get_current_rss_mb()

        metrics_text = (
            f"# HELP prompt_mesh_requests_total Total requests processed\n"
            f"# TYPE prompt_mesh_requests_total counter\n"
            f"prompt_mesh_requests_total {self.total_requests}\n"
            f"prompt_mesh_pruned_total {self.pruned_requests}\n"
            f"prompt_mesh_routed_7b {self.routed_7b}\n"
            f"prompt_mesh_routed_1_5b {self.routed_1_5b}\n"
            f"prompt_mesh_routed_32b {self.routed_32b}\n"
            f"prompt_mesh_cb_fallbacks {self.cb_fallbacks}\n"
            f"prompt_mesh_rss_mb {rss_mb:.2f}\n"
            f"prompt_mesh_gpu_vram_pct {vram_pct:.1f}\n"
            f"prompt_mesh_gpu_ttm_alert {ttm_alert}\n"
        )
        body = metrics_text.encode("utf-8")
        resp = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: text/plain; version=0.0.4\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8") + body
        writer.write(resp)
        await writer.drain()

if __name__ == "__main__":
    gateway = PromptMeshGateway()
    try:
        asyncio.run(gateway.start())
    except KeyboardInterrupt:
        print("\n[PROMPT-MESH] Gateway stopped.")
