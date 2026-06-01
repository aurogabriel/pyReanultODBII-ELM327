"""Transport Bluetooth SPP Clássico no Android via Pyjnius.
Usa BluetoothSocket + UUID SPP padrão (00001101-0000-1000-8000-00805F9B34FB).

CRÍTICO: requer permissões em runtime no Android 12+:
- BLUETOOTH_CONNECT (Android 12+)
- BLUETOOTH (legado)
- ACCESS_FINE_LOCATION (necessário pra listagem em alguns Androids)

A descoberta de pareados precisa ser pedida ANTES no nível da UI.
"""

import threading
import time
from typing import Optional

from .base import IObdTransport, TransportError

# UUID padrão Serial Port Profile (SPP)
_SPP_UUID = "00001101-0000-1000-8000-00805F9B34FB"


class SppTransport(IObdTransport):
    def __init__(self, mac_address: str, read_buffer_size: int = 1024):
        self._mac = mac_address
        self._buf_size = read_buffer_size
        self._socket = None
        self._in_stream = None
        self._out_stream = None
        self._connected = False
        self._lock = threading.Lock()

    def connect(self) -> bool:
        try:
            from jnius import autoclass
            BluetoothAdapter = autoclass("android.bluetooth.BluetoothAdapter")
            UUID = autoclass("java.util.UUID")

            adapter = BluetoothAdapter.getDefaultAdapter()
            if adapter is None or not adapter.isEnabled():
                raise TransportError("Bluetooth desligado ou indisponível")

            device = adapter.getRemoteDevice(self._mac)
            uuid = UUID.fromString(_SPP_UUID)
            # createRfcommSocketToServiceRecord é o caminho oficial
            self._socket = device.createRfcommSocketToServiceRecord(uuid)

            if self._socket is None:
                raise TransportError("Falha ao criar Socket: dispositivo nulo ou indisponível")
            
            
            adapter.cancelDiscovery()
            # Adicione este log para debugar o que está acontecendo antes do connect
            print("Tentando conectar no socket...")
            self._socket.connect()
            # Cancela discovery antes de conectar (recomendação Android)
            adapter.cancelDiscovery()
            self._socket.connect()
            self._in_stream = self._socket.getInputStream()
            self._out_stream = self._socket.getOutputStream()
            self._connected = True
            return True
        except Exception as e:
            raise TransportError(f"Falha conexão SPP: {e}") from e

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            for obj_name in ("_in_stream", "_out_stream", "_socket"):
                obj = getattr(self, obj_name, None)
                if obj is not None:
                    try:
                        obj.close()
                    except Exception:
                        pass
                    setattr(self, obj_name, None)

    def is_connected(self) -> bool:
        return self._connected

    def write(self, data: bytes) -> int:
        if not self._connected or self._out_stream is None:
            raise TransportError("Não conectado")
        with self._lock:
            try:
                # OutputStream.write(byte[]) — Pyjnius converte bytes Python pra byte[] Java
                self._out_stream.write(data)
                self._out_stream.flush()
                return len(data)
            except Exception as e:
                self._connected = False
                raise TransportError(f"write falhou: {e}") from e

    def read(self, max_bytes: int = 256) -> bytes:
        if not self._connected or self._in_stream is None:
            raise TransportError("Não conectado")
        try:
            available = self._in_stream.available()
            if available <= 0:
                return b""
            to_read = min(available, max_bytes, self._buf_size)
            # InputStream.read(byte[]) precisa de array Java; usamos read() byte a byte
            # mas em chunk pra reduzir round-trips JNI
            buf = bytearray()
            for _ in range(to_read):
                b = self._in_stream.read()
                if b < 0:
                    break
                buf.append(b & 0xFF)
            return bytes(buf)
        except Exception as e:
            self._connected = False
            raise TransportError(f"read falhou: {e}") from e

    def flush(self) -> None:
        # Drena qualquer lixo residual do buffer
        try:
            while True:
                chunk = self.read(512)
                if not chunk:
                    break
                time.sleep(0.005)
        except TransportError:
            pass


def list_paired_devices() -> list[dict]:
    """Retorna lista de dispositivos pareados (nome, mac)."""
    try:
        from jnius import autoclass
        BluetoothAdapter = autoclass("android.bluetooth.BluetoothAdapter")
        adapter = BluetoothAdapter.getDefaultAdapter()
        if adapter is None:
            return []
        devices = adapter.getBondedDevices().toArray()
        return [
            {"name": d.getName() or "?", "mac": d.getAddress()}
            for d in devices
        ]
    except Exception:
        return []
