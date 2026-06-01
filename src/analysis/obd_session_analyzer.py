"""Analisador offline de sessões OBD2 — Renault Logan 1.0 D4F 16v.

Corrige três bugs do analisador original (fuel_analyzer.py):
  Bug 1 — Malha fechada agora lida diretamente do PID 0103, não inferida
           por temperatura. Cold start em OL é normal — só sinaliza OL
           anômalo quando motor quente (>70°C por >=60 s).
  Bug 2 — Timing/knock separado por regime (IDLE/CRUISE/LOAD). Timing
           negativo em idle é comportamento normal do D4F; knock retard
           real só é reportado quando LOAD tiver >=10 % das amostras
           abaixo do threshold. Confiança LOW sem m21_knock_count.
  Bug 3 — Diagnóstico de termostato com critérios robustos (duração,
           carga acumulada, slope de temperatura) e suporte multi-sessão:
           usa a sessão de maior temperatura como referência.

Regra geral: se dado Mode 21 (m21_*) estiver disponível, é preferido ao
PID OBD2 padrão equivalente (Renault proprietário > SAE J1979).
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Optional


# ─── Constantes do motor ─────────────────────────────────────────────────────

D4F_SPECS: dict = {
    "thermostat_opening_temp_c": 89,
    "closed_loop_min_temp_c": 65,
    "timing_idle_expected_range": (-40, 5),
    "timing_cruise_expected_range": (5, 30),
    "timing_load_expected_range": (0, 25),
    "knock_retard_max_cruise_deg": -5,
    "knock_retard_max_load_deg": -10,
    "ltft_normal_range_pct": (-5.0, 5.0),
    "ltft_warning_pct": (-8.0, 8.0),
}

# SAE J1979 — decodificação do byte A do PID 0103
FUEL_SYSTEM_STATUS: dict[int, str] = {
    0x01: "Open Loop — condições não satisfeitas",
    0x02: "Closed Loop — sonda lambda ativa",
    0x04: "Open Loop — carga/aceleração",
    0x08: "Open Loop — falha detectada",
    0x10: "Closed Loop — falha em sensor lambda",
}
# Códigos que indicam malha aberta ANÔMALA quando motor quente
_ANOMALOUS_OL: frozenset[int] = frozenset({0x01, 0x08})


# ─── Estruturas de dados ─────────────────────────────────────────────────────

@dataclass
class Row:
    """Uma linha do CSV bruto (raw_*.csv) já parseada."""
    timestamp: float = 0.0
    elapsed_s: float = 0.0
    speed_kmh:       Optional[float] = None
    rpm:             Optional[float] = None
    accel_kmh_s:     Optional[float] = None
    map_kpa:         Optional[float] = None
    iat_c:           Optional[float] = None
    coolant_c:       Optional[float] = None
    engine_load_pct: Optional[float] = None
    throttle_pct:    Optional[float] = None
    timing_adv_deg:  Optional[float] = None
    stft_pct:        Optional[float] = None
    ltft_pct:        Optional[float] = None
    o2_b1s1_V:       Optional[float] = None
    o2_b1s2_V:       Optional[float] = None
    bat_V:           Optional[float] = None
    fuel_flow_g_s:   Optional[float] = None
    instant_L100:    Optional[float] = None
    cum_distance_km: Optional[float] = None
    cum_fuel_L:      Optional[float] = None
    # Mode 21 Renault (KWP2000 proprietário — preferido quando disponível)
    m21_rpm:          Optional[float] = None
    m21_coolant_c:    Optional[float] = None
    m21_inj_time_ms:  Optional[float] = None
    m21_timing_deg:   Optional[float] = None
    m21_map_kpa:      Optional[float] = None
    m21_ltft_pct:     Optional[float] = None
    m21_lambda_mV:    Optional[float] = None
    m21_load_pct:     Optional[float] = None
    m21_inj_duty_pct: Optional[float] = None
    m21_knock_count:  Optional[float] = None
    # Preenchido pelo RegimeClassifier
    regime: str = ""


@dataclass
class PidRow:
    """Uma linha do CSV de sessão (sess_*.csv)."""
    pid:               str = ""
    name:              str = ""
    raw_response:      str = ""
    parsed_value:      Optional[float] = None
    unit:              str = ""
    timestamp:         float = 0.0
    transport_delay_ms: float = 0.0
    status:            str = ""


@dataclass
class DiagnosticResult:
    """Diagnóstico com evidência obrigatória e nível de confiança."""
    code:       str
    severity:   str        # "CRITICO" | "AVISO" | "INFO" | "DADO_INSUFICIENTE"
    confidence: str        # "HIGH" | "MEDIUM" | "LOW"
    title:      str
    description: str
    evidence:   str        # variável/PID, n amostras, valor vs threshold, regime
    pid_used:   str
    n_samples:  int
    regime:     str = ""
    causes:     list[str] = field(default_factory=list)
    actions:    list[str] = field(default_factory=list)


# ─── Utilitários matemáticos ─────────────────────────────────────────────────

def _best(row: Row, m21_attr: str, std_attr: str) -> Optional[float]:
    """Prefere dado Mode 21 Renault quando disponível."""
    v = getattr(row, m21_attr, None)
    return v if v is not None else getattr(row, std_attr, None)


def _pct(values: list[float], p: float) -> float:
    """Percentil p (0–100) por interpolação linear."""
    if not values:
        raise ValueError("lista vazia")
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (k - lo) * (s[hi] - s[lo])


def _slope_c_per_min(elapsed_s: list[float], temps_c: list[float]) -> float:
    """Inclinação linear de temperatura em °C/min (regressão mínimos quadrados)."""
    n = len(elapsed_s)
    if n < 2:
        return 0.0
    mx = statistics.mean(elapsed_s)
    my = statistics.mean(temps_c)
    num = sum((t - mx) * (v - my) for t, v in zip(elapsed_s, temps_c))
    den = sum((t - mx) ** 2 for t in elapsed_s)
    return (num / den) * 60.0 if den else 0.0  # s → min


def _confidence(n: int, has_direct_pid: bool) -> str:
    if not has_direct_pid or n < 10:
        return "LOW"
    return "MEDIUM" if n < 50 else "HIGH"


def _cap_severity(severity: str, confidence: str) -> str:
    """CRÍTICO com LOW confidence → AVISO (não emitir falso crítico)."""
    return "AVISO" if severity == "CRITICO" and confidence == "LOW" else severity


# ─── SessionLoader ───────────────────────────────────────────────────────────

class SessionLoader:
    """SRP: carrega e valida CSVs brutos e de sessão, tolerante a células vazias.

    Aceita path (str | Path) ou io.StringIO para facilitar testes sem disco.
    Linhas iniciando com '#' são tratadas como comentários e ignoradas.
    """

    _RAW_COLS = [
        "timestamp_unix", "elapsed_s", "speed_kmh", "rpm", "accel_kmh_s",
        "map_kpa", "iat_c", "coolant_c", "engine_load_pct", "throttle_pct",
        "timing_adv_deg", "stft_pct", "ltft_pct", "o2_b1s1_V", "o2_b1s2_V",
        "bat_V", "fuel_flow_g_s", "instant_L100", "cum_distance_km", "cum_fuel_L",
        "m21_rpm", "m21_coolant_c", "m21_inj_time_ms", "m21_timing_deg",
        "m21_map_kpa", "m21_ltft_pct", "m21_lambda_mV", "m21_load_pct",
        "m21_inj_duty_pct", "m21_knock_count",
    ]

    def load_raw_csv(self, source: str | Path | io.StringIO) -> list[Row]:
        rows: list[Row] = []
        for rec in self._iter_csv(source):
            f = self._to_floats(rec, self._RAW_COLS)
            rows.append(Row(
                timestamp=f.get("timestamp_unix") or 0.0,
                elapsed_s=f.get("elapsed_s") or 0.0,
                speed_kmh=f.get("speed_kmh"),
                rpm=f.get("rpm"),
                accel_kmh_s=f.get("accel_kmh_s"),
                map_kpa=f.get("map_kpa"),
                iat_c=f.get("iat_c"),
                coolant_c=f.get("coolant_c"),
                engine_load_pct=f.get("engine_load_pct"),
                throttle_pct=f.get("throttle_pct"),
                timing_adv_deg=f.get("timing_adv_deg"),
                stft_pct=f.get("stft_pct"),
                ltft_pct=f.get("ltft_pct"),
                o2_b1s1_V=f.get("o2_b1s1_V"),
                o2_b1s2_V=f.get("o2_b1s2_V"),
                bat_V=f.get("bat_V"),
                fuel_flow_g_s=f.get("fuel_flow_g_s"),
                instant_L100=f.get("instant_L100"),
                cum_distance_km=f.get("cum_distance_km"),
                cum_fuel_L=f.get("cum_fuel_L"),
                m21_rpm=f.get("m21_rpm"),
                m21_coolant_c=f.get("m21_coolant_c"),
                m21_inj_time_ms=f.get("m21_inj_time_ms"),
                m21_timing_deg=f.get("m21_timing_deg"),
                m21_map_kpa=f.get("m21_map_kpa"),
                m21_ltft_pct=f.get("m21_ltft_pct"),
                m21_lambda_mV=f.get("m21_lambda_mV"),
                m21_load_pct=f.get("m21_load_pct"),
                m21_inj_duty_pct=f.get("m21_inj_duty_pct"),
                m21_knock_count=f.get("m21_knock_count"),
            ))
        return rows

    def load_session_csv(self, source: str | Path | io.StringIO) -> list[PidRow]:
        rows: list[PidRow] = []
        for rec in self._iter_csv(source):
            pv = None
            try:
                raw_pv = rec.get("parsed_value", "")
                pv = float(raw_pv) if raw_pv else None
            except (ValueError, TypeError):
                pass
            rows.append(PidRow(
                pid=rec.get("pid", "").strip().upper(),
                name=rec.get("name", "").strip(),
                raw_response=rec.get("raw_response", "").strip(),
                parsed_value=pv,
                unit=rec.get("unit", "").strip(),
                timestamp=float(rec.get("timestamp", 0) or 0),
                transport_delay_ms=float(rec.get("transport_delay_ms", 0) or 0),
                status=rec.get("status", "").strip().upper(),
            ))
        return rows

    @staticmethod
    def _iter_csv(source: str | Path | io.StringIO) -> Iterator[dict]:
        owned = not isinstance(source, io.StringIO)
        fh = open(source, newline="", encoding="utf-8") if owned else source
        try:
            non_comment = (ln for ln in fh if not ln.lstrip().startswith("#"))
            for row in csv.DictReader(non_comment):
                yield {k.strip(): (v.strip() if v else "") for k, v in row.items()}
        finally:
            if owned:
                fh.close()

    @staticmethod
    def _to_floats(rec: dict, cols: list[str]) -> dict[str, Optional[float]]:
        out: dict[str, Optional[float]] = {}
        for c in cols:
            v = rec.get(c, "")
            try:
                out[c] = float(v) if v else None
            except (ValueError, TypeError):
                out[c] = None
        return out


# ─── RegimeClassifier ────────────────────────────────────────────────────────

class RegimeClassifier:
    """SRP: classifica cada amostra em IDLE / CRUISE / LOAD / TRANSITION.

    Usa load do Mode 21 se disponível (m21_load_pct > engine_load_pct).
    """

    # Limiares conforme especificação do prompt
    _IDLE_MAX_SPEED   = 5.0     # km/h
    _IDLE_MAX_RPM     = 1100.0
    _CRUISE_MIN_SPEED = 15.0    # km/h
    _CRUISE_MAX_LOAD  = 60.0    # %
    _CRUISE_MIN_RPM   = 1000.0
    _CRUISE_MAX_RPM   = 3000.0
    _LOAD_MIN_LOAD    = 60.0    # %
    _LOAD_MIN_ACCEL   = 1.5     # km/h/s

    def classify(self, rows: list[Row]) -> list[Row]:
        from dataclasses import replace
        return [replace(r, regime=self._regime(r)) for r in rows]

    def _regime(self, r: Row) -> str:
        speed = r.speed_kmh or 0.0
        rpm   = r.rpm or 0.0
        load  = _best(r, "m21_load_pct", "engine_load_pct") or 0.0
        accel = r.accel_kmh_s or 0.0

        if load >= self._LOAD_MIN_LOAD or accel >= self._LOAD_MIN_ACCEL:
            return "LOAD"
        if speed < self._IDLE_MAX_SPEED and rpm < self._IDLE_MAX_RPM:
            return "IDLE"
        if (speed >= self._CRUISE_MIN_SPEED
                and load < self._CRUISE_MAX_LOAD
                and self._CRUISE_MIN_RPM <= rpm <= self._CRUISE_MAX_RPM):
            return "CRUISE"
        return "TRANSITION"


# ─── FuelSystemAnalyzer ──────────────────────────────────────────────────────

class FuelSystemAnalyzer:
    """SRP / Bug 1: Open/Closed Loop via PID 0103, não por temperatura.

    Só reporta OL anômalo se o motor estiver quente (coolant > 70 °C por
    >=60 s contínuos). Cold start em OL é esperado e não é reportado.
    """

    _PID               = "0103"
    _HOT_THRESHOLD_C   = 70.0
    _HOT_MIN_DURATION  = 60.0   # segundos contínuos

    def analyze(
        self,
        pid_rows: list[PidRow],
        raw_rows: list[Row],
    ) -> DiagnosticResult:
        candidates = [p for p in pid_rows
                      if p.pid == self._PID and p.status == "SUCCESS"]

        if not candidates:
            return self._no_data(0)

        decoded = [(p.timestamp, self._decode(p.raw_response)) for p in candidates]
        decoded = [(ts, ba) for ts, ba in decoded if ba is not None]

        if not decoded:
            return self._no_data(len(candidates))

        # Período quente: [hot_start_ts, hot_end_ts]
        hot_s, hot_start, hot_end = self._hot_period(raw_rows)
        engine_warm = hot_s >= self._HOT_MIN_DURATION

        if not engine_warm:
            conf = _confidence(len(decoded), True)
            return DiagnosticResult(
                code="FUEL_LOOP_COLD_ONLY",
                severity="INFO",
                confidence=conf,
                title="Malha aberta durante cold start (normal)",
                description=(
                    f"Motor não atingiu {self._HOT_THRESHOLD_C:.0f}°C por "
                    f"{self._HOT_MIN_DURATION:.0f} s. Open Loop em cold start é esperado."
                ),
                evidence=(
                    f"PID {self._PID} | n={len(decoded)} | "
                    f"período quente={hot_s:.0f} s"
                ),
                pid_used=self._PID,
                n_samples=len(decoded),
            )

        # Filtro: só leituras durante período quente
        warm_decoded = [(ts, ba) for ts, ba in decoded
                        if hot_start <= ts <= hot_end]
        anomalous = [(ts, ba) for ts, ba in warm_decoded
                     if ba in _ANOMALOUS_OL]

        n_total    = len(decoded)
        n_warm     = len(warm_decoded)
        n_anomalous = len(anomalous)
        conf = _confidence(n_total, True)

        if n_anomalous == 0:
            # Descreve estado dominante
            counts: dict[int, int] = {}
            for _, ba in (warm_decoded or decoded):
                counts[ba] = counts.get(ba, 0) + 1
            dom = max(counts, key=lambda k: counts[k]) if counts else None
            dom_label = (FUEL_SYSTEM_STATUS.get(dom, f"0x{dom:02X}")
                         if dom is not None else "desconhecido")
            return DiagnosticResult(
                code="FUEL_LOOP_OK",
                severity="INFO",
                confidence=conf,
                title="Malha de combustível normal",
                description=f"Motor quente operando em: {dom_label}.",
                evidence=(
                    f"PID {self._PID} | n_total={n_total} | "
                    f"n_quente={n_warm} | anômalos=0"
                ),
                pid_used=self._PID,
                n_samples=n_total,
            )

        pct = n_anomalous / max(n_warm, 1) * 100
        states_str = ", ".join(
            FUEL_SYSTEM_STATUS.get(ba, f"0x{ba:02X}")
            for _, ba in anomalous[:3]
        )
        severity = "CRITICO" if pct > 20 else "AVISO"
        severity = _cap_severity(severity, conf)

        return DiagnosticResult(
            code="FUEL_LOOP_ANOMALOUS",
            severity=severity,
            confidence=conf,
            title="Malha aberta anômala com motor quente",
            description=(
                f"PID 0103 indicou Open Loop anômalo em {pct:.0f}% das leituras "
                f"com motor acima de {self._HOT_THRESHOLD_C:.0f}°C. "
                f"ECU usa mapa fixo sem correção lambda → consumo aumentado."
            ),
            evidence=(
                f"PID {self._PID} | n={n_total} | "
                f"anômalos={n_anomalous}/{n_warm} ({pct:.0f}%) | "
                f"estados: {states_str}"
            ),
            pid_used=self._PID,
            n_samples=n_total,
            causes=[
                "Sonda lambda com defeito (tensão travada ou resposta lenta)",
                "Falha no circuito da sonda (fiação, conector oxidado)",
                "Sensor de temperatura (ECT) lendo incorreto",
            ],
            actions=[
                "Verificar oscilação da sonda O2 em scan ao vivo (0.1 V–0.9 V)",
                "Checar DTCs P0130–P0167 (circuito O2)",
                "Confirmar tensão aquecedor da sonda (~12 V)",
            ],
        )

    @staticmethod
    def _decode(raw: str) -> Optional[int]:
        """Extrai byte A da resposta do PID 0103 (ex: '410302' → 0x02)."""
        cleaned = raw.replace(" ", "").upper()
        if cleaned.startswith("4103") and len(cleaned) >= 6:
            try:
                return int(cleaned[4:6], 16)
            except ValueError:
                pass
        return None

    def _hot_period(
        self, rows: list[Row]
    ) -> tuple[float, float, float]:
        """Retorna (duração_s, ts_início, ts_fim) do maior período contínuo quente."""
        best_dur = best_start = best_end = 0.0
        cur_start = cur_start_ts = None

        for r in rows:
            temp = _best(r, "m21_coolant_c", "coolant_c")
            if temp is not None and temp > self._HOT_THRESHOLD_C:
                if cur_start is None:
                    cur_start, cur_start_ts = r.elapsed_s, r.timestamp
            else:
                if cur_start is not None:
                    dur = r.elapsed_s - cur_start
                    if dur > best_dur:
                        best_dur, best_start, best_end = dur, cur_start_ts, r.timestamp
                    cur_start = cur_start_ts = None

        if cur_start is not None and rows:
            dur = rows[-1].elapsed_s - cur_start
            if dur > best_dur:
                best_dur, best_start, best_end = dur, cur_start_ts, rows[-1].timestamp

        return best_dur, best_start, best_end

    @staticmethod
    def _no_data(n: int) -> DiagnosticResult:
        return DiagnosticResult(
            code="FUEL_LOOP_NO_DATA",
            severity="DADO_INSUFICIENTE",
            confidence="LOW",
            title="Estado de malha combustível desconhecido",
            description="PID 0103 não disponível ou sem leituras SUCCESS no CSV de sessão.",
            evidence=f"PID 0103 | n={n}",
            pid_used="0103",
            n_samples=n,
        )


# ─── TimingAnalyzer ──────────────────────────────────────────────────────────

class TimingAnalyzer:
    """SRP / Bug 2: Análise de timing por regime com percentis, sem média global.

    Prioriza m21_timing_deg (Mode 21) sobre PID 010E quando disponível.
    Knock só é reportado como CRITICO se LOAD tiver >=10% das amostras
    abaixo do threshold E m21_knock_count confirmar (se disponível).
    Sem m21_knock_count → confidence LOW para diagnósticos de knock.
    """

    # Thresholds por regime: abaixo desses valores é anomalia
    _THRESHOLDS: dict[str, dict[str, float]] = {
        "IDLE":   {"aviso": -35.0, "critico": -45.0},
        "CRUISE": {"aviso":   5.0, "critico":  -5.0},
        "LOAD":   {"aviso":   0.0, "critico": -10.0},
    }
    # Percentual mínimo de amostras abaixo do threshold para disparar diagnóstico
    _MIN_PCT_BELOW = 10.0  # %

    def analyze(self, rows: list[Row]) -> list[DiagnosticResult]:
        results = []
        for regime in ("IDLE", "CRUISE", "LOAD"):
            r = self._analyze_regime(rows, regime)
            if r is not None:
                results.append(r)
        return results

    def _analyze_regime(self, rows: list[Row], regime: str) -> Optional[DiagnosticResult]:
        subset = [r for r in rows if r.regime == regime]
        if not subset:
            return None

        timing_vals: list[float] = []
        has_m21 = False
        for r in subset:
            v = _best(r, "m21_timing_deg", "timing_adv_deg")
            if v is not None:
                timing_vals.append(v)
                if r.m21_timing_deg is not None:
                    has_m21 = True

        if not timing_vals:
            return None

        n     = len(timing_vals)
        conf  = _confidence(n, True)
        p10   = _pct(timing_vals, 10)
        p50   = _pct(timing_vals, 50)
        p90   = _pct(timing_vals, 90)
        mean  = statistics.mean(timing_vals)
        pid   = "Mode21:timing" if has_m21 else "010E"

        th_aviso   = self._THRESHOLDS[regime]["aviso"]
        th_critico = self._THRESHOLDS[regime]["critico"]

        pct_below_aviso   = sum(1 for v in timing_vals if v < th_aviso)   / n * 100
        pct_below_critico = sum(1 for v in timing_vals if v < th_critico) / n * 100

        evidence = (
            f"Regime={regime} | PID={pid} | n={n} | "
            f"P10={p10:.1f}° P50={p50:.1f}° P90={p90:.1f}° média={mean:.1f}° | "
            f"<{th_aviso:.0f}°: {pct_below_aviso:.0f}% | "
            f"<{th_critico:.0f}°: {pct_below_critico:.0f}%"
        )

        # ── LOAD: único regime onde reportamos knock ───────────────────────
        if regime == "LOAD" and pct_below_aviso >= self._MIN_PCT_BELOW:
            knock_vals = [
                r.m21_knock_count for r in subset
                if r.m21_knock_count is not None
            ]
            has_knock = len(knock_vals) > 0
            # Sem contador de knock do Mode 21 → confiança LOW
            if not has_knock:
                conf = "LOW"

            if pct_below_critico >= self._MIN_PCT_BELOW:
                severity = _cap_severity("CRITICO", conf)
                code     = "TIMING_KNOCK_LOAD_CRITICAL"
                title    = "Retardo de ignição crítico sob carga — possível knock"
                knock_note = ""
                if has_knock:
                    avg_k = statistics.mean(knock_vals)
                    knock_note = f" m21_knock_count médio={avg_k:.1f}."
                desc = (
                    f"{pct_below_critico:.0f}% das amostras sob carga com timing "
                    f"< {th_critico:.0f}° — ECU está retardando por detonação.{knock_note}"
                )
            else:
                severity = _cap_severity("AVISO", conf)
                code     = "TIMING_RETARD_LOAD"
                title    = "Retardo de ignição sob carga"
                desc     = (
                    f"{pct_below_aviso:.0f}% das amostras sob carga com timing "
                    f"< {th_aviso:.0f}° (ideal >{th_aviso:.0f}°). P50={p50:.1f}°."
                )

            if conf == "LOW":
                desc += (" (confiança LOW — m21_knock_count indisponível;"
                         " diagnóstico de knock inconclusivo)")

            return DiagnosticResult(
                code=code, severity=severity, confidence=conf,
                title=title, description=desc, evidence=evidence,
                pid_used=pid, n_samples=n, regime=regime,
                causes=[
                    "Combustível de baixa octanagem",
                    "Sensor de detonação (knock sensor) com defeito",
                    "Velas de ignição gastas ou com folga incorreta",
                    "Acúmulo de carbono nas câmaras de combustão",
                ],
                actions=[
                    "Usar combustível de melhor qualidade (92+ octanas)",
                    "Verificar/substituir velas (D4F: cada 30.000 km)",
                    "Checar sensor de detonação e fiação",
                ],
            )

        # ── CRUISE: timing muito baixo ────────────────────────────────────
        if regime == "CRUISE" and pct_below_aviso >= self._MIN_PCT_BELOW:
            sev = "CRITICO" if pct_below_critico >= self._MIN_PCT_BELOW else "AVISO"
            sev = _cap_severity(sev, conf)
            return DiagnosticResult(
                code="TIMING_LOW_CRUISE", severity=sev, confidence=conf,
                title=f"Timing baixo em cruzeiro (P50={p50:.1f}°)",
                description=(
                    f"{pct_below_aviso:.0f}% das amostras de cruzeiro com timing "
                    f"< {th_aviso:.0f}° (esperado 5–30°). P10={p10:.1f}° P90={p90:.1f}°."
                ),
                evidence=evidence, pid_used=pid, n_samples=n, regime=regime,
                causes=["Combustível de baixa octanagem", "Velas de ignição gastas"],
                actions=["Tentar combustível com mais octanagem", "Verificar velas"],
            )

        # ── IDLE: timing muito negativo (extremo) ─────────────────────────
        if regime == "IDLE" and pct_below_critico >= self._MIN_PCT_BELOW:
            sev = _cap_severity("AVISO", conf)
            return DiagnosticResult(
                code="TIMING_EXTREME_IDLE", severity=sev, confidence=conf,
                title=f"Timing extremamente negativo em marcha lenta (P10={p10:.1f}°)",
                description=(
                    f"Timing negativo em idle é normal no D4F, mas {pct_below_critico:.0f}% "
                    f"das amostras abaixo de {th_critico:.0f}° pode indicar estratégia ECU "
                    f"excessiva de aquecimento do catalisador ou offset de sensor."
                ),
                evidence=evidence, pid_used=pid, n_samples=n, regime=regime,
                causes=[
                    "Estratégia de aquecimento do catalisador (ECU design)",
                    "Possível offset de calibração do sensor de posição da árvore de cames",
                ],
                actions=[
                    "Monitorar — timing negativo em idle é esperado para o D4F",
                    "Comparar com valor Mode 21 (m21_timing_deg) se disponível",
                ],
            )

        # Normal para o regime
        return DiagnosticResult(
            code=f"TIMING_OK_{regime}", severity="INFO", confidence=conf,
            title=f"Timing normal em {regime.lower()} (P50={p50:.1f}°)",
            description=f"P10={p10:.1f}° P50={p50:.1f}° P90={p90:.1f}° (n={n})",
            evidence=evidence, pid_used=pid, n_samples=n, regime=regime,
        )


# ─── ThermalAnalyzer ─────────────────────────────────────────────────────────

class ThermalAnalyzer:
    """SRP / Bug 3: Diagnóstico de termostato com critérios robustos e multi-sessão.

    Multi-sessão: usa a sessão de maior temperatura como referência.
    Se qualquer sessão atingiu >=85 °C → termostato normal.

    Critério de defeito requer TODAS as condições:
      • duração total > 20 min
      • velocidade média > 20 km/h (não predominantemente idle)
      • tempo em LOAD >= 5 min acumulados
      • temperatura máxima < 80 °C
      • slope de temperatura na 2ª metade <= 0.1 °C/min
    """

    _HEALTHY_C       = 85.0    # >= este valor em qualquer sessão → OK
    _FAULT_MAX_C     = 80.0    # abaixo deste com todas as outras condições → FAULT
    _OPENING_C       = D4F_SPECS["thermostat_opening_temp_c"]   # 89 °C
    _MIN_DURATION_S  = 1200    # 20 min
    _MIN_AVG_SPEED   = 20.0    # km/h — abaixo = perfil idle
    _MIN_LOAD_S      = 300     # 5 min em LOAD
    _SLOPE_THRESHOLD = 0.1     # °C/min — abaixo = temperatura estagnada

    def analyze(
        self,
        primary: list[Row],
        others: list[list[Row]] | None = None,
    ) -> DiagnosticResult:
        all_sessions = [primary] + list(others or [])

        # Sessão de referência = aquela com maior temperatura máxima
        def max_temp(rows: list[Row]) -> float:
            vals = [_best(r, "m21_coolant_c", "coolant_c") for r in rows]
            return max((v for v in vals if v is not None), default=0.0)

        ranked = sorted(enumerate(all_sessions), key=lambda x: max_temp(x[1]), reverse=True)
        ref_idx, ref_session = ranked[0]
        ref_max = max_temp(ref_session)
        ref_label = ("sessão atual" if ref_idx == 0
                     else f"sessão adicional #{ref_idx} (de {len(all_sessions)})")

        n_coolant = sum(
            1 for r in ref_session
            if _best(r, "m21_coolant_c", "coolant_c") is not None
        )
        conf = _confidence(n_coolant, True)

        # Qualquer sessão ≥ 85 °C → termostato OK
        if ref_max >= self._HEALTHY_C:
            return DiagnosticResult(
                code="THERMOSTAT_OK", severity="INFO", confidence=conf,
                title=f"Termostato normal — máx {ref_max:.0f}°C ({ref_label})",
                description=(
                    f"Temperatura máxima {ref_max:.0f}°C indica que o termostato "
                    f"(abertura ~{self._OPENING_C}°C) está funcionando corretamente."
                ),
                evidence=(
                    f"temp_max={ref_max:.0f}°C | threshold={self._HEALTHY_C:.0f}°C | "
                    f"ref={ref_label} | n={n_coolant}"
                ),
                pid_used="0105 / m21_coolant_c",
                n_samples=n_coolant,
            )

        stats = self._stats(ref_session)

        # Verificações de dados insuficientes — em ordem de prioridade
        if stats["duration_s"] < self._MIN_DURATION_S:
            return DiagnosticResult(
                code="THERMOSTAT_SHORT_SESSION", severity="DADO_INSUFICIENTE",
                confidence="LOW",
                title="Dados insuficientes — viagem muito curta para avaliar termostato",
                description=(
                    f"Duração {stats['duration_s']/60:.0f} min < mínimo "
                    f"{self._MIN_DURATION_S/60:.0f} min necessário."
                ),
                evidence=(
                    f"duração={stats['duration_s']/60:.0f} min | "
                    f"mínimo={self._MIN_DURATION_S/60:.0f} min | ref={ref_label}"
                ),
                pid_used="0105", n_samples=n_coolant,
            )

        if stats["mean_speed"] < self._MIN_AVG_SPEED:
            return DiagnosticResult(
                code="THERMOSTAT_IDLE_PROFILE", severity="DADO_INSUFICIENTE",
                confidence="LOW",
                title="Dados insuficientes — perfil predominantemente idle",
                description=(
                    f"Velocidade média {stats['mean_speed']:.0f} km/h < "
                    f"{self._MIN_AVG_SPEED:.0f} km/h. Motor sem carga suficiente "
                    f"para acionar o termostato."
                ),
                evidence=(
                    f"vel_média={stats['mean_speed']:.0f} km/h | "
                    f"mínimo={self._MIN_AVG_SPEED:.0f} km/h | ref={ref_label}"
                ),
                pid_used="0105", n_samples=n_coolant,
            )

        if stats["load_s"] < self._MIN_LOAD_S:
            return DiagnosticResult(
                code="THERMOSTAT_LOW_LOAD", severity="DADO_INSUFICIENTE",
                confidence="LOW",
                title="Dados insuficientes — tempo em carga insuficiente",
                description=(
                    f"Motor operou em LOAD por {stats['load_s']/60:.0f} min "
                    f"(mínimo: {self._MIN_LOAD_S/60:.0f} min). "
                    f"Termostato pode não ter sido solicitado."
                ),
                evidence=(
                    f"tempo_LOAD={stats['load_s']/60:.0f} min | "
                    f"mínimo={self._MIN_LOAD_S/60:.0f} min | ref={ref_label}"
                ),
                pid_used="0105", n_samples=n_coolant,
            )

        # Todas as pré-condições satisfeitas — avalia o termostato
        slope   = stats["slope_c_per_min"]
        max_t   = stats["max_temp"]

        if max_t < self._FAULT_MAX_C and slope <= self._SLOPE_THRESHOLD:
            sev = _cap_severity("CRITICO", conf)
            return DiagnosticResult(
                code="THERMOSTAT_FAULT", severity=sev, confidence=conf,
                title="Termostato possivelmente defeituoso (preso aberto)",
                description=(
                    f"Temp máxima {max_t:.0f}°C < {self._FAULT_MAX_C:.0f}°C após "
                    f"{stats['duration_s']/60:.0f} min com {stats['load_s']/60:.0f} min em carga. "
                    f"Slope na 2ª metade: {slope:.2f}°C/min ≤ {self._SLOPE_THRESHOLD}°C/min "
                    f"(temperatura estagnada). Ref: {ref_label}."
                ),
                evidence=(
                    f"temp_max={max_t:.0f}°C | threshold={self._FAULT_MAX_C:.0f}°C | "
                    f"slope={slope:.2f}°C/min | carga={stats['load_s']/60:.0f} min | "
                    f"ref={ref_label} | n={n_coolant}"
                ),
                pid_used="0105 / m21_coolant_c",
                n_samples=n_coolant,
                causes=[
                    "Termostato preso na posição aberta",
                    "Sensor ECT (NTC) com desvio ou circuito aberto",
                ],
                actions=[
                    f"Substituir termostato (abertura nominal: {self._OPENING_C}°C)",
                    "Medir resistência do sensor ECT: ~2,5 kΩ a 20°C / ~300 Ω a 80°C",
                ],
            )

        if max_t < self._FAULT_MAX_C:
            # Temperatura baixa mas ainda subindo — inconclusivo
            sev = _cap_severity("AVISO", conf)
            return DiagnosticResult(
                code="THERMOSTAT_TEMP_RISING", severity=sev, confidence=conf,
                title=f"Temperatura baixa ({max_t:.0f}°C) mas em elevação — monitorar",
                description=(
                    f"Temp máxima {max_t:.0f}°C < {self._FAULT_MAX_C:.0f}°C, mas slope "
                    f"{slope:.2f}°C/min indica ainda estava subindo. "
                    f"Repetir análise com sessão mais longa."
                ),
                evidence=(
                    f"temp_max={max_t:.0f}°C | slope={slope:.2f}°C/min | "
                    f"ref={ref_label} | n={n_coolant}"
                ),
                pid_used="0105 / m21_coolant_c",
                n_samples=n_coolant,
            )

        # Entre 80–85°C sem atingir limiar de saúde
        return DiagnosticResult(
            code="THERMOSTAT_BORDERLINE", severity="AVISO", confidence=conf,
            title=f"Temperatura {max_t:.0f}°C abaixo do ponto de abertura ({self._OPENING_C}°C)",
            description=(
                f"Motor não atingiu temperatura plena. Termostato D4F abre em "
                f"~{self._OPENING_C}°C; temperatura máxima observada: {max_t:.0f}°C."
            ),
            evidence=(
                f"temp_max={max_t:.0f}°C | abertura={self._OPENING_C}°C | "
                f"ref={ref_label} | n={n_coolant}"
            ),
            pid_used="0105 / m21_coolant_c",
            n_samples=n_coolant,
        )

    def _stats(self, rows: list[Row]) -> dict:
        if not rows:
            return {
                "duration_s": 0, "max_temp": 0, "mean_speed": 0,
                "load_s": 0, "slope_c_per_min": 0,
            }

        duration_s = rows[-1].elapsed_s - rows[0].elapsed_s

        temps = [
            (r.elapsed_s, _best(r, "m21_coolant_c", "coolant_c"))
            for r in rows
            if _best(r, "m21_coolant_c", "coolant_c") is not None
        ]
        max_temp = max(t for _, t in temps) if temps else 0.0

        speeds = [r.speed_kmh for r in rows if r.speed_kmh is not None]
        mean_speed = statistics.mean(speeds) if speeds else 0.0

        # Tempo acumulado em LOAD (ignora gaps > 10 s)
        load_rows = [r for r in rows if r.regime == "LOAD"]
        load_s = 0.0
        for i in range(1, len(load_rows)):
            dt = load_rows[i].elapsed_s - load_rows[i - 1].elapsed_s
            if dt < 10:
                load_s += dt

        # Slope na segunda metade
        half = len(temps) // 2
        second = temps[half:]
        slope = (
            _slope_c_per_min([e for e, _ in second], [t for _, t in second])
            if len(second) >= 4 else 0.0
        )

        return {
            "duration_s":     duration_s,
            "max_temp":       max_temp,
            "mean_speed":     mean_speed,
            "load_s":         load_s,
            "slope_c_per_min": slope,
        }


# ─── DiagnosticEngine ────────────────────────────────────────────────────────

class DiagnosticEngine:
    """SRP: coordena os analisadores e retorna lista de diagnósticos ordenada."""

    _SEV_ORDER = {"CRITICO": 0, "AVISO": 1, "INFO": 2, "DADO_INSUFICIENTE": 3}

    def __init__(self) -> None:
        self._loader     = SessionLoader()
        self._classifier = RegimeClassifier()
        self._fuel_sys   = FuelSystemAnalyzer()
        self._timing     = TimingAnalyzer()
        self._thermal    = ThermalAnalyzer()

    def run(
        self,
        raw_csv:       str | Path | io.StringIO,
        session_csv:   str | Path | io.StringIO | None = None,
        extra_raw_csvs: list[str | Path | io.StringIO] | None = None,
    ) -> list[DiagnosticResult]:
        """Executa todos os analisadores e retorna diagnósticos por severidade."""
        raw   = self._classifier.classify(self._loader.load_raw_csv(raw_csv))
        pids  = self._loader.load_session_csv(session_csv) if session_csv else []
        extra = [
            self._classifier.classify(self._loader.load_raw_csv(p))
            for p in (extra_raw_csvs or [])
        ]

        results: list[DiagnosticResult] = []
        results.append(self._fuel_sys.analyze(pids, raw))
        results.extend(self._timing.analyze(raw))
        results.append(self._thermal.analyze(raw, extra or None))

        return sorted(results, key=lambda r: self._SEV_ORDER.get(r.severity, 9))


# ─── ReportFormatter ─────────────────────────────────────────────────────────

class ReportFormatter:
    """SRP: formata lista de DiagnosticResult em texto legível ou JSON."""

    _ICONS = {
        "CRITICO":           "🔴",
        "AVISO":             "🟡",
        "INFO":              "✅",
        "DADO_INSUFICIENTE": "⚪",
    }

    def to_text(self, results: list[DiagnosticResult]) -> str:
        lines = [
            "=" * 66,
            "  DIAGNÓSTICO OBD2 — RENAULT LOGAN D4F 1.0 16v",
            "=" * 66,
            "",
        ]
        for r in results:
            icon = self._ICONS.get(r.severity, "❓")
            lines.append(
                f"{icon} [{r.severity}] [{r.confidence}]  {r.title}"
            )
            lines.append(f"   {r.description}")
            lines.append(f"   Evidência : {r.evidence}")
            lines.append(f"   PID/fonte : {r.pid_used}  |  n={r.n_samples}"
                         + (f"  |  regime={r.regime}" if r.regime else ""))
            if r.causes:
                lines.append("   Causas   :")
                for c in r.causes:
                    lines.append(f"     • {c}")
            if r.actions:
                lines.append("   Ações    :")
                for a in r.actions:
                    lines.append(f"     → {a}")
            lines.append("")
        lines.append("=" * 66)
        return "\n".join(lines)

    def to_json(self, results: list[DiagnosticResult]) -> dict:
        return {
            "diagnostics": [asdict(r) for r in results],
            "summary": {
                "total":        len(results),
                "criticos":     sum(1 for r in results if r.severity == "CRITICO"),
                "avisos":       sum(1 for r in results if r.severity == "AVISO"),
                "insuficiente": sum(1 for r in results if r.severity == "DADO_INSUFICIENTE"),
            },
        }


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="Analisa sessão OBD2 do Logan D4F a partir de CSVs."
    )
    ap.add_argument("raw_csv",         help="raw_*.csv (dados brutos contínuos)")
    ap.add_argument("--session",       help="sess_*.csv (polling por PID)")
    ap.add_argument("--extra",         nargs="*", help="CSVs adicionais para análise multi-sessão")
    ap.add_argument("--json",          action="store_true", help="Saída em JSON")
    args = ap.parse_args()

    engine    = DiagnosticEngine()
    formatter = ReportFormatter()

    results = engine.run(
        raw_csv     = args.raw_csv,
        session_csv = args.session,
        extra_raw_csvs = args.extra,
    )

    if args.json:
        print(json.dumps(formatter.to_json(results), ensure_ascii=False, indent=2))
    else:
        print(formatter.to_text(results))

    has_critical = any(r.severity == "CRITICO" for r in results)
    sys.exit(1 if has_critical else 0)
