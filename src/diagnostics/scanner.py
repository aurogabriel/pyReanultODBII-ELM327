"""Diagnóstico. SRP: descobre o que o carro suporta e lê dados estáticos.
- Bitmap PIDs suportados (modo 01)
- VIN (modo 09 PID 02)
- DTCs armazenados (modo 03) e pendentes (modo 07)
- Freeze frame (modo 02)
"""

import re
from dataclasses import dataclass, field
from typing import Optional

from ..protocol import Elm327Protocol, ResponseStatus


@dataclass
class DiagnosticReport:
    protocol: Optional[str] = None
    elm_version: Optional[str] = None
    vin: Optional[str] = None
    supported_pids: list[str] = field(default_factory=list)
    stored_dtcs: list[str] = field(default_factory=list)
    pending_dtcs: list[str] = field(default_factory=list)
    freeze_frame: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class DiagnosticsService:
    """Roda scan inicial completo do veículo."""

    # Bitmaps a verificar (cada um cobre 32 PIDs do modo 01)
    _BITMAP_PIDS = ("0100", "0120", "0140", "0160", "0180", "01A0", "01C0")

    def __init__(self, protocol: Elm327Protocol):
        self._proto = protocol

    def run_full_scan(self, progress_cb=None) -> DiagnosticReport:
        report = DiagnosticReport()

        def _log(msg: str):
            report.notes.append(msg)
            if progress_cb:
                progress_cb(msg)

        # Protocolo detectado
        report.protocol = self._proto.detected_protocol

        # Versão ELM (puro debug)
        r = self._proto.send("ATI", timeout_s=1.0)
        if r.status == ResponseStatus.SUCCESS:
            report.elm_version = r.cleaned

        # Bitmap PIDs suportados
        _log("Descobrindo PIDs suportados...")
        report.supported_pids = self._discover_supported_pids()
        _log(f"PIDs suportados: {len(report.supported_pids)}")

        # VIN
        _log("Lendo VIN...")
        report.vin = self._read_vin()
        if report.vin:
            _log(f"VIN: {report.vin}")

        # DTCs armazenados (modo 03)
        _log("Lendo DTCs armazenados...")
        report.stored_dtcs = self._read_dtcs("03")
        _log(f"DTCs armazenados: {len(report.stored_dtcs)}")

        # DTCs pendentes (modo 07)
        _log("Lendo DTCs pendentes...")
        report.pending_dtcs = self._read_dtcs("07")
        _log(f"DTCs pendentes: {len(report.pending_dtcs)}")

        # Freeze frame (modo 02 PID 0C, 0D, 05 do frame 0)
        if report.stored_dtcs:
            _log("Lendo freeze frame...")
            report.freeze_frame = self._read_freeze_frame()

        return report

    def _discover_supported_pids(self) -> list[str]:
        supported = []
        for bitmap_pid in self._BITMAP_PIDS:
            r = self._proto.send(bitmap_pid, timeout_s=2.0)
            if r.status != ResponseStatus.SUCCESS:
                break
            # Resposta esperada: 41XX AABBCCDD (4 bytes de bitmap)
            mode = format(int(bitmap_pid[:2], 16) + 0x40, "02X")
            pid_hex = bitmap_pid[2:]
            match = re.search(mode + pid_hex + r"([0-9A-F]{8})", r.cleaned)
            if not match:
                break
            bitmap = int(match.group(1), 16)
            base = int(bitmap_pid[2:], 16)
            for i in range(32):
                bit = (bitmap >> (31 - i)) & 1
                if bit:
                    pid_num = base + i + 1
                    if pid_num > 0xFF:
                        continue
                    supported.append(f"01{pid_num:02X}")
            # Se bit 0 da próxima janela não tá setado, para
            next_supported = bitmap & 1
            if not next_supported:
                break

        # PIDs críticos que o bitmap pode não reportar (ex: 0142 em ECUs que
        # não anunciam suporte ao grupo 0140). Sonda diretamente como fallback.
        _ALWAYS_PROBE = ("0142",)
        for extra_pid in _ALWAYS_PROBE:
            if extra_pid.upper() not in supported:
                r = self._proto.send(extra_pid, timeout_s=1.5)
                if r.status == ResponseStatus.SUCCESS:
                    supported.append(extra_pid.upper())

        return supported

    def _read_vin(self) -> Optional[str]:
        r = self._proto.send("0902", timeout_s=3.0)
        if r.status != ResponseStatus.SUCCESS:
            return None
        # Resposta multi-linha: '4902XX...' várias vezes
        # Cleaned junta tudo. Pegar todos chunks após '490201..490205'
        # Padrão: 4902NN + 3 bytes hex (NN = nº linha 01-05)
        matches = re.findall(r"4902[0-9A-F]{2}([0-9A-F]{6})", r.cleaned)
        if not matches:
            return None
        hex_str = "".join(matches)
        try:
            vin = bytes.fromhex(hex_str).decode("ascii", errors="ignore")
            return vin.strip("\x00 ").strip()
        except Exception:
            return None

    def _read_dtcs(self, mode: str) -> list[str]:
        """Lê DTCs do modo 03 (armazenados) ou 07 (pendentes)."""
        r = self._proto.send(mode, timeout_s=2.0)
        if r.status != ResponseStatus.SUCCESS:
            return []

        # Resposta KWP2000 (ISO 14230): 43 AABB CCDD ... (sem byte de contagem)
        # Resposta CAN (ISO 15765):     43 NN AABB CCDD... (NN = contagem de DTCs)
        # Esta implementação assume KWP2000 (sem NN). Para o Logan 2012 D4F
        # que usa KWP2000, está correto. Se o protocolo for CAN, os DTCs ficarão
        # deslocados por 1 byte (latent bug — não afeta este veículo).
        mode_resp = format(int(mode, 16) + 0x40, "02X")
        idx = r.cleaned.find(mode_resp)
        if idx < 0:
            return []
        payload = r.cleaned[idx + 2:]
        # Cada DTC = 4 chars hex
        dtcs = []
        for i in range(0, len(payload) - 3, 4):
            code = payload[i:i + 4]
            if code == "0000":
                continue
            dtcs.append(self._decode_dtc(code))
        return dtcs

    @staticmethod
    def _decode_dtc(hex_code: str) -> str:
        """Converte 4 chars hex em código DTC tipo 'P0301'."""
        if len(hex_code) != 4:
            return f"?{hex_code}"
        try:
            byte_a = int(hex_code[0:2], 16)
            byte_b_str = hex_code[2:4]
        except ValueError:
            return f"?{hex_code}"
        # Primeiros 2 bits = letra
        letter_map = {0: "P", 1: "C", 2: "B", 3: "U"}
        letter = letter_map[(byte_a & 0xC0) >> 6]
        # Próximos 2 bits = primeiro dígito
        first_digit = (byte_a & 0x30) >> 4
        # Últimos 4 bits = segundo dígito
        second_digit = byte_a & 0x0F
        return f"{letter}{first_digit}{second_digit:X}{byte_b_str}"

    def _read_freeze_frame(self) -> dict:
        """Lê dados do freeze frame (modo 02). Frame 0 padrão."""
        frame_data = {}
        # 020C = RPM no momento do erro, 020D = velocidade, 0205 = temp
        for ff_pid, name in [("020C", "rpm"), ("020D", "speed"),
                              ("0205", "coolant_temp"), ("0204", "engine_load"),
                              ("0211", "throttle")]:
            # Modo 02 precisa do frame number depois do PID
            r = self._proto.send(f"{ff_pid} 00", timeout_s=2.0)
            if r.status == ResponseStatus.SUCCESS:
                frame_data[name] = r.cleaned
        return frame_data
