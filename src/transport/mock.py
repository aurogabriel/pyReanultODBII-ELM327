"""Mock Transport. LSP: substitui SppTransport pra rodar no desktop sem Android."""

import random
import time
from collections import deque

from .base import IObdTransport, TransportError


class MockTransport(IObdTransport):
    """Simula ECU Logan 2012. Útil pra testar scheduler/UI sem dongle."""

    def __init__(self, simulate_latency_ms: tuple[int, int] = (60, 180)):
        self._connected = False
        self._latency_range = simulate_latency_ms
        self._response_queue: deque[bytes] = deque()
        self._last_cmd = b""
        self._rpm = 850.0  # idle
        self._speed = 0.0
        self._coolant = 30.0
        self._t_last_cmd = 0.0

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def write(self, data: bytes) -> int:
        if not self._connected:
            raise TransportError("Mock desconectado")
        self._last_cmd = data.strip().upper()
        self._t_last_cmd = time.perf_counter()
        # Simula latência variável
        lat_ms = random.uniform(*self._latency_range)
        time.sleep(lat_ms / 1000.0)
        self._enqueue_response(self._last_cmd)
        return len(data)

    def _enqueue_response(self, cmd: bytes) -> None:
        cmd_str = cmd.decode("ascii", errors="ignore").replace("\r", "").strip()

        # AT commands NUNCA retornam NO_DATA (processados localmente pelo ELM)
        if cmd_str.startswith("AT"):
            if cmd_str == "ATZ":
                self._response_queue.append(b"ELM327 v1.5\r>")
            elif cmd_str == "ATDP":
                self._response_queue.append(b"ISO 9141-2\r>")
            elif cmd_str == "ATI":
                self._response_queue.append(b"ELM327 v1.5\r>")
            else:
                self._response_queue.append(b"OK\r>")
            return

        # Simulação de cenários de erro só para comandos OBD reais (5% chance)
        if random.random() < 0.05:
            self._response_queue.append(b"NO DATA\r>")
            return

        # PIDs modo 01
        if cmd_str == "0100":
            # bitmap PIDs 01-20 suportados (Logan típico)
            self._response_queue.append(b"41 00 BE 3F A8 13\r>")
            return
        if cmd_str == "0120":
            self._response_queue.append(b"41 20 80 07 B0 11\r>")
            return
        if cmd_str == "0140":
            self._response_queue.append(b"41 40 00 00 00 00\r>")
            return
        if cmd_str == "010C":
            # simula RPM variando
            self._rpm += random.uniform(-50, 60)
            self._rpm = max(700, min(6500, self._rpm))
            val = int(self._rpm * 4)
            a, b = (val >> 8) & 0xFF, val & 0xFF
            self._response_queue.append(f"41 0C {a:02X} {b:02X}\r>".encode())
            return
        if cmd_str == "010D":
            self._speed = max(0, min(180, self._speed + random.uniform(-2, 3)))
            self._response_queue.append(f"41 0D {int(self._speed):02X}\r>".encode())
            return
        if cmd_str == "0105":
            self._coolant = min(95, self._coolant + 0.1)
            a = int(self._coolant + 40)
            self._response_queue.append(f"41 05 {a:02X}\r>".encode())
            return
        if cmd_str == "0111":
            tp = int(255 * random.uniform(0.1, 0.4))
            self._response_queue.append(f"41 11 {tp:02X}\r>".encode())
            return
        if cmd_str == "0114":
            v = int(random.uniform(50, 200))
            self._response_queue.append(f"41 14 {v:02X} FF\r>".encode())
            return
        if cmd_str == "0142":
            mv = int(13800 + random.uniform(-200, 200))
            a, b = (mv >> 8) & 0xFF, mv & 0xFF
            self._response_queue.append(f"41 42 {a:02X} {b:02X}\r>".encode())
            return
        if cmd_str == "0106" or cmd_str == "0107":
            trim = random.uniform(-5, 5)
            a = int((trim + 100) * 128 / 100)
            self._response_queue.append(
                f"41 {cmd_str[2:]} {a:02X}\r>".encode()
            )
            return
        if cmd_str == "03":
            self._response_queue.append(b"43 00\r>")  # zero DTCs
            return
        if cmd_str == "07":
            self._response_queue.append(b"47 00\r>")
            return
        if cmd_str.startswith("0902"):
            # VIN simulado
            self._response_queue.append(
                b"49 02 01 39 33 56 53\r49 02 02 35 38 4C 42\r"
                b"49 02 03 52 36 35 32\r49 02 04 33 34 35 36\r"
                b"49 02 05 37 38 39 30\r>"
            )
            return

        self._response_queue.append(b"NO DATA\r>")

    def read(self, max_bytes: int = 256) -> bytes:
        if not self._connected:
            raise TransportError("Mock desconectado")
        if not self._response_queue:
            return b""
        chunk = self._response_queue.popleft()
        return chunk[:max_bytes]

    def flush(self) -> None:
        self._response_queue.clear()
