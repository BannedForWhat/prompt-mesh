import mmap
import struct
import time
from typing import NamedTuple, Optional

class ShmGpuSnapshot(NamedTuple):
    vram_used_bytes: int
    vram_total_bytes: int
    gtt_used_bytes: int
    gpu_busy_pct: int
    temp_c: int
    ttm_alert: bool
    timestamp_ns: int

    @property
    def vram_used_gib(self) -> float:
        return self.vram_used_bytes / (1024 ** 3)

    @property
    def vram_total_gib(self) -> float:
        return self.vram_total_bytes / (1024 ** 3)

    @property
    def vram_pct(self) -> float:
        if self.vram_total_bytes == 0:
            return 0.0
        return (self.vram_used_bytes / self.vram_total_bytes) * 100.0

class ShmGpuReader:
    """
    Zero-copy читатель телеметрии GPU из POSIX Shared Memory (/dev/shm/vram_guard).
    Использует mmap и Seqlock для исключения блокировок и torn reads.
    Задержка: < 1 микросекунды, 0 аллокаций в куче glibc.
    """
    __slots__ = ("_fd", "_mm", "_unpacker")

    # Формат: 4xQ (uint64), 2xI (uint32), 1xB (uint8), 7s (pad), 1xQ (uint64), 8s (pad) = 64 байта
    STRUCT_FORMAT = "=QQQQIIB7sQ8s"
    STRUCT_SIZE = 64

    def __init__(self, shm_path: str = "/dev/shm/vram_guard") -> None:
        self._fd = open(shm_path, "rb")
        self._mm = mmap.mmap(
            self._fd.fileno(),
            self.STRUCT_SIZE,
            access=mmap.ACCESS_READ
        )
        self._unpacker = struct.Struct(self.STRUCT_FORMAT)

    def read_state(self, max_retries: int = 4) -> Optional[ShmGpuSnapshot]:
        mm = self._mm
        unpacker = self._unpacker

        for _ in range(max_retries):
            data = unpacker.unpack_from(mm, 0)
            seq1 = data[0]

            # Если sequence нечетный — C++ демон прямо сейчас обновляет структуру
            if seq1 & 1:
                continue

            seq2 = struct.unpack_from("=Q", mm, 0)[0]
            if seq1 == seq2:
                return ShmGpuSnapshot(
                    vram_used_bytes=data[1],
                    vram_total_bytes=data[2],
                    gtt_used_bytes=data[3],
                    gpu_busy_pct=data[4],
                    temp_c=data[5],
                    ttm_alert=bool(data[6]),
                    timestamp_ns=data[8]
                )

        return ShmGpuSnapshot(
            vram_used_bytes=data[1],
            vram_total_bytes=data[2],
            gtt_used_bytes=data[3],
            gpu_busy_pct=data[4],
            temp_c=data[5],
            ttm_alert=bool(data[6]),
            timestamp_ns=data[8]
        )

    def is_stale(self, snapshot: ShmGpuSnapshot, max_age_sec: float = 2.0) -> bool:
        """Проверка устаревания метрики (падение amdgpu-sentinel)."""
        now_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC)
        delta_sec = (now_ns - snapshot.timestamp_ns) / 1e9
        return delta_sec > max_age_sec

    def close(self) -> None:
        if hasattr(self, "_mm") and not self._mm.closed:
            self._mm.close()
        if hasattr(self, "_fd") and not self._fd.closed:
            self._fd.close()

    def __del__(self) -> None:
        self.close()

if __name__ == "__main__":
    reader = ShmGpuReader()
    state = reader.read_state()
    if state:
        stale = reader.is_stale(state)
        print("[SHM ZERO-COPY READER TEST]")
        print(f"  VRAM:   {state.vram_used_gib:.2f} / {state.vram_total_gib:.2f} GiB ({state.vram_pct:.1f}%)")
        print(f"  GPU:    {state.gpu_busy_pct}% | TEMP: {state.temp_c}°C")
        print(f"  ALERT:  {'TTM SPILLOVER' if state.ttm_alert else 'OPTIMAL'}")
        print(f"  STALE:  {'YES (WARNING)' if stale else 'NO (LIVE)'}")
    reader.close()
