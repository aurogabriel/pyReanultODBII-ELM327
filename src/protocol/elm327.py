"""Protocolo ELM327. SRP: conhece sintaxe (CR, prompt '>', AT*, modos).
Não conhece nada de Bluetooth (delega ao transport) nem de PIDs específicos."""

import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..transport.base import IObdTransport, TransportError
from ..debug_log import get_logger

_log = get_logger(__name__)


class ResponseStatus(Enum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    NO_DATA = "NO_DATA"
    UNABLE_CONNECT = "UNABLE_CONNECT"
    BUS_ERROR = "BUS_ERROR"
    STOPPED = "STOPPED"
    SEARCHING = "SEARCHING"
    ELM_ERROR = "ELM_ERROR"
    CORRUPTED = "CORRUPTED"


@dataclass
class ElmResponse:
    raw: str
    cleaned: str          # sem espaços, sem prompt, sem eco
    status: ResponseStatus
    elapsed_ms: float
    command: str


_ERROR_MARKERS = {
    "NODATA": ResponseStatus.NO_DATA,
    "UNABLETOCONNECT": ResponseStatus.UNABLE_CONNECT,
    "BUSERROR": ResponseStatus.BUS_ERROR,
    "STOPPED": ResponseStatus.STOPPED,
    "SEARCHING": ResponseStatus.SEARCHING,
    "?": ResponseStatus.ELM_ERROR,
}


class Elm327Protocol:
    """Wrapper sobre transport. Faz envio + leitura até prompt '>'."""

    PROMPT = b">"

    def __init__(
        self,
        transport: IObdTransport,
        default_timeout_s: float = 1.5,
        init_timeout_s: float = 15.0,
        read_poll_interval_s: float = 0.01,
    ):
        self._tx = transport
        self._timeout = default_timeout_s
        self._init_timeout = init_timeout_s
        self._poll = read_poll_interval_s
        self._protocol_name: Optional[str] = None

    @property
    def detected_protocol(self) -> Optional[str]:
        return self._protocol_name

    def send(self, command: str, timeout_s: Optional[float] = None) -> ElmResponse:
        """Envia comando e lê até '>' ou timeout."""
        timeout = timeout_s if timeout_s is not None else self._timeout
        cmd_bytes = (command.strip() + "\r").encode("ascii")
        t0 = time.perf_counter()

        _log.debug("ELM327 >> %r  (timeout=%.1fs)", command, timeout)

        try:
            self._tx.flush()
            self._tx.write(cmd_bytes)
        except TransportError as e:
            _log.error("ELM327 write error on %r: %s", command, e)
            return ElmResponse(
                raw=str(e), cleaned="", status=ResponseStatus.CORRUPTED,
                elapsed_ms=(time.perf_counter() - t0) * 1000, command=command,
            )

        buf = bytearray()
        deadline = t0 + timeout
        while time.perf_counter() < deadline:
            try:
                chunk = self._tx.read(512)
            except TransportError as e:
                _log.error("ELM327 read error on %r: %s", command, e)
                return ElmResponse(
                    raw=str(e), cleaned="", status=ResponseStatus.CORRUPTED,
                    elapsed_ms=(time.perf_counter() - t0) * 1000, command=command,
                )
            if chunk:
                buf.extend(chunk)
                if self.PROMPT in buf:
                    break
            else:
                time.sleep(self._poll)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        raw = buf.decode("ascii", errors="ignore")

        if self.PROMPT.decode() not in raw:
            # Sem prompt — tenta recuperar dados do buffer antes de declarar timeout.
            # Clone ELM327s frequentemente enviam dados mas atrasam o '>'.
            cleaned_partial = self._clean_partial(raw, command)
            if cleaned_partial:
                status_partial = self._classify(cleaned_partial)
                _log.warning(
                    "ELM327 PARTIAL %r  %.0fms  buf=%r → cleaned=%r  status=%s",
                    command, elapsed_ms, raw[:60], cleaned_partial[:40], status_partial.value,
                )
                return ElmResponse(
                    raw=raw, cleaned=cleaned_partial, status=status_partial,
                    elapsed_ms=elapsed_ms, command=command,
                )
            _log.warning(
                "ELM327 TIMEOUT %r  elapsed=%.0fms  buf=%r",
                command, elapsed_ms, raw[:80],
            )
            return ElmResponse(
                raw=raw, cleaned="", status=ResponseStatus.TIMEOUT,
                elapsed_ms=elapsed_ms, command=command,
            )

        cleaned = self._clean(raw, command)
        status = self._classify(cleaned)
        _log.debug(
            "ELM327 << %r  cleaned=%r  status=%s  %.0fms",
            command, cleaned[:60], status.value, elapsed_ms,
        )
        return ElmResponse(
            raw=raw, cleaned=cleaned, status=status,
            elapsed_ms=elapsed_ms, command=command,
        )

    @staticmethod
    def _clean(raw: str, cmd: str) -> str:
        # Remove CR, LF, espaços, prompt
        s = re.sub(r"[\r\n\s>]", "", raw).upper()
        # Remove eco do comando se presente (caso ATE não tenha desligado ainda)
        cmd_clean = cmd.replace(" ", "").upper()
        if s.startswith(cmd_clean):
            s = s[len(cmd_clean):]
        # Remove prefixo "SEARCHING..." emitido pelo ELM durante auto-detect:
        # o ELM envia "SEARCHING...\r\n41 0C...\r\n>" como um único burst.
        # Sem esse strip, _classify() descartaria a resposta válida que vem depois.
        s = re.sub(r"^SEARCHING\.*", "", s)
        return s

    @staticmethod
    def _clean_partial(raw: str, cmd: str) -> str:
        """Extrai primeira linha de dados útil de um buffer sem prompt '>'."""
        _SKIP = ("SEARCHING", "BUS INIT", "FB ERROR", "CAN ERROR")
        lines = [l.strip() for l in raw.replace(">", "").split("\r") if l.strip()]
        for line in lines:
            upper = line.upper().replace(" ", "")
            if any(upper.startswith(s.replace(" ", "")) for s in _SKIP):
                continue
            # Deve conter pelo menos 2 caracteres hex
            hex_only = re.sub(r"[^0-9A-F]", "", upper)
            if len(hex_only) < 2:
                continue
            cmd_clean = cmd.replace(" ", "").upper()
            if hex_only.startswith(cmd_clean):
                hex_only = hex_only[len(cmd_clean):]
            return hex_only
        return ""

    @staticmethod
    def _classify(cleaned: str) -> ResponseStatus:
        for marker, status in _ERROR_MARKERS.items():
            if marker in cleaned:
                return status
        if not cleaned:
            return ResponseStatus.CORRUPTED
        return ResponseStatus.SUCCESS

    def initialize(self) -> tuple[bool, str]:
        """Sequência de init robusta. Retorna (ok, mensagem)."""
        _log.info("ELM327 init: iniciando sequência de inicialização")

        r = self.send("ATZ", timeout_s=3.0)
        # ATZ pode retornar só '\r\r>' (prompt) sem banner de versão quando o ELM327
        # estava no meio de outro comando — aceita CORRUPTED com raw contendo '>'
        _atz_ok = r.status in (ResponseStatus.SUCCESS, ResponseStatus.ELM_ERROR) or (
            r.status == ResponseStatus.CORRUPTED and ">" in r.raw
        )
        if not _atz_ok:
            _log.error("ELM327 init: ATZ falhou — status=%s raw=%r", r.status.value, r.raw[:80])
            return False, f"ATZ falhou: {r.status.value} ({r.raw[:80]})"
        _log.debug("ELM327 init: ATZ OK — versão=%r (status=%s)", r.cleaned, r.status.value)
        time.sleep(0.4)  # Mais tempo após ATZ para o ELM327 se estabilizar

        for cmd in ("ATE0", "ATL0", "ATS0", "ATH0", "ATSP0"):
            r = self.send(cmd, timeout_s=2.0)
            # Aceita prompt-only (CORRUPTED com '>') — AT command foi processado
            # mas echo já estava desligado então não há texto "OK", só o prompt.
            _at_ok = r.status == ResponseStatus.SUCCESS or (
                r.status == ResponseStatus.CORRUPTED and ">" in r.raw
            )
            if not _at_ok:
                _log.error("ELM327 init: %s falhou — status=%s raw=%r",
                           cmd, r.status.value, r.raw[:60])
                return False, f"{cmd} falhou: {r.status.value}"
            _log.debug("ELM327 init: %s OK (status=%s)", cmd, r.status.value)
            time.sleep(0.05)

        last_status = None
        for attempt in range(3):
            _log.debug("ELM327 init: 0100 tentativa %d/3…", attempt + 1)
            r = self.send("0100", timeout_s=self._init_timeout)
            last_status = r.status
            _log.debug("ELM327 init: 0100 resposta — status=%s cleaned=%r",
                       r.status.value, r.cleaned[:40])
            if r.status == ResponseStatus.SUCCESS:
                break
            time.sleep(0.3)
        else:
            _log.error("ELM327 init: 0100 falhou após 3 tentativas — último status=%s",
                       last_status.value if last_status else "?")
            return False, f"0100 inicial falhou após 3 tentativas: {last_status.value}"

        r_dp = self.send("ATDP", timeout_s=1.0)
        if r_dp.status == ResponseStatus.SUCCESS:
            self._protocol_name = r_dp.cleaned
        _log.info("ELM327 init: sucesso — protocolo=%s", self._protocol_name or "desconhecido")
        return True, f"OK | Protocolo: {self._protocol_name or 'desconhecido'}"

    def keep_alive(self) -> None:
        """Envia ATPC pra evitar timeout do barramento (carros ISO 9141)."""
        try:
            self.send("0100", timeout_s=1.0)
        except Exception:
            pass
