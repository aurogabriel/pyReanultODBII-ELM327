"""Bluetooth Clássico SPP para Windows via WinRT (Windows.Devices.Bluetooth).

Usa a API WinRT nativa do Windows 10/11 — não depende de porta COM nem de
configuração de socket AF_BTH. Requer apenas que o dispositivo esteja pareado.

Dependências (instaladas via pip):
  winrt-Windows.Devices.Bluetooth
  winrt-Windows.Devices.Bluetooth.Rfcomm
  winrt-Windows.Networking.Sockets
  winrt-Windows.Storage.Streams
  winrt-Windows.Foundation
  winrt-Windows.Networking
"""

import asyncio
import threading
import time
import winreg
from typing import Optional

from .base import IObdTransport, TransportError
from ..debug_log import get_logger

_log = get_logger(__name__)
_SPP_SHORT_ID = 0x1101  # Serial Port Profile


# ---------------------------------------------------------------------------
# Helpers assíncronos WinRT (executados num loop dedicado)
# ---------------------------------------------------------------------------

def _run_in_new_loop(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _winrt_connect(mac_int: int):
    """Conecta via WinRT e retorna (sock, writer, reader) ou lança exceção."""
    from winrt.windows.devices.bluetooth import BluetoothDevice, BluetoothConnectionStatus
    from winrt.windows.devices.bluetooth.rfcomm import RfcommServiceId
    from winrt.windows.networking.sockets import StreamSocket
    from winrt.windows.storage.streams import DataWriter, DataReader, InputStreamOptions

    mac_str = ":".join(f"{(mac_int >> (8*i)) & 0xFF:02X}" for i in range(5, -1, -1))
    _log.debug("WinRT: buscando dispositivo MAC=%s (int=%d)", mac_str, mac_int)

    dev = await BluetoothDevice.from_bluetooth_address_async(mac_int)
    if dev is None:
        _log.error("WinRT: BluetoothDevice retornou None para MAC=%s", mac_str)
        raise TransportError(
            "Dispositivo não encontrado no Windows. "
            "Verifique se o MAC está correto e o dispositivo está pareado."
        )

    _log.debug(
        "WinRT: dispositivo encontrado — nome='%s'  status=%s",
        dev.name, dev.connection_status,
    )

    svc_id = RfcommServiceId.from_short_id(_SPP_SHORT_ID)
    _log.debug("WinRT: consultando SDP para UUID SPP (0x%04X)…", _SPP_SHORT_ID)

    result = await dev.get_rfcomm_services_for_id_async(svc_id)
    _log.debug(
        "WinRT: SDP resultado — erro=%d  serviços_encontrados=%d",
        result.error, len(result.services),
    )

    if result.error != 0 or len(result.services) == 0:
        _log.error(
            "WinRT: SPP não encontrado — erro_sdp=%d  n_servicos=%d  "
            "→ leitor provavelmente desligado ou sem energia",
            result.error, len(result.services),
        )
        raise TransportError(
            "O leitor OBD não respondeu ao Bluetooth.\n\n"
            "➤ SOLUÇÃO: plugue o leitor na tomada OBD do carro (sob o painel) "
            "para ele receber energia, depois tente novamente.\n\n"
            f"(Detalhe técnico: erro SDP={result.error}, serviços={len(result.services)})"
        )

    svc = result.services[0]
    host  = svc.connection_host_name
    svc_n = svc.connection_service_name
    _log.debug("WinRT: serviço SPP encontrado — host=%s  service=%s", host, svc_n)

    _log.debug("WinRT: abrindo StreamSocket…")
    sock = StreamSocket()
    await sock.connect_async(host, svc_n)
    _log.info("WinRT: StreamSocket conectado com sucesso a %s", mac_str)

    writer = DataWriter(sock.output_stream)
    reader = DataReader(sock.input_stream)
    reader.input_stream_options = InputStreamOptions.PARTIAL

    return sock, writer, reader


# ---------------------------------------------------------------------------
# Transport síncrono que envolve a API WinRT assíncrona
# ---------------------------------------------------------------------------

class BluetoothDirectTransport(IObdTransport):
    """Conecta ao ELM327 via Bluetooth Clássico SPP usando WinRT."""

    def __init__(self, mac: str, conn_timeout_s: float = 20.0):
        if isinstance(mac, str):
            self._mac_str = mac
            self._mac_int = int(mac.replace(":", ""), 16)
        else:
            self._mac_int = int(mac)
            self._mac_str = ":".join(f"{(mac >> (8*i)) & 0xFF:02X}" for i in range(5, -1, -1))

        self._conn_timeout = conn_timeout_s
        self._sock = None
        self._writer = None
        self._reader = None
        self._write_loop: Optional[asyncio.AbstractEventLoop] = None
        self._write_thread: Optional[threading.Thread] = None
        self._rx_buf = bytearray()
        self._rx_lock = threading.Lock()
        self._read_thread: Optional[threading.Thread] = None
        self._connected = False
        self._write_lock = threading.Lock()
        self._bytes_sent = 0
        self._bytes_recv = 0

    # ------------------------------------------------------------------ ciclo de vida

    def connect(self) -> bool:
        _log.info("BT connect: iniciando conexão WinRT para %s (timeout=%ds)",
                  self._mac_str, self._conn_timeout)
        # Tenta conectar com até 2 tentativas (alguns erros são transitórios)
        last_exc: Optional[Exception] = None
        for attempt in range(1, 3):
            try:
                _log.debug("BT connect: tentativa %d/2", attempt)
                sock, writer, reader = _run_in_new_loop(_winrt_connect(self._mac_int))
                last_exc = None
                break  # sucesso
            except TransportError:
                raise
            except OSError as e:
                last_exc = e
                we = getattr(e, "winerror", 0)
                _log.warning("BT connect: OSError tentativa %d — winerror=%s  %s", attempt, we, e)
                if we in (-2147014848,):  # WSAEADDRINUSE: socket em uso
                    raise TransportError(
                        "Conexão Bluetooth já em uso (sessão anterior ainda aberta).\n"
                        "Aguarde 10 segundos e tente novamente."
                    ) from e
                # WSATYPE_NOT_FOUND (-2147014788) e outros: retry após pausa
                if attempt < 2:
                    _log.debug("BT connect: aguardando 5s antes de retry…")
                    time.sleep(5)
            except Exception as e:
                last_exc = e
                _log.exception("BT connect: exceção inesperada tentativa %d", attempt)
                if attempt < 2:
                    time.sleep(3)

        if last_exc is not None:
            raise TransportError(
                f"Falha Bluetooth após 2 tentativas: {last_exc}\n\n"
                "Dica: verifique se o leitor OBD está plugado no carro e com LED aceso."
            ) from last_exc

        self._sock   = sock
        self._writer = writer
        self._reader = reader
        self._connected = True
        _log.debug("BT connect: socket OK, iniciando loops de leitura/escrita")

        self._write_loop = asyncio.new_event_loop()
        self._write_thread = threading.Thread(
            target=self._run_write_loop, daemon=True, name="bt-write"
        )
        self._write_thread.start()

        self._read_thread = threading.Thread(
            target=self._reader_worker, daemon=True, name="bt-read"
        )
        self._read_thread.start()
        _log.info("BT connect: pronto — %s", self._mac_str)
        return True

    def _run_write_loop(self):
        asyncio.set_event_loop(self._write_loop)
        _log.debug("BT write-loop: started")
        self._write_loop.run_forever()
        _log.debug("BT write-loop: stopped")

    def _reader_worker(self):
        """Thread dedicada: lê continuamente do WinRT e enche o buffer."""
        _log.debug("BT reader-worker: started")

        async def _loop():
            while self._connected:
                try:
                    count = await self._reader.load_async(512)
                    if count > 0:
                        chunk = bytearray([self._reader.read_byte() for _ in range(count)])
                        with self._rx_lock:
                            self._rx_buf.extend(chunk)
                            self._bytes_recv += count
                        _log.debug("BT rx %d bytes: %r", count, bytes(chunk))
                except Exception as exc:
                    _log.warning("BT reader-worker: exceção → desconectando: %s", exc)
                    break
            self._connected = False
            _log.debug("BT reader-worker: stopped (connected=%s)", self._connected)

        loop = asyncio.new_event_loop()
        loop.run_until_complete(_loop())
        loop.close()

    def disconnect(self) -> None:
        _log.info("BT disconnect: encerrando conexão %s  (sent=%dB recv=%dB)",
                  self._mac_str, self._bytes_sent, self._bytes_recv)
        self._connected = False
        if self._sock:
            try:
                self._sock.close()
            except Exception as e:
                _log.debug("BT disconnect: erro ao fechar socket: %s", e)
            self._sock = self._writer = self._reader = None
        if self._write_loop and self._write_loop.is_running():
            self._write_loop.call_soon_threadsafe(self._write_loop.stop)

    def is_connected(self) -> bool:
        return self._connected and self._sock is not None

    # ------------------------------------------------------------------ I/O

    def write(self, data: bytes) -> int:
        if not self.is_connected():
            raise TransportError("Bluetooth não conectado")
        _log.debug("BT tx %d bytes: %r", len(data), data)
        with self._write_lock:
            try:
                async def _do_write():
                    self._writer.write_bytes(bytearray(data))
                    await self._writer.store_async()
                fut = asyncio.run_coroutine_threadsafe(_do_write(), self._write_loop)
                fut.result(timeout=5.0)
                self._bytes_sent += len(data)
                return len(data)
            except Exception as e:
                _log.error("BT write falhou: %s  (data=%r)", e, data)
                self._connected = False
                raise TransportError(f"write BT falhou: {e}") from e

    def read(self, max_bytes: int = 256) -> bytes:
        with self._rx_lock:
            if not self._rx_buf:
                return b""
            chunk = bytes(self._rx_buf[:max_bytes])
            del self._rx_buf[:max_bytes]
            return chunk

    def flush(self) -> None:
        """Drena o buffer até o stream BT ficar quieto (sem novos bytes).

        Fast-path: se o buffer já estiver vazio e nenhum byte chegar em 30ms,
        retorna imediatamente sem atraso extra.
        Slow-path: aguarda até 1.5s até que nenhum byte chegue por 120ms.
        """
        QUICK_MS  = 0.030   # verificação rápida (buffer vazio)
        QUIET_MS  = 0.120   # pausa de silêncio para slow-path
        MAX_DRAIN = 1.5     # timeout máximo do slow-path

        with self._rx_lock:
            had_data = bool(self._rx_buf)
            snap_before = self._bytes_recv
            self._rx_buf.clear()

        time.sleep(QUICK_MS)

        with self._rx_lock:
            new_bytes = self._bytes_recv - snap_before
            self._rx_buf.clear()

        if not had_data and new_bytes == 0:
            _log.debug("BT flush: fast-path (buffer limpo)")
            return  # já estava quieto

        # Slow-path: drenar até 120ms de silêncio
        _log.debug("BT flush: slow-path (had_data=%s  new_bytes=%d)", had_data, new_bytes)
        deadline = time.perf_counter() + MAX_DRAIN
        while time.perf_counter() < deadline:
            with self._rx_lock:
                snap = self._bytes_recv
                self._rx_buf.clear()

            time.sleep(QUIET_MS)

            with self._rx_lock:
                arrived = self._bytes_recv - snap
                self._rx_buf.clear()

            if arrived == 0:
                _log.debug("BT flush: stream quieto, flush concluído")
                return

        _log.warning("BT flush: slow-path timeout — buffer pode estar sujo")
        with self._rx_lock:
            self._rx_buf.clear()


# ---------------------------------------------------------------------------
# Enumeração de dispositivos pareados via registro do Windows
# ---------------------------------------------------------------------------

def list_paired_bt_devices() -> list[dict]:
    """Lê dispositivos Bluetooth pareados do registro do Windows."""
    _log.debug("Registry: lendo dispositivos BT pareados…")
    devices = []
    reg_path = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
    except OSError as e:
        _log.warning("Registry: não foi possível abrir chave BT: %s", e)
        return devices

    i = 0
    while True:
        try:
            mac_hex = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1

        if len(mac_hex) != 12:
            _log.debug("Registry: chave ignorada (comprimento inválido): %s", mac_hex)
            continue

        try:
            dev_key = winreg.OpenKey(root, mac_hex)
        except OSError as e:
            _log.debug("Registry: erro ao abrir chave %s: %s", mac_hex, e)
            continue

        name: str | None = None
        try:
            name_raw, _ = winreg.QueryValueEx(dev_key, "Name")
            if isinstance(name_raw, bytes):
                name = name_raw.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
            else:
                name = str(name_raw).strip()
        except FileNotFoundError:
            _log.debug("Registry: %s sem chave 'Name' (entrada incompleta)", mac_hex)
        finally:
            winreg.CloseKey(dev_key)

        if name:
            mac = ":".join(mac_hex[j:j + 2].upper() for j in range(0, 12, 2))
            _log.debug("Registry: dispositivo encontrado — '%s'  MAC=%s", name, mac)
            devices.append({"name": name, "mac": mac})

    winreg.CloseKey(root)
    _log.info("Registry: %d dispositivo(s) BT encontrado(s): %s",
              len(devices), [d["name"] for d in devices])
    return devices
