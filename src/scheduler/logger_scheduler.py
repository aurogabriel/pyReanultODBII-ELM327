"""Scheduler de leitura. SRP: decide quem ler quando.
Pacing adaptativo: se atraso médio sobe, espaça leituras.

Modelo:
- Cada PID tem priority (1/2/3).
- Loop infinito tick. A cada tick decide quais PIDs ler com base em prioridade vs contador.
- Mede latência rolante (janela 10 amostras). Ajusta sleep entre comandos.
"""

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from ..pids import PidDefinition, PidRegistry, extract_payload
from ..protocol import Elm327Protocol, ResponseStatus
from ..storage import IStorage, TelemetrySample


@dataclass
class SchedulerConfig:
    min_pacing_ms: float = 20.0
    max_pacing_ms: float = 250.0
    latency_window: int = 10
    high_latency_threshold_ms: float = 200.0
    batch_write_size: int = 25
    batch_write_interval_s: float = 2.0


SampleCallback = Callable[[TelemetrySample], None]
ErrorCallback = Callable[[str], None]


class LoggerScheduler:
    def __init__(
        self,
        protocol: Elm327Protocol,
        registry: PidRegistry,
        storage: IStorage,
        config: Optional[SchedulerConfig] = None,
    ):
        self._proto = protocol
        self._reg = registry
        self._storage = storage
        self._cfg = config or SchedulerConfig()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._on_sample: Optional[SampleCallback] = None
        self._on_error: Optional[ErrorCallback] = None

        self._tick = 0
        self._latency_window: deque[float] = deque(maxlen=self._cfg.latency_window)
        self._current_pacing_ms = self._cfg.min_pacing_ms

        self._session_id = ""
        self._pending_batch: list[TelemetrySample] = []
        self._last_flush_t = 0.0
        self._supported_filter: Optional[set[str]] = None

    def set_supported_filter(self, supported_pids: list[str]) -> None:
        """Restringe leitura aos PIDs descobertos pelo scan."""
        self._supported_filter = {p.upper() for p in supported_pids}

    def start(
        self,
        session_id: str,
        on_sample: SampleCallback,
        on_error: ErrorCallback,
        session_metadata: dict,
    ) -> None:
        if self._running:
            return
        self._session_id = session_id or str(uuid.uuid4())
        self._on_sample = on_sample
        self._on_error = on_error
        self._storage.open_session(self._session_id, session_metadata)
        self._running = True
        self._tick = 0
        self._last_flush_t = time.perf_counter()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._flush_batch(force=True)

    def is_running(self) -> bool:
        return self._running

    @property
    def current_pacing_ms(self) -> float:
        return self._current_pacing_ms

    @property
    def avg_latency_ms(self) -> float:
        return sum(self._latency_window) / len(self._latency_window) if self._latency_window else 0.0

    def _should_read(self, pid_def: PidDefinition) -> bool:
        if self._supported_filter and pid_def.code not in self._supported_filter:
            return False
        if pid_def.priority == 1:
            return True
        if pid_def.priority == 2:
            return self._tick % 3 == 0
        if pid_def.priority == 3:
            return self._tick % 12 == 0
        return False

    def _loop(self) -> None:
        try:
            while self._running:
                self._tick += 1
                pids_this_tick = [p for p in self._reg.all() if self._should_read(p)]

                for pid_def in pids_this_tick:
                    if not self._running:
                        break
                    sample = self._read_single(pid_def)
                    self._pending_batch.append(sample)
                    if self._on_sample:
                        try:
                            self._on_sample(sample)
                        except RuntimeError:
                            # Sessão Flet destruída (janela fechada) — encerra o loop
                            self._running = False
                            return
                        except Exception as e:
                            try:
                                if self._on_error:
                                    self._on_error(f"callback erro: {e}")
                            except RuntimeError:
                                self._running = False
                                return

                    self._adjust_pacing()
                    time.sleep(self._current_pacing_ms / 1000.0)

                self._maybe_flush()
        except Exception as e:
            try:
                if self._on_error:
                    self._on_error(f"loop crashou: {e}")
            except RuntimeError:
                pass  # janela já fechada
            self._running = False

    def _read_single(self, pid_def: PidDefinition) -> TelemetrySample:
        response = self._proto.send(pid_def.code)
        self._latency_window.append(response.elapsed_ms)

        parsed_value: Optional[float] = None
        status = response.status.value

        if response.status == ResponseStatus.SUCCESS:
            payload = extract_payload(response.cleaned, pid_def)
            if payload is None:
                status = ResponseStatus.CORRUPTED.value
            else:
                try:
                    parsed_value = pid_def.decoder(payload)
                    if parsed_value is None:
                        status = ResponseStatus.CORRUPTED.value
                except Exception:
                    status = ResponseStatus.CORRUPTED.value

        return TelemetrySample(
            session_id=self._session_id,
            pid=pid_def.code,
            name=pid_def.name,
            raw_response=response.cleaned,
            parsed_value=parsed_value,
            unit=pid_def.unit,
            timestamp=time.time(),
            transport_delay_ms=response.elapsed_ms,
            status=status,
        )

    def _adjust_pacing(self) -> None:
        avg = self.avg_latency_ms
        if avg > self._cfg.high_latency_threshold_ms:
            # Buffer estourando — aumenta pacing
            self._current_pacing_ms = min(
                self._cfg.max_pacing_ms,
                self._current_pacing_ms * 1.2,
            )
        else:
            # Saudável — relaxa pacing rumo ao mínimo
            self._current_pacing_ms = max(
                self._cfg.min_pacing_ms,
                self._current_pacing_ms * 0.95,
            )

    def _maybe_flush(self) -> None:
        size_ok = len(self._pending_batch) >= self._cfg.batch_write_size
        time_ok = (time.perf_counter() - self._last_flush_t) >= self._cfg.batch_write_interval_s
        if size_ok or time_ok:
            self._flush_batch()

    def _flush_batch(self, force: bool = False) -> None:
        if not self._pending_batch and not force:
            return
        try:
            self._storage.write_batch(self._pending_batch)
        except Exception as e:
            if self._on_error:
                self._on_error(f"persist falhou: {e}")
        finally:
            self._pending_batch.clear()
            self._last_flush_t = time.perf_counter()
