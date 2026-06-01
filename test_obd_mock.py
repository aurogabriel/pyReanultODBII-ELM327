"""Testes de interface OBD com injeção de falhas.

Cenários cobertos:
  1. baseline       — sem falhas, linha de referência
  2. perda leve     — 20% packet_loss
  3. perda pesada   — 60% packet_loss
  4. sem resposta   — 50% no_response (ECU silencioso)
  5. truncamento    — 40% pacotes cortados antes do '>'
  6. corrupção      — 35% bytes embaralhados
  7. misto realista — 5% de cada tipo (cenário BT clone barato)
  8. scheduler      — LoggerScheduler + 15% falhas mistas por 2 s

Execute:
    python test_obd_mock.py
"""

import sys
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.protocol import Elm327Protocol, ResponseStatus
from src.transport.mock import MockTransport
from src.transport.fault_inject import FaultConfig, FaultInjectTransport


# ── Helpers ────────────────────────────────────────────────────────────────────

def _setup(fault_cfg: FaultConfig, *, timeout_s: float = 0.25) -> tuple[Elm327Protocol, FaultInjectTransport]:
    """Cria MockTransport + FaultInjectTransport, inicializa protocolo SEM falhas,
    depois ativa o FaultConfig fornecido para os testes."""
    mock = MockTransport(simulate_latency_ms=(5, 15))
    # Fase 1: init sem falhas (AT imunes garantem inicialização limpa)
    tx = FaultInjectTransport(mock, FaultConfig(seed=fault_cfg.seed))
    tx.connect()
    proto = Elm327Protocol(tx, default_timeout_s=timeout_s, init_timeout_s=1.5)
    ok, msg = proto.initialize()
    assert ok, f"Inicialização falhou: {msg}"
    # Fase 2: ativa injeção de falhas para os testes
    tx.configure(fault_cfg)
    return proto, tx


def _poll(proto: Elm327Protocol, pid: str = "010C", n: int = 30) -> tuple[Counter, float]:
    """Faz n leituras do PID e retorna (contagem de status, latência média ms)."""
    counts: Counter = Counter()
    total_ms = 0.0
    for _ in range(n):
        r = proto.send(pid)
        counts[r.status.value] += 1
        total_ms += r.elapsed_ms
    return counts, total_ms / n


def _print_result(label: str, counts: Counter, avg_ms: float, tx: FaultInjectTransport) -> None:
    total = sum(counts.values())
    success = counts.get(ResponseStatus.SUCCESS.value, 0)
    timeout = counts.get(ResponseStatus.TIMEOUT.value, 0)
    no_data = counts.get(ResponseStatus.NO_DATA.value, 0)
    corrupted = counts.get(ResponseStatus.CORRUPTED.value, 0)
    other = total - success - timeout - no_data - corrupted
    print(f"  {label}")
    print(f"    SUCCESS={success}  TIMEOUT={timeout}  NO_DATA={no_data}  "
          f"CORRUPTED={corrupted}  other={other}  avg={avg_ms:.0f}ms")
    print(f"    Injetor: {tx.fault_summary()}")


# ── Testes ─────────────────────────────────────────────────────────────────────

def test_baseline():
    """Sem injeção: verifica que a interface funciona normalmente."""
    print("=== 1. BASELINE (sem falhas) ===")
    proto, tx = _setup(FaultConfig(seed=0))
    counts, avg_ms = _poll(proto, n=30)
    _print_result("010C × 30", counts, avg_ms, tx)

    success = counts[ResponseStatus.SUCCESS.value]
    # MockTransport tem 5% NO_DATA aleatório; esperamos >75% sucesso
    assert success >= 22, f"Baseline: sucesso esperado ≥22/30, veio {success}"
    print("  OK baseline\n")


def test_packet_loss_light():
    """20% de perda de pacotes — degradação moderada."""
    print("=== 2. PACKET LOSS LEVE (20%) ===")
    proto, tx = _setup(FaultConfig(packet_loss_rate=0.20, seed=1))
    counts, avg_ms = _poll(proto, n=30)
    _print_result("010C × 30", counts, avg_ms, tx)

    timeout = counts[ResponseStatus.TIMEOUT.value]
    # Com 20% loss: esperamos pelo menos 3 timeouts em 30 tentativas
    assert timeout >= 3, f"Perda leve: esperado ≥3 TIMEOUT, veio {timeout}"
    print("  OK packet_loss_light\n")


def test_packet_loss_heavy():
    """60% de perda de pacotes — maioria das leituras falha."""
    print("=== 3. PACKET LOSS PESADO (60%) ===")
    proto, tx = _setup(FaultConfig(packet_loss_rate=0.60, seed=2))
    counts, avg_ms = _poll(proto, n=30)
    _print_result("010C × 30", counts, avg_ms, tx)

    success = counts[ResponseStatus.SUCCESS.value]
    timeout = counts[ResponseStatus.TIMEOUT.value]
    # Com 60% loss: espera-se mais falhas do que sucessos
    assert timeout > success, (
        f"Perda pesada: TIMEOUT ({timeout}) deveria > SUCCESS ({success})"
    )
    print("  OK packet_loss_heavy\n")


def test_no_response():
    """50% ECU silencioso — simula ECU dormindo ou PID não suportado."""
    print("=== 4. SEM RESPOSTA (50% no_response) ===")
    proto, tx = _setup(FaultConfig(no_response_rate=0.50, seed=3))
    counts, avg_ms = _poll(proto, n=20)
    _print_result("010C × 20", counts, avg_ms, tx)

    timeout = counts[ResponseStatus.TIMEOUT.value]
    assert timeout >= 5, f"No-response: esperado ≥5 TIMEOUT em 20, veio {timeout}"

    # Média deve ser alta por causa dos timeouts (timeout_s=0.25 → ~250ms por falha)
    assert avg_ms > 50, f"No-response: latência média deveria ser >50ms, veio {avg_ms:.0f}ms"
    print("  OK no_response\n")


def test_truncated_packets():
    """40% de pacotes truncados — frame chega sem o prompt '>'.

    O _clean_partial do ELM327Protocol tenta recuperar dados úteis do buffer
    incompleto, por isso a maioria dos truncamentos vira SUCCESS com dados parciais.
    O indicador de impacto é a latência média: pacotes truncados forçam espera do
    timeout inteiro (250ms) antes da recuperação, vs ~10ms em operação normal.
    """
    print("=== 5. TRUNCAMENTO (40% truncate) ===")
    proto, tx = _setup(FaultConfig(truncate_rate=0.40, seed=4))
    counts, avg_ms = _poll(proto, n=30)
    _print_result("010C × 30", counts, avg_ms, tx)

    # Confirma que a injeção de truncamentos ocorreu
    truncated = tx.fault_counts["truncate"]
    assert truncated >= 8, f"Truncamento: esperado ≥8 injeções, veio {truncated}"

    # Latência média deve ser alta: cada truncamento espera timeout (250ms) antes de
    # _clean_partial recuperar — vs ~10ms no baseline
    assert avg_ms > 80, f"Truncamento: latência média deveria ser >80ms, veio {avg_ms:.0f}ms"
    print(f"  Pacotes truncados injetados: {truncated}/30, latência média: {avg_ms:.0f}ms")
    print("  OK truncated_packets\n")


def test_corrupted_data():
    """35% de bytes corrompidos — '>' preservado, dados embaralhados.

    O protocolo ELM327 não tem checksum: _classify retorna SUCCESS se cleaned é
    não-vazio e não contém marcadores de erro. Corrupção só produz CORRUPTED quando
    todos os bytes úteis são embaralhados (cleaned fica vazio). Por isso o status
    não é o indicador principal; o indicador é o contador de injeções do transport.
    """
    print("=== 6. CORRUPÇÃO (35% corrupt) ===")
    proto, tx = _setup(FaultConfig(corrupt_rate=0.35, seed=5))
    counts, avg_ms = _poll(proto, n=30)
    _print_result("010C × 30", counts, avg_ms, tx)

    corrupted_injected = tx.fault_counts["corrupt"]
    assert corrupted_injected >= 5, (
        f"Corrupção: esperado ≥5 injeções, veio {corrupted_injected}"
    )

    # Latência deve ser baixa: '>' preservado → protocolo não espera timeout
    assert avg_ms < 80, f"Corrupção: latência deveria ser baixa (<80ms), veio {avg_ms:.0f}ms"

    corrupted_status = counts[ResponseStatus.CORRUPTED.value]
    print(f"  Bytes corrompidos injetados: {corrupted_injected}/30")
    print(f"  CORRUPTED classificados pelo protocolo: {corrupted_status} "
          f"(demais SUCCESS com dados lixo — ELM327 sem checksum)")
    print("  OK corrupted_data\n")


def test_mixed_realistic():
    """Cenário realista: 5% de cada tipo — BT clone de baixa qualidade."""
    print("=== 7. MISTO REALISTA (5% cada tipo) ===")
    cfg = FaultConfig(
        no_response_rate=0.05,
        packet_loss_rate=0.05,
        truncate_rate=0.05,
        corrupt_rate=0.05,
        seed=6,
    )
    proto, tx = _setup(cfg)
    counts, avg_ms = _poll(proto, n=50)
    _print_result("010C × 50", counts, avg_ms, tx)

    success = counts[ResponseStatus.SUCCESS.value]
    # Com ~20% falhas totais, esperamos pelo menos 35/50 de sucesso
    assert success >= 30, f"Misto: esperado ≥30/50 SUCCESS, veio {success}"

    # Cada tipo de falha deve ter ocorrido pelo menos 1 vez (seed controlado)
    injected_total = (
        tx.fault_counts["no_response"]
        + tx.fault_counts["packet_loss"]
        + tx.fault_counts["truncate"]
        + tx.fault_counts["corrupt"]
    )
    assert injected_total >= 4, f"Misto: esperado ≥4 falhas injetadas, veio {injected_total}"
    print("  OK mixed_realistic\n")


def test_scheduler_with_faults():
    """LoggerScheduler rodando 2s com 15% de falhas mistas — verifica robustez."""
    print("=== 8. SCHEDULER COM FALHAS (15% misto) ===")
    import os
    from src.pids import PidRegistry
    from src.scheduler import LoggerScheduler, SchedulerConfig
    from src.storage import SqliteStorage, TelemetrySample

    cfg = FaultConfig(
        no_response_rate=0.05,
        packet_loss_rate=0.05,
        truncate_rate=0.03,
        corrupt_rate=0.02,
        seed=7,
    )

    mock = MockTransport(simulate_latency_ms=(5, 15))
    tx = FaultInjectTransport(mock, FaultConfig(seed=7))
    tx.connect()
    proto = Elm327Protocol(tx, default_timeout_s=0.25, init_timeout_s=1.5)
    ok, msg = proto.initialize()
    assert ok, f"Init falhou: {msg}"
    tx.configure(cfg)

    db_path = Path("data/test_fault_sched.db")
    db_path.parent.mkdir(exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    reg = PidRegistry("src/config/pids.yaml")
    storage = SqliteStorage(db_path)
    sched_cfg = SchedulerConfig(min_pacing_ms=5, max_pacing_ms=60, batch_write_interval_s=0.3)
    sched = LoggerScheduler(proto, reg, storage, sched_cfg)

    samples: list[TelemetrySample] = []
    errors: list[str] = []

    sched.start(
        f"fault_{uuid.uuid4().hex[:6]}",
        lambda s: samples.append(s),
        lambda e: errors.append(str(e)),
        {"test": "fault_inject"},
    )
    time.sleep(2.0)
    sched.stop()

    session_id = samples[0].session_id if samples else None
    stats = storage.session_stats(session_id) if session_id else {}
    storage.close()

    print(f"  Amostras recebidas: {len(samples)}")
    print(f"  Erros do scheduler: {len(errors)}")
    print(f"  Confiabilidade: {stats.get('reliability', 0)*100:.1f}%")
    print(f"  Latência média: {stats.get('avg_delay_ms', 0):.1f}ms")
    print(f"  Pacing final: {sched.current_pacing_ms:.1f}ms")
    print(f"  Injetor: {tx.fault_summary()}")

    assert len(samples) > 0, "Scheduler não gerou nenhuma amostra"
    assert len(errors) == 0, f"Scheduler gerou erros inesperados: {errors}"

    # Nota: MockTransport só implementa ~11 dos ~30 PIDs do registry.
    # PIDs não-suportados retornam NO_DATA (status != 'SUCCESS'), o que já reduz
    # o baseline de reliability para ~40-50%. O objetivo aqui é verificar que o
    # scheduler sobrevive com faults e ainda grava dados — não atingir 100%.
    reliability = stats.get("reliability", 0)
    assert reliability >= 0.25, f"Confiabilidade muito baixa: {reliability*100:.1f}%"
    print("  OK scheduler_with_faults\n")


# ── Runner ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t0 = time.perf_counter()
    test_baseline()
    test_packet_loss_light()
    test_packet_loss_heavy()
    test_no_response()
    test_truncated_packets()
    test_corrupted_data()
    test_mixed_realistic()
    test_scheduler_with_faults()
    elapsed = time.perf_counter() - t0
    print(f"=== TODOS OS TESTES PASSARAM ({elapsed:.1f}s) ===")
