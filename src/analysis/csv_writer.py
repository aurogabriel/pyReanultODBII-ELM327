"""CSV writer para dados brutos OBD2 em tempo real.

Salva um arquivo CSV por sessão com todos os PIDs coletados, valores
computados, dados Mode 21 Renault (quando disponível) e metadados da sessão.

Colunas salvas (em ordem):
  - Temporal: timestamp_unix, elapsed_s
  - Dinâmica: speed_kmh, rpm, accel_kmh_s (calculada)
  - Motor: map_kpa, iat_c, coolant_c, engine_load_pct, throttle_pct, timing_adv_deg
  - Combustível OBD: stft_pct, ltft_pct, o2_b1s1_V, o2_b1s2_V, bat_V
  - Consumo calculado: fuel_flow_g_s, instant_L100, cum_distance_km, cum_fuel_L
  - Mode 21 Renault: m21_rpm, m21_coolant_c, m21_inj_time_ms, m21_timing_deg,
                     m21_map_kpa, m21_ltft_pct, m21_lambda_mV, m21_load_pct,
                     m21_inj_duty_pct, m21_knock_count

Uso em análise externa (Python/pandas):
  import pandas as pd
  df = pd.read_csv("analise_raw_....csv", comment="#")
  # Filtra trechos em movimento
  moving = df[df.speed_kmh > 5].copy()
  # Calcula consumo médio real
  total_L100 = moving.fuel_flow_g_s.sum() / moving.speed_kmh.sum() * 36000
"""

import csv
import time
from pathlib import Path
from typing import Optional, TextIO

from .fuel_analyzer import FuelSample

# Ordem das colunas no CSV
CSV_HEADERS = [
    # Temporal
    "timestamp_unix",
    "elapsed_s",
    # Dinâmica do veículo
    "speed_kmh",
    "rpm",
    "accel_kmh_s",
    # Estado do motor
    "map_kpa",
    "iat_c",
    "coolant_c",
    "engine_load_pct",
    "throttle_pct",
    "timing_adv_deg",
    # Fuel system OBD2
    "stft_pct",
    "ltft_pct",
    "o2_b1s1_V",
    "o2_b1s2_V",
    "bat_V",
    # Consumo computado (Speed Density)
    "fuel_flow_g_s",
    "instant_L100",
    "cum_distance_km",
    "cum_fuel_L",
    # Mode 21 Renault proprietário (snapshot + atualizações)
    "m21_rpm",
    "m21_coolant_c",
    "m21_inj_time_ms",
    "m21_timing_deg",
    "m21_map_kpa",
    "m21_ltft_pct",
    "m21_lambda_mV",
    "m21_load_pct",
    "m21_inj_duty_pct",
    "m21_knock_count",
]


def _f(v, decimals: int = 2) -> str:
    """Formata valor para CSV — string vazia se None."""
    if v is None:
        return ""
    return f"{v:.{decimals}f}"


class CsvSessionWriter:
    """Escreve dados brutos de uma sessão de análise em CSV.

    Flush após cada linha → dados não são perdidos em crash.
    Linhas iniciadas com # são comentários ignorados por pandas.
    """

    def __init__(self, path: str, session_info: dict):
        self._path = path
        self._start_time = time.time()
        self._prev_speed: Optional[float] = None
        self._prev_ts: Optional[float] = None
        self._f: Optional[TextIO] = None
        self._writer = None
        # Dados Mode 21 mais recentes (atualizados por snapshot)
        self._m21: dict = {}
        self._open(session_info)

    # ------------------------------------------------------------------

    def _open(self, info: dict) -> None:
        self._f = open(self._path, "w", newline="", encoding="utf-8")
        # Metadados da sessão como comentários (ignorados por pd.read_csv)
        meta_lines = [
            "# OBD Logan Scanner — Dados Brutos de Sessão",
            f"# Motor: {info.get('engine', 'N/A')}",
            f"# Combustível: {info.get('fuel', 'N/A')}",
            f"# Início: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"# Protocolo OBD: {info.get('protocol', 'N/A')}",
            f"# VIN: {info.get('vin', 'N/A')}",
            f"# Arquivo: {Path(self._path).name}",
            "#",
            "# Fórmula consumo (pandas):",
            "# df[df.speed_kmh>5].fuel_flow_g_s.sum()/df[df.speed_kmh>5].speed_kmh.sum()*36000",
        ]
        for line in meta_lines:
            self._f.write(line + "\n")

        self._writer = csv.DictWriter(
            self._f, fieldnames=CSV_HEADERS, extrasaction="ignore"
        )
        self._writer.writeheader()
        self._f.flush()

    # ------------------------------------------------------------------

    def update_m21(self, m21_data: dict) -> None:
        """Atualiza snapshot Mode 21. Chamado no início e em snapshots periódicos."""
        self._m21.update(m21_data)

    # ------------------------------------------------------------------

    def write(self, sample: FuelSample, cum_dist: float, cum_fuel: float) -> None:
        """Escreve uma linha. Seguro chamar de thread separada."""
        if not self._writer or not self._f:
            return

        # Aceleração (Δv/Δt)
        accel = None
        now = sample.timestamp
        if self._prev_speed is not None and self._prev_ts is not None:
            dt = now - self._prev_ts
            if dt > 0.1:
                accel = ((sample.speed_kmh or 0) - self._prev_speed) / dt
        if sample.speed_kmh is not None:
            self._prev_speed = sample.speed_kmh
        self._prev_ts = now

        m = self._m21  # alias curto
        row = {
            # Temporal
            "timestamp_unix": f"{now:.3f}",
            "elapsed_s": _f(now - self._start_time, 1),
            # Dinâmica
            "speed_kmh":   _f(sample.speed_kmh),
            "rpm":         _f(sample.rpm, 0),
            "accel_kmh_s": _f(accel, 2),
            # Motor
            "map_kpa":         _f(sample.map_kpa),
            "iat_c":           _f(sample.iat_c),
            "coolant_c":       _f(sample.coolant_c),
            "engine_load_pct": _f(sample.engine_load),
            "throttle_pct":    _f(sample.throttle),
            "timing_adv_deg":  _f(sample.timing_adv),
            # Fuel system
            "stft_pct":   _f(sample.stft, 2),
            "ltft_pct":   _f(sample.ltft, 2),
            "o2_b1s1_V":  _f(sample.o2_b1s1, 3),
            "o2_b1s2_V":  _f(getattr(sample, "o2_b1s2", None), 3),
            "bat_V":      _f(sample.bat_voltage, 2),
            # Consumo
            "fuel_flow_g_s": _f(sample.fuel_flow_g_s, 5),
            "instant_L100":  _f(sample.instant_L100, 2),
            "cum_distance_km": f"{cum_dist:.5f}",
            "cum_fuel_L":      f"{cum_fuel:.6f}",
            # Mode 21
            "m21_rpm":          _f(m.get("rpm"), 0),
            "m21_coolant_c":    _f(m.get("coolant_c")),
            "m21_inj_time_ms":  _f(m.get("inj_time_ms"), 3),
            "m21_timing_deg":   _f(m.get("timing_deg")),
            "m21_map_kpa":      _f(m.get("map_kpa")),
            "m21_ltft_pct":     _f(m.get("ltft_pct"), 2),
            "m21_lambda_mV":    _f(m.get("lambda_mV"), 0),
            "m21_load_pct":     _f(m.get("load_pct")),
            "m21_inj_duty_pct": _f(m.get("inj_duty_pct")),
            "m21_knock_count":  _f(m.get("knock_count"), 0),
        }

        self._writer.writerow(row)
        self._f.flush()  # sem buffering — dados sempre no disco

    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._f:
            try:
                self._f.flush()
                self._f.close()
            except Exception:
                pass
            self._f = None
            self._writer = None

    @property
    def path(self) -> str:
        return self._path
