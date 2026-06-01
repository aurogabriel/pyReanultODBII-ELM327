"""Entry point Windows — conecta ELM327 via porta COM serial."""

import flet as ft

# Ativa logging antes de importar qualquer módulo da app
from src.debug_log import setup_logging
setup_logging()

import logging
logging.getLogger(__name__).info("=== OBD Logan Scanner iniciando ===")

from src.ui import main as ui_main

if __name__ == "__main__":
    ft.run(ui_main)