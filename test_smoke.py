"""Smoke tests rodando integração ponta-a-ponta com MockTransport."""

import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.diagnostics import DiagnosticsService
from src.pids import PidRegistry, extract_payload
from src.pids.decoders import rpm, fuel_trim, temp_a_minus_40, o2_narrow, voltage_ab
from src.protocol import Elm327Protocol, ResponseStatus
from src.scheduler import LoggerScheduler, SchedulerConfig
from src.storage import SqliteStorage, TelemetrySample
from src.transport.mock import MockTransport


def test_decoders():
    print("=== test_decoders ===")
    # 010C com payload '0C5E' = (0x0C * 256 + 0x5E) / 4 = (3072 + 94)/4 = 791.5 RPM
    assert rpm("0C5E") == 791.5, f"rpm 0C5E esperado 791.5, veio {rpm('0C5E')}"
    # 0106 com '80' = (128 * 100/128) - 100 = 0%
    assert fuel_trim("80") == 0.0
    # 0105 com '5A' = 0x5A - 40 = 90 - 40 = 50
    assert temp_a_minus_40("5A") == 50.0
    # 0114 com 'C8' = 0xC8 * 0.005 = 1.0V
    assert o2_narrow("C8") == 1.0
    # 0142 com '35EC' = (0x35*256 + 0xEC)/1000 = 13804/1000 = 13.804V
    assert abs(voltage_ab("35EC") - 13.804) < 0.001
    print("OK decoders")


def test_registry():
    print("=== test_registry ===")
    reg = PidRegistry("src/config/pids.yaml")
    rpm_def = reg.get("010C")
    assert rpm_def is not None
    assert rpm_def.name == "engine_rpm"
    assert rpm_def.priority == 1
    assert rpm_def.response_prefix == "410C"
    payload = extract_payload("410C0C5E", rpm_def)
    assert payload == "0C5E", f"esperado 0C5E veio '{payload}'"
    assert rpm_def.decoder(payload) == 791.5
    print(f"OK registry. Total PIDs: {len(reg.all())}")


def test_protocol_mock():
    print("=== test_protocol_mock ===")
    tx = MockTransport()
    tx.connect()
    proto = Elm327Protocol(tx, default_timeout_s=2.0)
    ok, msg = proto.initialize()
    print(f"  init: ok={ok} msg={msg}")
    assert ok, msg
    # Lê RPM várias vezes
    for _ in range(3):
        r = proto.send("010C")
        print(f"  010C → status={r.status.value} cleaned={r.cleaned[:40]} {r.elapsed_ms:.0f}ms")
        if r.status == ResponseStatus.SUCCESS:
            assert r.cleaned.startswith("410C")
    print("OK protocol mock")


def test_diagnostics():
    print("=== test_diagnostics ===")
    import random
    random.seed(42)  # determinístico
    tx = MockTransport(simulate_latency_ms=(10, 30))
    tx.connect()
    proto = Elm327Protocol(tx)
    ok, msg = proto.initialize()
    print(f"  init: ok={ok} protocolo={proto.detected_protocol}")
    service = DiagnosticsService(proto)
    report = service.run_full_scan(progress_cb=lambda m: print(f"  {m}"))
    print(f"  Protocolo: {report.protocol}")
    print(f"  VIN: {report.vin}")
    print(f"  PIDs suportados: {len(report.supported_pids)} primeiros 8: {report.supported_pids[:8]}")
    print(f"  DTCs armazenados: {report.stored_dtcs}")
    assert report.protocol is not None
    assert len(report.supported_pids) > 0
    print("OK diagnostics")


def test_scheduler_full():
    print("=== test_scheduler_full ===")
    tx = MockTransport()
    tx.connect()
    proto = Elm327Protocol(tx)
    proto.initialize()

    import os
    db_path = os.path.join(os.getcwd(), "data", "test_obd.db")
    # Certifique-se de que a pasta existe antes de conectar:
    os.makedirs(os.path.join(os.getcwd(), "data"), exist_ok=True)

    reg = PidRegistry("src/config/pids.yaml")
    db_path = Path("C:\dev\AT_Android\data\obd_data.db")
    if db_path.exists():
        db_path.unlink()
    print(f"DEBUG DB PATH: {db_path}")
    storage = SqliteStorage(db_path)

    cfg = SchedulerConfig(min_pacing_ms=5, max_pacing_ms=50, batch_write_interval_s=0.5)
    sched = LoggerScheduler(proto, reg, storage, cfg)

    samples_received = []

    def on_sample(s: TelemetrySample):
        samples_received.append(s)

    def on_error(e):
        print(f"  ERR: {e}")

    session_id = f"test_{uuid.uuid4().hex[:6]}"
    sched.start(session_id, on_sample, on_error, {"protocol": "ISO 9141-2 mock"})
    time.sleep(2.5)
    sched.stop()

    csv_path = os.path.join(os.getcwd(), "data", "test_obd.csv")
    rows = storage.export_csv(session_id, csv_path)
    stats = storage.session_stats(session_id)
    print(f"  Amostras callback: {len(samples_received)}")
    print(f"  Linhas CSV: {rows}")
    print(f"  Confiabilidade: {stats['reliability']*100:.1f}%")
    print(f"  Latência média: {stats['avg_delay_ms']:.1f}ms")
    print(f"  Pacing final: {sched.current_pacing_ms:.1f}ms")
    assert rows > 0
    assert len(samples_received) > 0
    storage.close()
    print("OK scheduler full")


def test_dtc_decoder():
    print("=== test_dtc_decoder ===")
    from src.diagnostics.scanner import DiagnosticsService
    # P0301 = 0301 hex. byte_a=03 → letter bits=00 → P, first=00=0, second=03=3, byte_b=01 → "P0301"
    assert DiagnosticsService._decode_dtc("0301") == "P0301"
    # C1234 → byte_a=52 (0101 0010 = letra 01=C, first=01=1, second=0010=2) byte_b=34 → "C1234"
    # 52 = 0x52 = 01010010 → letter (52>>6)=01=C, first=(52&30)>>4=01=1, second=52&0F=02
    assert DiagnosticsService._decode_dtc("5234") == "C1234"
    print("OK DTC decoder")


if __name__ == "__main__":
    test_decoders()
    test_registry()
    test_dtc_decoder()
    test_protocol_mock()
    test_diagnostics()
    test_scheduler_full()
    print("\n=== TODOS TESTES PASSARAM ===")
