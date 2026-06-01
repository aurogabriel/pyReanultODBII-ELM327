"""Leitura rápida de parâmetros Mode 21 Renault durante sessão OBD2.

Faz um snapshot dos parâmetros proprietários Renault (injeção, timing,
carga real, knock, etc.) sem interromper o loop OBD2 principal por muito tempo.

Fluxo:
  1. ATSP 0 + AT SH 7E0  → CAN para ECU motor
  2. 10 03               → sessão estendida UDS (obrigatória para Mode 21 via CAN)
  3. 21 XX para cada ID  → lê os parâmetros de interesse
  4. 10 01 + ATSP 0      → volta à sessão default
  5. Reinit ELM327       → garante que o loop OBD2 retome normalmente

O snapshot completo (10 parâmetros) leva ~4-6 segundos.
O snapshot rápido (5 parâmetros) leva ~2-3 segundos.
"""

from __future__ import annotations

import time
from typing import Optional

from ..protocol import Elm327Protocol, ResponseStatus
from ..debug_log import get_logger

_log = get_logger(__name__)

# Parâmetros Mode 21 de interesse para o snapshot
# (local_id, key_name, num_bytes, decoder_fn)
_SNAPSHOT_PARAMS = [
    # Prioridade 1: mais importantes, sempre lidos
    (0x09, "inj_time_ms",   2, lambda b: (b[0] << 8 | b[1]) * 0.016),   # ms de injeção
    (0x08, "timing_deg",    1, lambda b: b[0] / 2.0 - 64.0),            # avanço ignição
    (0x06, "map_kpa",       1, lambda b: float(b[0])),                   # MAP Renault
    (0x0A, "lambda_mV",     1, lambda b: float(b[0] * 5)),               # sonda λ em mV
    (0x10, "knock_count",   1, lambda b: float(b[0])),                   # detonação
    # Prioridade 2: lidos se timeout permitir
    (0x0D, "ltft_pct",      1, lambda b: b[0] * 100.0 / 128.0 - 100.0),
    (0x0B, "load_pct",      1, lambda b: b[0] * 100.0 / 255.0),
    (0x15, "inj_duty_pct",  1, lambda b: b[0] * 100.0 / 255.0),
    (0x03, "coolant_c",     1, lambda b: float(b[0] - 40)),
    (0x01, "rpm",           2, lambda b: (b[0] << 8 | b[1]) / 4.0),
]

# Somente estes IDs num snapshot "rápido"
_FAST_IDS = {0x09, 0x08, 0x06, 0x0A, 0x10}


def _parse_m21_response(cleaned: str, local_id: int, num_bytes: int,
                        decoder) -> Optional[float]:
    """Extrai valor de uma resposta Mode 21 (61 XX [dados])."""
    expected = f"61{local_id:02X}"
    idx = cleaned.upper().find(expected)
    if idx < 0:
        return None
    hex_data = cleaned[idx + 4: idx + 4 + num_bytes * 2]
    if len(hex_data) < num_bytes * 2:
        return None
    try:
        data = bytes.fromhex(hex_data)
        return decoder(data)
    except Exception:
        return None


def collect_m21_snapshot(
    proto: Elm327Protocol,
    quick: bool = False,
    progress_cb=None,
) -> dict:
    """Coleta snapshot Mode 21 via CAN.

    Args:
        proto: protocolo ELM327 já inicializado
        quick: se True, lê só os 5 parâmetros mais importantes (~2-3s)
        progress_cb: callable(str) para reportar progresso na UI

    Returns:
        dict com os valores lidos (chaves = key_name, valores = float ou None)
    """
    result: dict = {}
    params = [p for p in _SNAPSHOT_PARAMS if p[0] in _FAST_IDS] if quick else _SNAPSHOT_PARAMS

    def _log_cb(msg: str):
        _log.debug("M21 snapshot: %s", msg)
        if progress_cb:
            progress_cb(msg)

    try:
        # ── 1. Configura CAN para o ECU motor ────────────────────────────
        _log_cb("Configurando CAN (AT SH 7E0)…")
        r = proto.send("ATSP 0", timeout_s=2.0)
        r = proto.send("AT SH 7E0", timeout_s=1.5)
        if r.status != ResponseStatus.SUCCESS:
            _log_cb(f"AT SH falhou: {r.status.value}")
            return result

        # ── 2. Sessão diagnóstica (tenta 10 03, 10 01, 10 02) ───────────
        _session_ok = False
        _session_byte = None
        for _sb in ("03", "01", "02"):
            _cmd = f"10 {_sb}"
            _log_cb(f"Tentando sessão {_cmd}…")
            r = proto.send(_cmd, timeout_s=2.5)
            if r.status == ResponseStatus.UNABLE_CONNECT:
                continue
            _c = r.cleaned.upper()
            _pos = f"50{_sb}"
            if _c.startswith(_pos) or _pos in _c:
                _session_ok = True
                _session_byte = _sb
                _log_cb(f"Sessão aceita com {_cmd}")
                break
            elif not _c.startswith("7F"):
                # Resposta não-negativa — ECU provavelmente aceitou
                _session_ok = True
                _session_byte = _sb
                _log_cb(f"Sessão parcial com {_cmd}: {_c[:16]}")
                break
            # 7F = recusado, tenta próximo

        if not _session_ok:
            _log_cb("Nenhuma sessão aceita — tentando Mode 21 mesmo assim")

        # ── 3. Lê parâmetros Mode 21 ──────────────────────────────────────
        for _param_idx, (local_id, key, nbytes, decoder) in enumerate(params):
            if progress_cb:
                progress_cb(f"Lendo 21 {local_id:02X} ({key})…")

            # TesterPresent baseado no índice do loop (não em len(result)),
            # para que sessões com muitas falhas não percam o heartbeat.
            if _param_idx > 0 and _param_idx % 3 == 0:
                proto.send("3E 00", timeout_s=0.5)

            r = proto.send(f"21 {local_id:02X}", timeout_s=2.0)
            if r.status == ResponseStatus.SUCCESS:
                val = _parse_m21_response(r.cleaned, local_id, nbytes, decoder)
                if val is not None:
                    result[key] = val
                    _log_cb(f"  21 {local_id:02X} {key} = {val:.2f}")
                else:
                    # Verifica resposta negativa UDS
                    neg = r.cleaned.upper()
                    if f"7F21" in neg:
                        _log_cb(f"  21 {local_id:02X} → ECU recusou (7F)")
                    else:
                        _log_cb(f"  21 {local_id:02X} → PARSE_ERROR: {r.cleaned[:20]}")
            else:
                _log_cb(f"  21 {local_id:02X} → {r.status.value}")

        _log_cb(f"Snapshot completo: {len(result)}/{len(params)} parâmetros lidos")

    except Exception as exc:
        _log.exception("M21 snapshot error: %s", exc)
        _log_cb(f"Erro no snapshot: {exc}")

    finally:
        # ── 4. Restaura ELM327 para OBD2 normal ──────────────────────────
        _log.debug("M21 snapshot: restaurando ELM327…")
        try:
            proto.send("10 01", timeout_s=1.0)   # volta para default session
        except Exception:
            pass
        try:
            proto.send("ATSP 0", timeout_s=1.5)   # auto protocolo
            proto.send("AT SH 7DF", timeout_s=1.0) # header broadcast OBD2
        except Exception:
            pass

    return result


def format_m21_for_display(m21: dict) -> list[str]:
    """Formata dados Mode 21 para exibição em texto."""
    LABELS = {
        "inj_time_ms":   ("Tempo injeção",   "ms"),
        "timing_deg":    ("Avanço ignição",   "°"),
        "map_kpa":       ("MAP Renault",      "kPa"),
        "lambda_mV":     ("Sonda λ (Renault)","mV"),
        "knock_count":   ("Cont. detonação",  "cnt"),
        "ltft_pct":      ("LTFT Renault",     "%"),
        "load_pct":      ("Carga (Renault)",  "%"),
        "inj_duty_pct":  ("Duty injetor",     "%"),
        "coolant_c":     ("Temp. arref.",     "°C"),
        "rpm":           ("RPM (Renault)",    "rpm"),
    }
    lines = []
    for key, val in m21.items():
        label, unit = LABELS.get(key, (key, ""))
        lines.append(f"{label}: {val:.2f} {unit}")
    return lines
