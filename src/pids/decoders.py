"""Funções puras de decodificação. SRP por função.
Cada decoder recebe bytes hex limpos (após 41XX) e retorna float ou None.

Convenção de entrada: string hex já SEM o cabeçalho de resposta (41 XX).
Ex.: para 010C cuja resposta é '410C0C5E', o decoder recebe '0C5E'.
"""

from typing import Optional


def _hex_byte(s: str, idx: int) -> Optional[int]:
    pos = idx * 2
    if pos + 2 > len(s):
        return None
    try:
        return int(s[pos:pos + 2], 16)
    except ValueError:
        return None


def raw_a(payload: str) -> Optional[float]:
    a = _hex_byte(payload, 0)
    return float(a) if a is not None else None


def raw_ab(payload: str) -> Optional[float]:
    a, b = _hex_byte(payload, 0), _hex_byte(payload, 1)
    if a is None or b is None:
        return None
    return float((a << 8) | b)


def percent_a(payload: str) -> Optional[float]:
    a = _hex_byte(payload, 0)
    return (a * 100.0 / 255.0) if a is not None else None


def temp_a_minus_40(payload: str) -> Optional[float]:
    a = _hex_byte(payload, 0)
    return float(a - 40) if a is not None else None


def rpm(payload: str) -> Optional[float]:
    a, b = _hex_byte(payload, 0), _hex_byte(payload, 1)
    if a is None or b is None:
        return None
    return ((a << 8) | b) / 4.0


def fuel_trim(payload: str) -> Optional[float]:
    """STFT/LTFT: -100% a +99.2%, fórmula (A * 100/128) - 100"""
    a = _hex_byte(payload, 0)
    return (a * 100.0 / 128.0) - 100.0 if a is not None else None


def timing_advance(payload: str) -> Optional[float]:
    """Avanço ignição: (A/2) - 64, em graus antes PMS"""
    a = _hex_byte(payload, 0)
    return (a / 2.0) - 64.0 if a is not None else None


def maf(payload: str) -> Optional[float]:
    """MAF: ((A*256) + B) / 100, em g/s"""
    a, b = _hex_byte(payload, 0), _hex_byte(payload, 1)
    if a is None or b is None:
        return None
    return ((a << 8) | b) / 100.0


def o2_narrow(payload: str) -> Optional[float]:
    """Sonda lambda narrow-band: A * 0.005 V (0-1.275V típico)"""
    a = _hex_byte(payload, 0)
    return a * 0.005 if a is not None else None


def voltage_ab(payload: str) -> Optional[float]:
    """Tensão módulo: ((A*256)+B) / 1000, em V"""
    a, b = _hex_byte(payload, 0), _hex_byte(payload, 1)
    if a is None or b is None:
        return None
    return ((a << 8) | b) / 1000.0


def abs_load(payload: str) -> Optional[float]:
    """Carga absoluta: ((A*256)+B) * 100/255, em %"""
    a, b = _hex_byte(payload, 0), _hex_byte(payload, 1)
    if a is None or b is None:
        return None
    return ((a << 8) | b) * 100.0 / 255.0


def monitor_status(payload: str) -> Optional[float]:
    """Retorna nº de DTCs armazenados (bits 0-6 do byte A, bit 7 = MIL)."""
    a = _hex_byte(payload, 0)
    if a is None:
        return None
    return float(a & 0x7F)


def fuel_system_status(payload: str) -> Optional[float]:
    """Retorna código bruto do byte A (1=OL, 2=CL, etc)."""
    return raw_a(payload)


# Registro nome -> função (usado pelo loader YAML)
DECODER_MAP = {
    "raw_a": raw_a,
    "raw_ab": raw_ab,
    "percent_a": percent_a,
    "temp_a_minus_40": temp_a_minus_40,
    "rpm": rpm,
    "fuel_trim": fuel_trim,
    "timing_advance": timing_advance,
    "maf": maf,
    "o2_narrow": o2_narrow,
    "voltage_ab": voltage_ab,
    "abs_load": abs_load,
    "monitor_status": monitor_status,
    "fuel_system_status": fuel_system_status,
}
