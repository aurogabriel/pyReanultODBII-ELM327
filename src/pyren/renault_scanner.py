"""Scanner Renault Mode 21/22 via ELM327.

Suporte a dois protocolos conforme o que o carro usa:

  CAN (ISO 15765-4) — Logan 2012 detectado: MANTÉM ATSP atual
    • AT SH 7E0        → direciona para ECU motor (CAN ID 0x7E0)
    • AT CRA 7E8       → filtra resposta da ECU (CAN ID 0x7E8)
    • 21 XX            → ReadDataByLocalIdentifier via CAN frame
    • Resposta: 61 XX [dados]

  KWP2000 (ISO 14230) — para carros mais antigos
    • ATSP 5           → KWP2000 fast init
    • AT SH 82 10 F1   → cabeçalho físico motor
    • 10 92            → StartDiagnosticSession extended
    • 21 XX            → ReadDataByLocalIdentifier KWP
    • 18 02 FF 00      → ReadDTCsByStatus Renault
"""

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..protocol import Elm327Protocol, ResponseStatus
from ..debug_log import get_logger
from .ecu_database import EcuDefinition, RenaultParam, ALL_ECUS, ENGINE_ECU

_log = get_logger(__name__)


@dataclass
class RenaultSample:
    local_id: int
    name: str
    description: str
    raw_bytes: bytes
    value: Optional[float]
    unit: str
    status: str  # "OK" | "NO_DATA" | "PARSE_ERROR" | "NOT_SUPPORTED" | "ERROR"
    raw_response: str = ""


@dataclass
class RenaultReport:
    ecu: str = ""
    protocol_used: str = ""       # "CAN" | "KWP2000" | "UNKNOWN"
    ecu_version: Optional[str] = None
    session_ok: bool = False
    live_data: list[RenaultSample] = field(default_factory=list)
    renault_dtcs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class RenaultScanner:
    """Acessa ECUs Renault via Mode 21 usando ELM327.

    Detecta automaticamente se o carro usa CAN ou KWP2000 e
    configura o ELM327 adequadamente.
    """

    # CAN IDs para ECUs Renault (11-bit)
    _CAN_ENGINE_REQ  = "7E0"
    _CAN_ENGINE_RESP = "7E8"

    def __init__(self, protocol: Elm327Protocol):
        self._proto = protocol
        self._is_can: Optional[bool] = None

    # ------------------------------------------------------------------
    # Detecção de protocolo
    # ------------------------------------------------------------------

    def _detect_protocol(self) -> bool:
        """Retorna True se o carro usa CAN (ISO 15765-x)."""
        proto_name = (self._proto.detected_protocol or "").upper()
        _log.debug("Renault scanner: protocolo detectado pelo ELM327: %r", proto_name)
        is_can = "CAN" in proto_name or "15765" in proto_name or "ISO15765" in proto_name
        self._is_can = is_can
        _log.info("Renault scanner: modo=%s", "CAN" if is_can else "KWP2000")
        return is_can

    # ------------------------------------------------------------------
    # Setup CAN
    # ------------------------------------------------------------------

    # Códigos de resposta negativa UDS (7F SID NRC)
    _UDS_NRC = {
        "11": "serviceNotSupported",
        "12": "subFunctionNotSupported",
        "13": "incorrectMessageLength",
        "22": "conditionsNotCorrect",
        "24": "requestSequenceError",
        "31": "requestOutOfRange",
        "33": "securityAccessDenied",
        "35": "invalidKey",
        "78": "requestCorrectlyReceived_ResponsePending",
    }

    def _is_negative_response(self, raw: str, service_id: int) -> tuple[bool, str]:
        """Retorna (True, motivo) se raw contém resposta negativa UDS (7F SID NRC)."""
        # Formato: 7F SID NRC (hex, sem espaços)
        sid_hex = f"{service_id:02X}"
        neg_prefix = f"7F{sid_hex}"
        if neg_prefix in raw.upper():
            idx = raw.upper().find(neg_prefix)
            nrc = raw[idx + 4: idx + 6].upper() if len(raw) >= idx + 6 else "??"
            reason = self._UDS_NRC.get(nrc, f"nrc=0x{nrc}")
            return True, f"ECU recusou: {reason}"
        # Resposta genérica negativa sem SID
        if raw.upper().startswith("7F"):
            return True, "ECU retornou resposta negativa"
        return False, ""

    def _setup_can_session(self, ecu: EcuDefinition) -> tuple[bool, str]:
        """Configura ELM327 e abre sessão estendida com ECU Renault via CAN.

        O ECU Renault Sirius/Valéo na maioria dos Logan CAN (ISO 15765-4) só
        responde ao Mode 21 após uma sessão estendida de diagnóstico (UDS 10 03).
        Sem isso, o ECU retorna 7F 21 11 (serviceNotSupported) em cada pedido.
        """
        _log.debug("Renault: configurando sessão CAN para ECU %s", ecu.name)

        # Garante protocolo auto-detect
        r = self._proto.send("ATSP 0", timeout_s=2.0)
        _log.debug("Renault CAN: ATSP 0 → %s", r.status.value)

        req_id = self._CAN_ENGINE_REQ
        resp_id = self._CAN_ENGINE_RESP

        # Seta header CAN (sem AT CRA para máxima compatibilidade com clones)
        r = self._proto.send(f"AT SH {req_id}", timeout_s=2.0)
        if r.status != ResponseStatus.SUCCESS:
            return False, f"AT SH {req_id} falhou: {r.status.value}"

        # Desliga headers para simplificar parse (AT H0)
        self._proto.send("AT H0", timeout_s=1.0)

        # ── Sessão diagnóstica UDS ────────────────────────────────────────
        # ECUs Renault Sirius variam no byte de sessão aceito:
        #   10 03 = extendedDiagnosticSession (UDS padrão) → resposta 50 03
        #   10 01 = defaultSession                          → resposta 50 01
        #   10 02 = programmingSession                      → resposta 50 02
        # Alguns ECUs aceitam Mode 21 sem nenhuma sessão especial.
        _session_ok = False
        _session_byte_used = None
        for _sb in ("03", "01", "02"):
            _cmd = f"10 {_sb}"
            _log.debug("Renault CAN: tentando sessão %s…", _cmd)
            r = self._proto.send(_cmd, timeout_s=3.0)
            _log.debug("Renault CAN: %s → status=%s  raw=%r", _cmd, r.status.value, r.raw[:40])

            if r.status == ResponseStatus.UNABLE_CONNECT:
                _log.warning("Renault CAN: ECU sem resposta para %s", _cmd)
                continue

            _c = r.cleaned.upper()
            _pos = f"50{_sb}"
            if _c.startswith(_pos) or _pos in _c:
                _log.info("Renault CAN: sessão aceita com %s (→ %s)", _cmd, _pos)
                _session_ok = True
                _session_byte_used = _sb
                break
            elif _c.startswith("7F"):
                nrc = _c[4:6] if len(_c) >= 6 else "??"
                reason = self._UDS_NRC.get(nrc, f"nrc=0x{nrc}")
                _log.warning("Renault CAN: %s recusado (%s)", _cmd, reason)
            else:
                # Resposta inesperada mas sem 7F — ECU pode ter aceito
                _log.debug("Renault CAN: %s resposta parcial %r — aceitando", _cmd, _c[:20])
                _session_ok = True
                _session_byte_used = _sb
                break

        if not _session_ok:
            _log.warning("Renault CAN: nenhuma sessão aceita (10 03/01/02) — tentando Mode 21 assim mesmo")

        # ── Testa Mode 21 agora que a sessão está aberta ─────────────────
        _log.debug("Renault CAN: testando Mode 21 (21 01 = RPM)…")
        r = self._proto.send("21 01", timeout_s=3.0)
        _log.debug("Renault CAN: 21 01 → status=%s  raw=%r", r.status.value, r.raw[:60])

        is_neg, reason = self._is_negative_response(r.cleaned, 0x21)
        _sess_label = f"10 {_session_byte_used}" if _session_byte_used else "sem sessão"
        if is_neg:
            _log.warning("Renault CAN: ECU recusou Mode 21 (sessão=%s): %s", _sess_label, reason)
            # Retorna True — talvez outros IDs funcionem mesmo com 21 01 recusado
            return True, f"CAN (21 01 recusado: {reason})"

        if r.status == ResponseStatus.UNABLE_CONNECT:
            return False, "ECU não respondeu ao Mode 21 — verifique se carro está ligado"

        return True, f"CAN+Sessão({_sess_label}) | ECU={req_id}→{resp_id}"

    # ------------------------------------------------------------------
    # Setup KWP2000
    # ------------------------------------------------------------------

    def _setup_kwp_session(self, ecu: EcuDefinition, extended: bool = True) -> tuple[bool, str]:
        """Configura ELM327 e abre sessão KWP2000 no ECU alvo."""
        _log.debug("Renault: configurando sessão KWP2000 para ECU %s", ecu.name)

        r = self._proto.send("ATSP 5", timeout_s=2.0)
        if r.status != ResponseStatus.SUCCESS:
            return False, f"ATSP 5 falhou: {r.status.value}"

        fmt, target, src = ecu.kwp_header
        header_cmd = f"AT SH {fmt:02X} {target:02X} {src:02X}"
        r = self._proto.send(header_cmd, timeout_s=2.0)
        if r.status != ResponseStatus.SUCCESS:
            return False, f"ATSH falhou: {r.status.value}"

        # AT H1 necessário: KWP2000 slow-init (BUSINI) precisa ver o header
        # de resposta para completar o handshake físico ISO 14230.
        # AT H0 antes do 10 XX causava BUSINI:...ERROR.
        self._proto.send("AT H1", timeout_s=1.0)

        session_sub = "92" if extended else "81"
        r = self._proto.send(f"10 {session_sub}", timeout_s=5.0)
        if r.status == ResponseStatus.UNABLE_CONNECT:
            return False, "ECU KWP não respondeu — verifique carro ligado e ECU correta"
        _log.debug("Renault KWP: sessão 10 %s → %s  %r", session_sub, r.status.value, r.cleaned[:30])

        # Desliga headers após sessão estabelecida: simplifica parse Mode 21
        self._proto.send("AT H0", timeout_s=1.0)
        return True, f"KWP2000 | header={header_cmd}"

    def close_session(self) -> None:
        """Restaura ELM327 para estado neutro após scan Renault."""
        _log.debug("Renault: fechando sessão, restaurando ELM327")
        try:
            # Volta à sessão default (10 01) antes de sair
            self._proto.send("10 01", timeout_s=1.0)
        except Exception:
            pass
        try:
            self._proto.send("AT H0",   timeout_s=1.0)
            self._proto.send("AT SH 7DF", timeout_s=1.0)  # header broadcast padrão
            self._proto.send("ATSP 0",  timeout_s=2.0)
        except Exception as e:
            _log.warning("Renault close_session: erro ao restaurar (ignorado): %s", e)

    # ------------------------------------------------------------------
    # Leitura Mode 21
    # ------------------------------------------------------------------

    def _read_local_id(self, local_id: int, num_bytes: int, timeout_s: float = 2.5) -> tuple[Optional[bytes], str, str]:
        """Envia '21 XX' e extrai bytes de dados.

        Retorna (data_bytes, status, raw_response).
        """
        cmd = f"21 {local_id:02X}"
        r = self._proto.send(cmd, timeout_s=timeout_s)
        raw = r.cleaned.upper()
        _log.debug("Renault Mode21 [%s] → status=%s  raw=%r", cmd, r.status.value, raw[:60])

        if r.status == ResponseStatus.NO_DATA:
            return None, "NO_DATA", raw
        if r.status == ResponseStatus.UNABLE_CONNECT:
            return None, "NOT_SUPPORTED", raw
        if r.status == ResponseStatus.TIMEOUT:
            return None, "TIMEOUT", raw
        if r.status not in (ResponseStatus.SUCCESS, ResponseStatus.ELM_ERROR):
            return None, r.status.value, raw

        # ── Detecta resposta negativa UDS (7F SID NRC) ─────────────────
        is_neg, reason = self._is_negative_response(raw, 0x21)
        if is_neg:
            _log.debug("Renault Mode21 [%s]: resposta negativa: %s", cmd, reason)
            return None, f"NEG_RESP({reason})", raw

        # ── Busca prefixo de resposta positiva '61 XX' ──────────────────
        expected = f"61{local_id:02X}"
        # Com AT H0 (sem header): raw = "6101XXYY..."
        # Com AT H1 (com header): raw = "7E80461 01XXYY..." → cleaned = "7E804610 1XXYY"
        match = re.search(expected + r"([0-9A-F]*)", raw)
        if not match:
            _log.debug("Renault Mode21 [%s]: PARSE_ERROR — esperado %r em %r",
                       cmd, expected, raw[:60])
            # Tenta detectar se o BUS INIT foi disparado (KWP2000 residual)
            if "BUSINI" in raw or "BUSIN" in raw:
                return None, "BUS_INIT_ERROR (ELM em KWP2000?)", raw
            return None, "PARSE_ERROR", raw

        payload_hex = match.group(1)
        needed_chars = num_bytes * 2
        if len(payload_hex) < needed_chars:
            _log.debug("Renault Mode21 [%s]: SHORT — %d/%d bytes",
                       cmd, len(payload_hex)//2, num_bytes)
            if len(payload_hex) < 2:
                return None, f"SHORT({len(payload_hex)//2}/{num_bytes}B)", raw
            needed_chars = len(payload_hex) - (len(payload_hex) % 2)

        try:
            data = bytes.fromhex(payload_hex[:needed_chars])
            return data, "OK", raw
        except ValueError:
            return None, "HEX_ERROR", raw

    def _tester_present(self) -> None:
        """Envia 3E 00 (TesterPresent) para manter sessão estendida viva."""
        try:
            self._proto.send("3E 00", timeout_s=1.0)
            _log.debug("Renault: TesterPresent 3E 00 enviado")
        except Exception:
            pass

    def read_all_params(
        self,
        ecu: EcuDefinition,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> list[RenaultSample]:
        samples = []
        ok_count = 0
        tester_every = 5  # envia TesterPresent a cada N parâmetros

        for i, param in enumerate(ecu.params):
            # Mantém sessão estendida ativa
            if i > 0 and i % tester_every == 0:
                self._tester_present()

            data, status, raw = self._read_local_id(param.local_id, param.num_bytes)
            value = None
            if data is not None and status == "OK":
                try:
                    value = param.decoder(data)
                    ok_count += 1
                except Exception as e:
                    status = f"DECODE_ERROR: {e}"

            sample = RenaultSample(
                local_id=param.local_id,
                name=param.name,
                description=param.description,
                raw_bytes=data or b"",
                value=value,
                unit=param.unit,
                status=status,
                raw_response=raw,
            )
            samples.append(sample)

            if progress_cb:
                if value is not None:
                    val_str = f"{value:.2f} {param.unit}"
                    icon = "✓"
                else:
                    val_str = status
                    icon = "✗" if "ERROR" in status or status in ("NO_DATA", "TIMEOUT") else "–"
                progress_cb(f"[21 {param.local_id:02X}] {icon} {param.description}: {val_str}")

            time.sleep(0.08)  # pequena pausa para o ECU processar

        _log.info("Renault Mode21: %d/%d parâmetros lidos com sucesso", ok_count, len(ecu.params))
        return samples

    # ------------------------------------------------------------------
    # DTCs Renault (Mode 0x18 — KWP2000 only)
    # ------------------------------------------------------------------

    def read_renault_dtcs(self) -> list[str]:
        if self._is_can:
            _log.debug("Renault: DTCs Mode 18 pulados (CAN — não suportado via ELM327 genérico)")
            return []

        r = self._proto.send("18 02 FF 00", timeout_s=3.0)
        if r.status != ResponseStatus.SUCCESS:
            return []
        cleaned = r.cleaned
        if not cleaned.startswith("58"):
            return []
        try:
            n_dtcs = int(cleaned[2:4], 16)
        except ValueError:
            return []

        dtcs = []
        payload = cleaned[4:]
        for i in range(n_dtcs):
            chunk = payload[i * 4: i * 4 + 4]
            if len(chunk) == 4 and chunk != "0000":
                dtcs.append(self._decode_renault_dtc(chunk))
        return dtcs

    @staticmethod
    def _decode_renault_dtc(hex4: str) -> str:
        try:
            a = int(hex4[0:2], 16)
            b = hex4[2:4]
        except ValueError:
            return f"?{hex4}"
        letter = {0: "P", 1: "C", 2: "B", 3: "U"}[(a & 0xC0) >> 6]
        d1 = (a & 0x30) >> 4
        d2 = a & 0x0F
        return f"{letter}{d1}{d2:X}{b}"

    # ------------------------------------------------------------------
    # Scan completo
    # ------------------------------------------------------------------

    def full_scan(
        self,
        ecu: EcuDefinition = ENGINE_ECU,
        progress_cb: Optional[Callable[[str], None]] = None,
    ) -> RenaultReport:
        report = RenaultReport(ecu=ecu.name)

        def _log_cb(msg: str):
            report.notes.append(msg)
            _log.debug("Renault scan: %s", msg)
            if progress_cb:
                progress_cb(msg)

        # Detecta protocolo do carro
        is_can = self._detect_protocol()
        report.protocol_used = "CAN" if is_can else "KWP2000"
        _log_cb(f"Protocolo do carro: {report.protocol_used}")

        # Abre sessão adequada
        if is_can:
            ok, msg = self._setup_can_session(ecu)
        else:
            ok, msg = self._setup_kwp_session(ecu)

        report.session_ok = ok
        _log_cb(f"Sessão: {'OK' if ok else 'FALHOU'} — {msg}")

        if not ok:
            report.errors.append(f"Falha ao abrir sessão: {msg}")
            return report

        # Versão ECU (Mode 0x1A, subfunction 0x80)
        r = self._proto.send("1A 80", timeout_s=2.0)
        if r.status == ResponseStatus.SUCCESS and r.cleaned:
            report.ecu_version = r.cleaned
            _log_cb(f"Versão ECU: {r.cleaned}")
        else:
            _log_cb("Versão ECU: não disponível")

        # Leitura Mode 21
        _log_cb(f"Lendo {len(ecu.params)} parâmetros Mode 21…")
        report.live_data = self.read_all_params(ecu, progress_cb=_log_cb)

        ok_count   = sum(1 for s in report.live_data if s.status == "OK")
        err_count  = sum(1 for s in report.live_data if s.status not in ("OK", "NO_DATA"))
        _log_cb(f"Mode 21: {ok_count} OK  |  {err_count} com erro  |  {len(report.live_data)-ok_count-err_count} sem dados")

        if err_count > 0:
            errs = [(s.description, s.status, s.raw_response) for s in report.live_data if s.status not in ("OK", "NO_DATA")]
            for desc, st, raw in errs[:5]:
                report.errors.append(f"[21 {desc}] {st}  raw={raw[:30]!r}")

        # DTCs (só KWP2000)
        if not is_can:
            _log_cb("Lendo DTCs Renault (Mode 18)…")
            report.renault_dtcs = self.read_renault_dtcs()
            if report.renault_dtcs:
                _log_cb(f"DTCs: {', '.join(report.renault_dtcs)}")
            else:
                _log_cb("Nenhum DTC Renault")
        else:
            _log_cb("DTCs Mode 18: não disponível em CAN (use aba OBD2 → Scan para DTCs padrão)")

        self.close_session()
        return report
