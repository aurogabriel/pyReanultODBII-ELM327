"""Transport com injeção de falhas para testes. Padrão Decorator sobre IObdTransport.

Simula condições adversas de comunicação Bluetooth/CAN:
  - no_response  : ECU completamente silencioso (ECU dormindo, PID não suportado)
  - packet_loss  : frame chega mas é descartado (CRC erro, dropout RF)
  - truncate     : frame cortado antes do '>' (buffer overflow, desconexão parcial)
  - corrupt      : bytes embaralhados, '>' preservado (ruído RF, interferência)
"""

import random
from dataclasses import dataclass
from typing import Optional

from .base import IObdTransport


@dataclass
class FaultConfig:
    """Probabilidades de falha por comando OBD, avaliadas em cascata.

    Ordem de precedência: no_response → packet_loss → truncate → corrupt.
    Taxas independentes: cada uma é testada separadamente no dado roll.
    """
    no_response_rate: float = 0.0    # ECU silencioso → TIMEOUT
    packet_loss_rate: float = 0.0    # Frame descartado → TIMEOUT
    truncate_rate: float = 0.0       # Frame sem prompt → TIMEOUT (+ tentativa partial)
    corrupt_rate: float = 0.0        # Bytes embaralhados, '>' preservado → CORRUPTED
    at_immune: bool = True           # AT commands imunes (processados pelo ELM localmente)
    seed: Optional[int] = None       # None = não-determinístico


class FaultInjectTransport(IObdTransport):
    """Decorator sobre IObdTransport que injeta falhas configuráveis por comando.

    Uso:
        cfg = FaultConfig(packet_loss_rate=0.2, seed=42)
        tx  = FaultInjectTransport(MockTransport(), cfg)
        proto = Elm327Protocol(tx, default_timeout_s=0.3)
        ok, _ = proto.initialize()

        # Para mudar config após inicialização (ex: init limpo, faults só no teste):
        tx.configure(FaultConfig(no_response_rate=0.5, seed=42))
    """

    def __init__(self, inner: IObdTransport, cfg: FaultConfig = FaultConfig()):
        self._inner = inner
        self._cfg = cfg
        self._rng = random.Random(cfg.seed)
        self._active_fault = ""
        self.fault_counts: dict[str, int] = {
            "no_response": 0,
            "packet_loss": 0,
            "truncate":    0,
            "corrupt":     0,
            "normal":      0,
        }

    # ── IObdTransport ──────────────────────────────────────────────────────────

    def connect(self) -> bool:
        return self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def is_connected(self) -> bool:
        return self._inner.is_connected()

    def write(self, data: bytes) -> int:
        n = self._inner.write(data)
        cmd = data.decode("ascii", errors="ignore").strip().upper()
        if self._cfg.at_immune and cmd.startswith("AT"):
            self._active_fault = ""
            self.fault_counts["normal"] += 1
        else:
            self._active_fault = self._roll_fault()
        return n

    def read(self, max_bytes: int = 256) -> bytes:
        raw = self._inner.read(max_bytes)

        if self._active_fault in ("no_response", "packet_loss"):
            # Consome da fila interna para não vazar para o próximo comando
            return b""

        if self._active_fault == "truncate":
            return self._do_truncate(raw)

        if self._active_fault == "corrupt":
            return self._do_corrupt(raw)

        return raw

    def flush(self) -> None:
        self._inner.flush()

    # ── Configuração ───────────────────────────────────────────────────────────

    def configure(self, cfg: FaultConfig) -> None:
        """Reconfigura o injetor. Útil para: init limpo → activate faults."""
        self._cfg = cfg
        self._rng = random.Random(cfg.seed)
        self.reset_counters()

    def reset_counters(self) -> None:
        for k in self.fault_counts:
            self.fault_counts[k] = 0

    # ── Diagnóstico ────────────────────────────────────────────────────────────

    @property
    def total_commands(self) -> int:
        return sum(self.fault_counts.values())

    def fault_summary(self) -> str:
        total = self.total_commands or 1
        parts = [f"total={total}"]
        for k, v in self.fault_counts.items():
            parts.append(f"{k}={v}({v/total*100:.0f}%)")
        return " | ".join(parts)

    # ── Internos ───────────────────────────────────────────────────────────────

    def _roll_fault(self) -> str:
        r = self._rng.random()
        c = self._cfg

        if r < c.no_response_rate:
            self.fault_counts["no_response"] += 1
            return "no_response"
        r -= c.no_response_rate

        if r < c.packet_loss_rate:
            self.fault_counts["packet_loss"] += 1
            return "packet_loss"
        r -= c.packet_loss_rate

        if r < c.truncate_rate:
            self.fault_counts["truncate"] += 1
            return "truncate"
        r -= c.truncate_rate

        if r < c.corrupt_rate:
            self.fault_counts["corrupt"] += 1
            return "corrupt"

        self.fault_counts["normal"] += 1
        return ""

    def _do_truncate(self, data: bytes) -> bytes:
        """Remove '>' e entrega somente metade inicial — força timeout no protocolo."""
        stripped = data.replace(b">", b"")
        if not stripped:
            return b""
        return stripped[: max(1, len(stripped) // 2)]

    def _do_corrupt(self, data: bytes) -> bytes:
        """Embaralha ~35% dos bytes de dados; preserva '>', CR e LF."""
        result = bytearray(data)
        noise = b"!@#$%GHIJKLMNghijklmnopqrs"
        for i in range(len(result)):
            if result[i] not in (ord(">"), ord("\r"), ord("\n"), ord(" ")):
                if self._rng.random() < 0.35:
                    result[i] = noise[self._rng.randint(0, len(noise) - 1)]
        return bytes(result)
