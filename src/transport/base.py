"""Interface de transporte. SRP: só define contrato I/O.
DIP: scheduler/protocol dependem disso, não de SPP/BLE/Mock concretos."""

from abc import ABC, abstractmethod


class IObdTransport(ABC):
    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def write(self, data: bytes) -> int: ...

    @abstractmethod
    def read(self, max_bytes: int = 256) -> bytes:
        """Leitura não-bloqueante. Retorna bytes disponíveis (pode ser b'')."""
        ...

    @abstractmethod
    def flush(self) -> None: ...


class TransportError(Exception):
    """Erro de baixo nível (socket fechou, timeout fatal, etc)."""
