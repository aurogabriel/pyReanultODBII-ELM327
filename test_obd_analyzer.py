"""Testes unitários para src/analysis/obd_session_analyzer.py.

Cobre os três bugs corrigidos:
  Bug 1 — FuelSystemAnalyzer usa PID 0103, não temperatura
  Bug 2 — TimingAnalyzer por regime; idle negativo ≠ knock
  Bug 3 — ThermalAnalyzer com critérios robustos + multi-sessão

Execute: python test_obd_analyzer.py
"""

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.analysis.obd_session_analyzer import (
    D4F_SPECS,
    DiagnosticResult,
    FuelSystemAnalyzer,
    PidRow,
    RegimeClassifier,
    Row,
    ThermalAnalyzer,
    TimingAnalyzer,
)


# ─── Fábrica de dados de teste ───────────────────────────────────────────────

def _pid_row(pid: str, raw: str, status: str = "SUCCESS", ts: float = 0.0) -> PidRow:
    return PidRow(pid=pid.upper(), raw_response=raw, status=status, timestamp=ts)


def _row(
    *,
    elapsed_s:       float = 0.0,
    timestamp:       float = 0.0,
    speed_kmh:       float | None = 0.0,
    rpm:             float | None = 800.0,
    accel_kmh_s:     float | None = 0.0,
    coolant_c:       float | None = 85.0,
    engine_load_pct: float | None = 20.0,
    timing_adv_deg:  float | None = None,
    m21_timing_deg:  float | None = None,
    m21_coolant_c:   float | None = None,
    m21_load_pct:    float | None = None,
    m21_knock_count: float | None = None,
    regime:          str = "",
) -> Row:
    return Row(
        elapsed_s=elapsed_s,
        timestamp=timestamp,
        speed_kmh=speed_kmh,
        rpm=rpm,
        accel_kmh_s=accel_kmh_s,
        coolant_c=coolant_c,
        engine_load_pct=engine_load_pct,
        timing_adv_deg=timing_adv_deg,
        m21_timing_deg=m21_timing_deg,
        m21_coolant_c=m21_coolant_c,
        m21_load_pct=m21_load_pct,
        m21_knock_count=m21_knock_count,
        regime=regime,
    )


def _hot_rows(n: int = 100, coolant: float = 80.0, start_elapsed: float = 0.0) -> list[Row]:
    """n linhas com motor quente (coolant > 70°C) distribuídas em n segundos."""
    return [
        _row(elapsed_s=start_elapsed + i, timestamp=start_elapsed + i,
             coolant_c=coolant)
        for i in range(n)
    ]


# ─── BUG 1 — FuelSystemAnalyzer usa PID 0103, não temperatura ───────────────

def test_fuel_system_uses_pid_0103_not_temperature():
    """Motor frio com PID 0103 = Closed Loop não deve gerar alerta de OL."""
    print("=== BUG 1a: motor frio + PID 0103 CL -> sem alerta ===")
    analyzer = FuelSystemAnalyzer()

    # Motor frio (coolant < 70°C) — período quente < 60 s
    cold_rows = [_row(elapsed_s=i, timestamp=i, coolant_c=50.0) for i in range(200)]

    # PID 0103 retorna 0x02 = Closed Loop
    pid_rows = [_pid_row("0103", "41030200", ts=float(i * 10)) for i in range(10)]

    result = analyzer.analyze(pid_rows, cold_rows)
    print(f"  code={result.code}  severity={result.severity}")
    assert result.severity != "CRITICO", (
        f"Motor frio não deve gerar CRITICO: {result.code}"
    )
    assert result.code in ("FUEL_LOOP_COLD_ONLY", "FUEL_LOOP_OK"), (
        f"Expected cold-start or OK, got {result.code}"
    )
    print("  OK\n")


def test_fuel_system_pid_0103_anomalous_when_hot():
    """PID 0103 = 0x01 (OL anômalo) com motor quente > 60 s deve gerar alerta."""
    print("=== BUG 1b: motor quente + PID 0103 = 0x01 -> alerta OL anômalo ===")
    analyzer = FuelSystemAnalyzer()

    # Motor quente por 120 s (elapsed 0–119, coolant = 80°C)
    hot_rows = [_row(elapsed_s=i, timestamp=i, coolant_c=80.0) for i in range(120)]

    # PID 0103 = 0x01 (Open Loop — condições não satisfeitas) durante período quente
    pid_rows = [_pid_row("0103", "41030100", ts=float(i * 5)) for i in range(20)]

    result = analyzer.analyze(pid_rows, hot_rows)
    print(f"  code={result.code}  severity={result.severity}  conf={result.confidence}")
    assert result.code == "FUEL_LOOP_ANOMALOUS", (
        f"Esperado FUEL_LOOP_ANOMALOUS, veio {result.code}"
    )
    assert result.severity in ("CRITICO", "AVISO"), (
        f"Severity deve ser CRITICO ou AVISO, veio {result.severity}"
    )
    print("  OK\n")


def test_fuel_system_no_pid_0103_returns_insufficient():
    """Sem PID 0103 no CSV de sessão -> DADO_INSUFICIENTE (não assume estado)."""
    print("=== BUG 1c: sem PID 0103 -> DADO_INSUFICIENTE ===")
    analyzer = FuelSystemAnalyzer()
    result = analyzer.analyze([], _hot_rows(100, 80.0))
    print(f"  code={result.code}  severity={result.severity}")
    assert result.severity == "DADO_INSUFICIENTE", (
        f"Esperado DADO_INSUFICIENTE, veio {result.severity}"
    )
    print("  OK\n")


# ─── BUG 2 — Timing por regime; idle negativo ≠ knock ───────────────────────

def test_timing_idle_negative_is_not_knock():
    """Timing -30° em idle é normal para o D4F; não deve gerar diagnóstico de knock."""
    print("=== BUG 2a: timing -30° em IDLE -> não é knock ===")
    analyzer = TimingAnalyzer()

    idle_rows = [
        _row(timing_adv_deg=-30.0, regime="IDLE", elapsed_s=float(i))
        for i in range(80)
    ]

    results = analyzer.analyze(idle_rows)
    idle_results = [r for r in results if r.regime == "IDLE"]
    print(f"  resultados IDLE: {[(r.code, r.severity) for r in idle_results]}")

    for r in results:
        assert "KNOCK" not in r.code, (
            f"Timing negativo em IDLE não deve gerar código KNOCK: {r.code}"
        )
        if r.regime == "IDLE":
            # Timing -30° está dentro do esperado (-40°..5°) para o D4F
            assert r.severity != "CRITICO", (
                f"IDLE com -30° não deve ser CRITICO: {r.severity}"
            )
    print("  OK\n")


def test_timing_idle_extreme_negative_triggers_warning():
    """Timing -50° em idle está além de -45° (threshold crítico); gera AVISO."""
    print("=== BUG 2b: timing -50° em IDLE -> AVISO (extremo) ===")
    analyzer = TimingAnalyzer()

    rows = [
        _row(timing_adv_deg=-50.0, regime="IDLE", elapsed_s=float(i))
        for i in range(60)
    ]

    results = analyzer.analyze(rows)
    idle = [r for r in results if r.regime == "IDLE"]
    print(f"  IDLE: {[(r.code, r.severity) for r in idle]}")

    assert idle, "Deve haver resultado para IDLE"
    assert idle[0].code == "TIMING_EXTREME_IDLE", (
        f"Esperado TIMING_EXTREME_IDLE, veio {idle[0].code}"
    )
    # Deve ser AVISO (não CRITICO — threshold capado por conf ou por regra do D4F)
    assert idle[0].severity in ("AVISO", "INFO"), (
        f"IDLE extremo deve ser AVISO, veio {idle[0].severity}"
    )
    print("  OK\n")


def test_timing_cruise_negative_triggers_alert():
    """Timing -10° em cruzeiro (<-5° threshold crítico) deve gerar AVISO ou CRITICO."""
    print("=== BUG 2c: timing -10° em CRUISE -> alerta ===")
    analyzer = TimingAnalyzer()

    # 60 amostras de cruzeiro com timing -10° (abaixo de -5° threshold crítico)
    rows = [
        _row(timing_adv_deg=-10.0, regime="CRUISE", elapsed_s=float(i))
        for i in range(60)
    ]

    results = analyzer.analyze(rows)
    cruise = [r for r in results if r.regime == "CRUISE"]
    print(f"  CRUISE: {[(r.code, r.severity) for r in cruise]}")

    assert cruise, "Deve haver resultado para CRUISE"
    assert cruise[0].severity in ("AVISO", "CRITICO"), (
        f"CRUISE com timing -10° deve ser AVISO ou CRITICO, veio {cruise[0].severity}"
    )
    assert cruise[0].code == "TIMING_LOW_CRUISE", (
        f"Esperado TIMING_LOW_CRUISE, veio {cruise[0].code}"
    )
    print("  OK\n")


def test_timing_load_knock_without_m21_is_low_confidence():
    """Knock em LOAD sem m21_knock_count -> confidence LOW e sem CRITICO."""
    print("=== BUG 2d: knock em LOAD sem m21_knock_count -> LOW confidence ===")
    analyzer = TimingAnalyzer()

    # 80 amostras sob carga com timing -15° (abaixo dos dois thresholds)
    rows = [
        _row(timing_adv_deg=-15.0, regime="LOAD", elapsed_s=float(i),
             m21_knock_count=None)   # sem contador de knock
        for i in range(80)
    ]

    results = analyzer.analyze(rows)
    load = [r for r in results if r.regime == "LOAD"]
    print(f"  LOAD: {[(r.code, r.severity, r.confidence) for r in load]}")

    assert load, "Deve haver resultado para LOAD"
    assert load[0].confidence == "LOW", (
        f"Sem m21_knock_count deve ser LOW confidence, veio {load[0].confidence}"
    )
    assert load[0].severity != "CRITICO", (
        "CRITICO com LOW confidence não deve ser emitido (deve ser capado para AVISO)"
    )
    print("  OK\n")


def test_timing_uses_m21_over_standard():
    """Quando m21_timing_deg disponível, deve ser usado em vez de timing_adv_deg."""
    print("=== BUG 2e: m21_timing_deg preferido sobre PID 010E ===")
    analyzer = TimingAnalyzer()

    # timing_adv_deg seria problemático (-20°), mas m21_timing_deg é normal (+15°)
    rows = [
        _row(timing_adv_deg=-20.0, m21_timing_deg=15.0, regime="CRUISE",
             elapsed_s=float(i))
        for i in range(60)
    ]

    results = analyzer.analyze(rows)
    cruise = [r for r in results if r.regime == "CRUISE"]
    print(f"  CRUISE: {[(r.code, r.severity, r.pid_used) for r in cruise]}")

    assert cruise, "Deve haver resultado para CRUISE"
    # Com m21=+15° (acima do threshold de 5°), deve ser INFO
    assert cruise[0].severity == "INFO", (
        f"Com m21_timing_deg=+15°, deve ser INFO, veio {cruise[0].severity}"
    )
    assert "Mode21" in cruise[0].pid_used, (
        f"PID usado deve mencionar Mode21, veio {cruise[0].pid_used}"
    )
    print("  OK\n")


# ─── BUG 3 — Thermostat com critérios robustos + multi-sessão ───────────────

def _session(
    n: int,
    max_coolant: float,
    duration_s: float = 1500.0,
    mean_speed: float = 40.0,
    load_s: float = 400.0,
    slope: float = 0.0,
) -> list[Row]:
    """Gera sessão sintética com parâmetros controlados."""
    rows: list[Row] = []
    step = duration_s / max(n, 1)

    for i in range(n):
        t = i * step
        # Temperatura cresce até max_coolant na 1ª metade e estabiliza
        frac = min(i / (n // 2), 1.0) if n > 1 else 1.0
        coolant = 30.0 + (max_coolant - 30.0) * frac
        # Slope na 2ª metade
        if i >= n // 2:
            extra = slope * (t - (n // 2) * step) / 60.0
            coolant = coolant + extra

        # Regime: alterna IDLE e LOAD para atingir load_s
        regime = "LOAD" if (i % 10 < (load_s / duration_s * 10)) else "IDLE"
        speed  = mean_speed if regime == "LOAD" else 0.0

        rows.append(_row(
            elapsed_s=t, timestamp=t,
            coolant_c=coolant,
            speed_kmh=speed,
            engine_load_pct=70.0 if regime == "LOAD" else 15.0,
            regime=regime,
        ))
    return rows


def test_thermostat_requires_load_and_duration():
    """Sessão curta ou perfil idle -> DADO_INSUFICIENTE, sem falso positivo."""
    print("=== BUG 3a: sessão curta -> DADO_INSUFICIENTE ===")
    analyzer = ThermalAnalyzer()

    # Sessão de 10 min (< 20 min mínimo)
    short = _session(n=100, max_coolant=75.0, duration_s=600.0, mean_speed=40.0)
    result = analyzer.analyze(short)
    print(f"  sessão curta: {result.code}  sev={result.severity}")
    assert result.severity == "DADO_INSUFICIENTE", (
        f"Sessão curta deve ser DADO_INSUFICIENTE, veio {result.severity}"
    )

    print("=== BUG 3b: perfil idle (velocidade média < 20 km/h) -> DADO_INSUFICIENTE ===")
    idle_session = _session(n=200, max_coolant=75.0, duration_s=1500.0, mean_speed=5.0)
    result = analyzer.analyze(idle_session)
    print(f"  perfil idle: {result.code}  sev={result.severity}")
    assert result.severity == "DADO_INSUFICIENTE", (
        f"Perfil idle deve ser DADO_INSUFICIENTE, veio {result.severity}"
    )

    print("=== BUG 3c: tempo em LOAD insuficiente -> DADO_INSUFICIENTE ===")
    no_load = _session(n=200, max_coolant=75.0, duration_s=1500.0,
                       mean_speed=40.0, load_s=60.0)   # só 1 min em carga
    result = analyzer.analyze(no_load)
    print(f"  pouco LOAD: {result.code}  sev={result.severity}")
    assert result.severity == "DADO_INSUFICIENTE", (
        f"Pouco tempo em LOAD deve ser DADO_INSUFICIENTE, veio {result.severity}"
    )

    print("  OK\n")


def test_thermostat_fault_detected_when_all_criteria_met():
    """Sessão com todas as condições para defeito deve retornar THERMOSTAT_FAULT."""
    print("=== BUG 3d: todas as condições de defeito -> THERMOSTAT_FAULT ===")
    analyzer = ThermalAnalyzer()

    # Sessão longa, com carga, temperatura estagnada em 75°C, slope ~0
    fault_session = _session(
        n=300, max_coolant=75.0,
        duration_s=1800.0,   # 30 min
        mean_speed=50.0,
        load_s=600.0,        # 10 min em carga
        slope=0.0,           # estagnada
    )
    result = analyzer.analyze(fault_session)
    print(f"  code={result.code}  sev={result.severity}  conf={result.confidence}")
    assert result.code == "THERMOSTAT_FAULT", (
        f"Esperado THERMOSTAT_FAULT, veio {result.code}"
    )
    assert result.severity in ("CRITICO", "AVISO"), (
        f"THERMOSTAT_FAULT deve ser CRITICO ou AVISO, veio {result.severity}"
    )
    print("  OK\n")


def test_thermostat_multisession_uses_hottest():
    """Com múltiplas sessões, usa a mais quente como referência.

    Sessão A sozinha teria temp baixa (77°C) -> aparência de defeito.
    Sessão B tem temp 88°C -> ≥ 85°C -> termostato OK com referência em B.
    """
    print("=== BUG 3e: multi-sessão usa a sessão mais quente como referência ===")
    analyzer = ThermalAnalyzer()

    session_a = _session(
        n=300, max_coolant=77.0,
        duration_s=1800.0, mean_speed=50.0, load_s=600.0, slope=0.0,
    )
    session_b = _session(
        n=250, max_coolant=88.0,
        duration_s=1500.0, mean_speed=60.0, load_s=500.0,
    )

    # Só a sessão A -> seria detectado como FAULT (temp 77°C, estagnada)
    result_alone = analyzer.analyze(session_a)
    print(f"  sessão A sozinha: {result_alone.code}  sev={result_alone.severity}")
    assert result_alone.code == "THERMOSTAT_FAULT", (
        f"Sessão A sozinha deve ser THERMOSTAT_FAULT, veio {result_alone.code}"
    )

    # Com sessão B como extra -> B tem 88°C ≥ 85°C -> OK
    result_multi = analyzer.analyze(session_a, others=[session_b])
    print(f"  A + B juntas:    {result_multi.code}  sev={result_multi.severity}")
    assert result_multi.code == "THERMOSTAT_OK", (
        f"Com sessão B (88°C), esperado THERMOSTAT_OK, veio {result_multi.code}"
    )
    assert "sessão adicional" in result_multi.evidence or "sessão" in result_multi.evidence, (
        "Evidência deve mencionar qual sessão foi usada como referência"
    )
    print("  OK\n")


def test_thermostat_single_session_85c_is_ok():
    """Sessão única que chega a 85°C não deve ser flagrada."""
    print("=== BUG 3f: sessão com 85°C -> THERMOSTAT_OK ===")
    analyzer = ThermalAnalyzer()
    rows = _session(n=200, max_coolant=87.0, duration_s=1500.0, mean_speed=50.0)
    result = analyzer.analyze(rows)
    print(f"  code={result.code}  sev={result.severity}")
    assert result.code == "THERMOSTAT_OK", (
        f"87°C deve ser THERMOSTAT_OK, veio {result.code}"
    )
    print("  OK\n")


# ─── Testes de RegimeClassifier ──────────────────────────────────────────────

def test_regime_classification():
    """Verifica classificação correta dos regimes."""
    print("=== RegimeClassifier: classificação de regimes ===")
    clf = RegimeClassifier()

    idle_row   = _row(speed_kmh=0,    rpm=850,  engine_load_pct=15.0)
    cruise_row = _row(speed_kmh=80,   rpm=2500, engine_load_pct=35.0)
    load_row   = _row(speed_kmh=60,   rpm=3000, engine_load_pct=75.0)
    accel_row  = _row(speed_kmh=30,   rpm=2000, engine_load_pct=50.0, accel_kmh_s=2.0)

    classified = clf.classify([idle_row, cruise_row, load_row, accel_row])
    print(f"  idle->{classified[0].regime}, cruise->{classified[1].regime}, "
          f"load->{classified[2].regime}, accel->{classified[3].regime}")

    assert classified[0].regime == "IDLE",    f"Esperado IDLE, veio {classified[0].regime}"
    assert classified[1].regime == "CRUISE",  f"Esperado CRUISE, veio {classified[1].regime}"
    assert classified[2].regime == "LOAD",    f"Esperado LOAD, veio {classified[2].regime}"
    assert classified[3].regime == "LOAD",    f"Aceleração > 1.5 deve ser LOAD, veio {classified[3].regime}"
    print("  OK\n")


def test_regime_uses_m21_load_over_standard():
    """RegimeClassifier deve preferir m21_load_pct quando disponível."""
    print("=== RegimeClassifier: m21_load_pct > engine_load_pct ===")
    clf = RegimeClassifier()

    # engine_load_pct = 30% (seria CRUISE), mas m21_load_pct = 70% -> LOAD
    row = _row(speed_kmh=60, rpm=2000, engine_load_pct=30.0, m21_load_pct=70.0)
    classified = clf.classify([row])
    print(f"  regime={classified[0].regime}")
    assert classified[0].regime == "LOAD", (
        f"m21_load_pct=70% deve resultar em LOAD, veio {classified[0].regime}"
    )
    print("  OK\n")


# ─── Runner ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time
    t0 = time.perf_counter()

    # Bug 1 — FuelSystemAnalyzer
    test_fuel_system_uses_pid_0103_not_temperature()
    test_fuel_system_pid_0103_anomalous_when_hot()
    test_fuel_system_no_pid_0103_returns_insufficient()

    # Bug 2 — TimingAnalyzer por regime
    test_timing_idle_negative_is_not_knock()
    test_timing_idle_extreme_negative_triggers_warning()
    test_timing_cruise_negative_triggers_alert()
    test_timing_load_knock_without_m21_is_low_confidence()
    test_timing_uses_m21_over_standard()

    # Bug 3 — ThermalAnalyzer multi-sessão
    test_thermostat_requires_load_and_duration()
    test_thermostat_fault_detected_when_all_criteria_met()
    test_thermostat_multisession_uses_hottest()
    test_thermostat_single_session_85c_is_ok()

    # RegimeClassifier
    test_regime_classification()
    test_regime_uses_m21_load_over_standard()

    elapsed = time.perf_counter() - t0
    print(f"=== TODOS OS TESTES PASSARAM ({elapsed:.2f}s) ===")
