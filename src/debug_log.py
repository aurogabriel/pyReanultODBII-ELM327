"""Configuração central de logging para o OBD Logan Scanner.

Grava em ~/ObdLoganData/debug.log com rotação automática (5 MB × 3 arquivos).
Nível DEBUG por padrão — captura tudo. Também escreve WARNING+ no console.

Uso em qualquer módulo:
    from src.debug_log import get_logger
    log = get_logger(__name__)
    log.debug("mensagem")
"""

import logging
import logging.handlers
from pathlib import Path


_LOG_DIR  = Path.home() / "ObdLoganData"
_LOG_FILE = _LOG_DIR / "debug.log"
_FMT      = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s — %(message)s"
_DATE_FMT = "%H:%M:%S"

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Silencia loggers externos ruidosos
    for noisy in ("flet", "flet_core", "flet_desktop", "flet_transport",
                  "flet_controls", "asyncio", "urllib3", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Arquivo rotativo: 5 MB × 3 backups
    fh = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))
    root.addHandler(fh)

    # Console: só WARNING+
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(ch)

    logging.getLogger(__name__).debug("=== Logging iniciado ===  arquivo: %s", _LOG_FILE)


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


def log_path() -> Path:
    return _LOG_FILE
