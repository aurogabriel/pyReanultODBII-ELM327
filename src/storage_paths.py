"""Resolve diretório. SRP. Documents público no Android, home no desktop."""

from __future__ import annotations

import os
import platform
from abc import ABC, abstractmethod
from pathlib import Path


def _is_android() -> bool:
    return "ANDROID_STORAGE" in os.environ or (
        platform.system() == "Linux" and "android" in platform.release().lower()
    )


class IStorageLocator(ABC):
    @abstractmethod
    def resolve(self) -> Path: ...


class DesktopStorage(IStorageLocator):
    def resolve(self) -> Path:
        p = Path.home() / "ObdLoganData"
        p.mkdir(parents=True, exist_ok=True)
        return p


class AndroidPublicDocumentsStorage(IStorageLocator):
    """Requer MANAGE_EXTERNAL_STORAGE concedido."""

    def resolve(self) -> Path:
        base = Path("/storage/emulated/0/Documents/ObdLogan")
        base.mkdir(parents=True, exist_ok=True)
        return base


def build_storage_locator() -> IStorageLocator:
    if _is_android():
        return AndroidPublicDocumentsStorage()
    return DesktopStorage()