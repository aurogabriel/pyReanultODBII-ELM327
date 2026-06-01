"""Windows/Desktop COM port transport for ELM327 OBD2 adapters.

Compatible with:
- USB ELM327 (CH340, FTDI, CP2102 chipsets) — typically COM3-COM9
- Bluetooth ELM327 paired as COM port via Windows Bluetooth settings
  (appears as "Standard Serial over Bluetooth link", BTHENUM in HW ID)
"""

import threading
import time
from typing import Optional

import serial
import serial.tools.list_ports

from .base import IObdTransport, TransportError
from ..debug_log import get_logger

_log = get_logger(__name__)
_DEFAULT_BAUD = 38400
_BT_HWID_MARKERS = ("BTHENUM", "00001101")


class SerialTransport(IObdTransport):
    """COM port transport. Funciona com USB e Bluetooth ELM327 no Windows."""

    def __init__(self, port: str, baud: int = _DEFAULT_BAUD, read_timeout_s: float = 0.05):
        self._port = port
        self._baud = baud
        self._timeout = read_timeout_s
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()
        self._connected = False

    def connect(self) -> bool:
        _log.info("Serial connect: abrindo %s @ %d baud", self._port, self._baud)
        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self._timeout,
                write_timeout=2.0,
                xonxoff=False,
                rtscts=False,
            )
            self._connected = True
            _log.info("Serial connect: %s aberta com sucesso", self._port)
            return True
        except serial.SerialException as e:
            _log.error("Serial connect: falha ao abrir %s — %s", self._port, e)
            raise TransportError(f"Falha ao abrir {self._port}: {e}") from e

    def disconnect(self) -> None:
        self._connected = False
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception:
                pass
        self._serial = None

    def is_connected(self) -> bool:
        return self._connected and self._serial is not None and self._serial.is_open

    def write(self, data: bytes) -> int:
        if not self.is_connected():
            raise TransportError("Porta serial não conectada")
        with self._lock:
            try:
                n = self._serial.write(data)
                self._serial.flush()
                return n
            except serial.SerialException as e:
                self._connected = False
                raise TransportError(f"write falhou em {self._port}: {e}") from e

    def read(self, max_bytes: int = 256) -> bytes:
        if not self.is_connected():
            raise TransportError("Porta serial não conectada")
        try:
            waiting = self._serial.in_waiting
            if waiting <= 0:
                return b""
            return self._serial.read(min(waiting, max_bytes))
        except serial.SerialException as e:
            self._connected = False
            raise TransportError(f"read falhou em {self._port}: {e}") from e

    def flush(self) -> None:
        if self._serial and self._serial.is_open:
            try:
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
            except Exception:
                pass


def list_com_ports() -> list[dict]:
    """Lista portas COM disponíveis no sistema."""
    _log.debug("list_com_ports: escaneando portas seriais…")
    result = []
    for info in sorted(serial.tools.list_ports.comports(), key=lambda p: p.device):
        hwid = (info.hwid or "").upper()
        is_bt = any(k in hwid for k in _BT_HWID_MARKERS)
        _log.debug("  porta: %s  desc=%r  bt=%s  hwid=%s",
                   info.device, info.description, is_bt, hwid[:60])
        result.append({
            "port": info.device,
            "description": info.description or info.device,
            "is_bluetooth": is_bt,
        })
    result.append({"port": "MOCK", "description": "Mock ELM327 (teste sem carro)", "is_bluetooth": False})
    return result
