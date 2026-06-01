"""Banco de dados ECU Renault Logan 2012.

Cobre os módulos acessíveis via ELM327:
  - UCE Motor (Engine ECU): Sirius 32N / Magneti Marelli — endereço KWP 0x10
  - UCH (Body Control Module) — endereço 0x76
  - ABS — endereço 0x28 (ISO 15765 CAN)

Protocolo usado pelo Logan 2012 1.0L D4F / 1.6L K7M:
  - ISO 14230-4 KWP2000 (fast init) para motor e UCH
  - ISO 15765-4 CAN 11-bit 500kbps para ABS (alguns anos/mercados)

Modo 0x21 = ReadDataByLocalIdentifier (Renault/KWP2000 proprietário).
Modo 0x22 = ReadDataByIdentifier (UDS/ISO 14229) em ECUs mais novas.
Modo 0x18 = ReadDTCByStatus (Renault proprietário).

Uso via ELM327:
  ATSP 5          → KWP2000 fast init, 10400 baud
  AT SH 82 10 F1  → cabeçalho físico: target=0x10 (motor), source=0xF1 (tester)
  10 92           → StartDiagnosticSession (extended)
  21 XX           → ReadDataByLocalIdentifier XX
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Tipos de dado
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenaultParam:
    """Parâmetro Mode 21 de um ECU Renault."""
    local_id: int           # byte enviado após 0x21
    name: str               # chave interna
    description: str
    unit: str
    num_bytes: int          # bytes de dado esperados na resposta
    decoder: Callable[[bytes], Optional[float]]
    priority: int = 2       # 1=alta, 2=média, 3=baixa


@dataclass
class EcuDefinition:
    """Definição de um módulo eletrônico do veículo."""
    name: str               # ex. "UCE Motor"
    short: str              # ex. "engine"
    kwp_address: int        # endereço KWP2000 destino (ex. 0x10)
    can_id: Optional[int]   # 11-bit CAN ID (None se só KWP)
    # Cabeçalho KWP2000 a passar no ATSH (3 bytes: fmt, target, source)
    kwp_header: tuple[int, int, int]
    params: list[RenaultParam] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Decoders Mode 21 (entrada: bytes payload após 61 XX)
# ---------------------------------------------------------------------------

def _u8(b: bytes, i: int = 0) -> Optional[int]:
    return b[i] if len(b) > i else None

def _u16(b: bytes, i: int = 0) -> Optional[int]:
    return (b[i] << 8 | b[i+1]) if len(b) > i + 1 else None


def dec_rpm(b: bytes) -> Optional[float]:
    """RPM = (A*256+B) / 4  — Sirius 32N / K7M"""
    v = _u16(b)
    return v / 4.0 if v is not None else None

def dec_rpm_8(b: bytes) -> Optional[float]:
    """RPM = (A*256+B) / 8  — variante Sirius 34"""
    v = _u16(b)
    return v / 8.0 if v is not None else None

def dec_volt(b: bytes) -> Optional[float]:
    """Tensão = A / 10  (V)"""
    v = _u8(b)
    return v / 10.0 if v is not None else None

def dec_temp(b: bytes) -> Optional[float]:
    """Temperatura = A - 40  (°C)"""
    v = _u8(b)
    return float(v - 40) if v is not None else None

def dec_percent(b: bytes) -> Optional[float]:
    """% = A * 100 / 255"""
    v = _u8(b)
    return v * 100.0 / 255.0 if v is not None else None

def dec_speed(b: bytes) -> Optional[float]:
    """Velocidade = A  (km/h)"""
    v = _u8(b)
    return float(v) if v is not None else None

def dec_advance(b: bytes) -> Optional[float]:
    """Avanço ignição = A / 2 - 64  (graus APMS)"""
    v = _u8(b)
    return v / 2.0 - 64.0 if v is not None else None

def dec_inj_ms(b: bytes) -> Optional[float]:
    """Tempo injeção = (A*256+B) * 0.016  (ms)"""
    v = _u16(b)
    return v * 0.016 if v is not None else None

def dec_lambda_mv(b: bytes) -> Optional[float]:
    """Lambda = A * 5  (mV)"""
    v = _u8(b)
    return float(v * 5) if v is not None else None

def dec_fuel_trim(b: bytes) -> Optional[float]:
    """Fuel trim = A * 100/128 - 100  (%)"""
    v = _u8(b)
    return v * 100.0 / 128.0 - 100.0 if v is not None else None

def dec_map_kpa(b: bytes) -> Optional[float]:
    """MAP = A  (kPa)"""
    v = _u8(b)
    return float(v) if v is not None else None

def dec_maf(b: bytes) -> Optional[float]:
    """MAF = (A*256+B) / 100  (g/s)"""
    v = _u16(b)
    return v / 100.0 if v is not None else None

def dec_raw_u8(b: bytes) -> Optional[float]:
    v = _u8(b)
    return float(v) if v is not None else None

def dec_abs_load(b: bytes) -> Optional[float]:
    """Carga absoluta — retorna valor raw u16 (escala não confirmada no Sirius 32N).
    SAE J1979 PID 0143 usa (A*256+B)*100/255; Renault Mode 21 pode diferir.
    Validar com leitura real antes de assumir unidade '%'."""
    v = _u16(b)
    return float(v) if v is not None else None


def dec_raw_u16(b: bytes) -> Optional[float]:
    v = _u16(b)
    return float(v) if v is not None else None

def dec_idle_rpm(b: bytes) -> Optional[float]:
    """RPM alvo de marcha lenta = A * 8"""
    v = _u8(b)
    return float(v * 8) if v is not None else None


# ---------------------------------------------------------------------------
# Parâmetros Mode 21 — UCE Motor (Sirius 32N / K7M) — Logan 2012
# ---------------------------------------------------------------------------
# Obs: IDs verificados contra DDT4ALL e projetos pyren para D4F/K7M.
# Alguns podem não responder dependendo da variante exata do ECU.

_ENGINE_PARAMS: list[RenaultParam] = [
    RenaultParam(0x01, "engine_rpm",       "Rotação motor",                 "rpm",    2, dec_rpm,      priority=1),
    RenaultParam(0x02, "battery_voltage",  "Tensão bateria",                "V",      1, dec_volt,     priority=2),
    RenaultParam(0x03, "coolant_temp",     "Temperatura líquido arrefec.",  "°C",     1, dec_temp,     priority=2),
    RenaultParam(0x04, "air_temp",         "Temperatura ar admissão",       "°C",     1, dec_temp,     priority=2),
    RenaultParam(0x05, "throttle_pos",     "Posição borboleta",             "%",      1, dec_percent,  priority=1),
    RenaultParam(0x06, "map_pressure",     "Pressão colet. admissão (MAP)", "kPa",    1, dec_map_kpa,  priority=1),
    RenaultParam(0x07, "vehicle_speed",    "Velocidade veículo",            "km/h",   1, dec_speed,    priority=1),
    RenaultParam(0x08, "timing_advance",   "Avanço ignição",                "°",      1, dec_advance,  priority=1),
    RenaultParam(0x09, "injection_time",   "Tempo de injeção",              "ms",     2, dec_inj_ms,   priority=1),
    RenaultParam(0x0A, "lambda_voltage",   "Tensão sonda lambda",           "mV",     1, dec_lambda_mv,priority=1),
    RenaultParam(0x0B, "engine_load",      "Carga motor",                   "%",      1, dec_percent,  priority=2),
    RenaultParam(0x0C, "short_fuel_trim",  "Correção combustível curta",    "%",      1, dec_fuel_trim,priority=2),
    RenaultParam(0x0D, "long_fuel_trim",   "Correção combustível longa",    "%",      1, dec_fuel_trim,priority=2),
    RenaultParam(0x0E, "idle_regulation",  "Regulação marcha lenta (0=ativa)","",     1, dec_raw_u8,   priority=3),
    RenaultParam(0x0F, "air_flow_maf",     "Fluxo de ar (MAF)",             "g/s",    2, dec_maf,      priority=2),
    RenaultParam(0x10, "knock_count",      "Contador de detonação",         "cnt",    1, dec_raw_u8,   priority=2),
    RenaultParam(0x11, "abs_load",         "Carga absoluta motor",          "%",      2, dec_abs_load, priority=3),
    RenaultParam(0x12, "idle_target_rpm",  "RPM alvo marcha lenta",         "rpm",    1, dec_idle_rpm, priority=3),
    RenaultParam(0x13, "fuel_pressure",    "Pressão combustível",           "kPa",    1, dec_map_kpa,  priority=3),
    RenaultParam(0x14, "o2_heater_duty",   "Duty-cycle aquec. sonda",       "%",      1, dec_percent,  priority=3),
    RenaultParam(0x15, "injector_duty",    "Duty-cycle injetor",            "%",      1, dec_percent,  priority=2),
    RenaultParam(0x60, "dtc_count",        "Quantidade de DTCs Renault",    "cnt",    1, dec_raw_u8,   priority=3),
]

# ---------------------------------------------------------------------------
# ECUs do Logan 2012
# ---------------------------------------------------------------------------

ENGINE_ECU = EcuDefinition(
    name="UCE Motor (Sirius 32N / K7M)",
    short="engine",
    kwp_address=0x10,
    can_id=None,
    kwp_header=(0x82, 0x10, 0xF1),  # ATSH 82 10 F1
    params=_ENGINE_PARAMS,
)

UCH_ECU = EcuDefinition(
    name="UCH (Body Control Module)",
    short="uch",
    kwp_address=0x76,
    can_id=None,
    kwp_header=(0x82, 0x76, 0xF1),
    params=[
        RenaultParam(0x02, "battery_voltage_uch", "Tensão bateria (UCH)", "V", 1, dec_volt, priority=3),
        RenaultParam(0x07, "vehicle_speed_uch",    "Velocidade (UCH)",    "km/h", 1, dec_speed, priority=3),
    ],
)

ABS_ECU = EcuDefinition(
    name="ABS / ESP",
    short="abs",
    kwp_address=0x28,
    can_id=0x7A0,
    kwp_header=(0x82, 0x28, 0xF1),
    params=[
        RenaultParam(0x01, "wheel_fl",  "Roda dianteira esq.",  "km/h", 1, dec_speed, priority=2),
        RenaultParam(0x02, "wheel_fr",  "Roda dianteira dir.",  "km/h", 1, dec_speed, priority=2),
        RenaultParam(0x03, "wheel_rl",  "Roda traseira esq.",   "km/h", 1, dec_speed, priority=2),
        RenaultParam(0x04, "wheel_rr",  "Roda traseira dir.",   "km/h", 1, dec_speed, priority=2),
    ],
)

ALL_ECUS: list[EcuDefinition] = [ENGINE_ECU, UCH_ECU, ABS_ECU]


def get_ecu(short_name: str) -> Optional[EcuDefinition]:
    return next((e for e in ALL_ECUS if e.short == short_name), None)
