"""Gate permissões Android 11+. SRP: só permissões.

Compatível com Flet 0.85+ rodando em serious_python runtime.
"""

from __future__ import annotations

import os
import platform
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


def _log(msg: str) -> None:
    """Print para logcat via stderr."""
    try:
        print(f"[PERMISSIONS] {msg}", flush=True)
        sys.stderr.write(f"[PERMISSIONS] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _is_android() -> bool:
    return "ANDROID_STORAGE" in os.environ or (
        platform.system() == "Linux" and "android" in platform.release().lower()
    )


@dataclass(frozen=True)
class PermissionResult:
    granted: list[str]
    denied: list[str]

    @property
    def all_granted(self) -> bool:
        return not self.denied


class IPermissionGate(ABC):
    @abstractmethod
    def request_bluetooth(self) -> PermissionResult: ...
    @abstractmethod
    def request_storage(self) -> PermissionResult: ...
    @abstractmethod
    def has_storage(self) -> bool: ...
    @abstractmethod
    def has_bluetooth(self) -> bool: ...


class DesktopPermissionGate(IPermissionGate):
    def request_bluetooth(self) -> PermissionResult:
        return PermissionResult(["DESKTOP"], [])

    def request_storage(self) -> PermissionResult:
        return PermissionResult(["DESKTOP"], [])

    def has_storage(self) -> bool:
        return True

    def has_bluetooth(self) -> bool:
        return True


class AndroidPermissionGate(IPermissionGate):
    BT_PERMS = [
        "android.permission.BLUETOOTH_CONNECT",
        "android.permission.BLUETOOTH_SCAN",
    ]
    STORAGE_SETTLE_TIMEOUT_S = 120.0
    BT_REQUEST_TIMEOUT_S = 30.0

    def __init__(self):
        _log("__init__ START")
        from jnius import autoclass
        _log("jnius imported")

        activity_host_class = os.getenv("MAIN_ACTIVITY_HOST_CLASS_NAME")
        _log(f"MAIN_ACTIVITY_HOST_CLASS_NAME={activity_host_class!r}")

        # Lista de candidatos. Se a env var existir, ela vai primeiro.
        candidates: list[str] = []
        if activity_host_class:
            candidates.append(activity_host_class)
        candidates.extend([
            "br.com.seuprojeto.obd_logan_scanner.MainActivity",
            "com.flet.serious_python.MainActivity",
            "io.flutter.embedding.android.FlutterActivity",
        ])

        self._cached_activity = None
        last_err = None
        for cand in candidates:
            _log(f"tentando: {cand}")
            try:
                host = autoclass(cand)
                acti = getattr(host, "mActivity", None)
                if acti is not None:
                    self._cached_activity = acti
                    _log(f"OK com {cand}")
                    break
                _log(f"{cand}.mActivity é None")
            except Exception as e:
                last_err = e
                _log(f"falhou {cand}: {e!r}")

        if self._cached_activity is None:
            raise RuntimeError(
                f"Nenhuma Activity acessível. Último erro: {last_err!r}"
            )

        _log("carregando classes auxiliares")
        self._ContextCompat = autoclass("androidx.core.content.ContextCompat")
        self._ActivityCompat = autoclass("androidx.core.app.ActivityCompat")
        self._Environment = autoclass("android.os.Environment")
        self._Settings = autoclass("android.provider.Settings")
        self._Intent = autoclass("android.content.Intent")
        self._Uri = autoclass("android.net.Uri")
        PackageManager = autoclass("android.content.pm.PackageManager")
        self._GRANTED = PackageManager.PERMISSION_GRANTED
        _log("__init__ OK")

    @property
    def _activity(self):
        return self._cached_activity

    def _runtime_check(self, perm: str) -> bool:
        return (
            self._ContextCompat.checkSelfPermission(self._activity, perm)
            == self._GRANTED
        )

    def _runtime_request(self, perms: list[str], timeout_s: float) -> PermissionResult:
        pending = [p for p in perms if not self._runtime_check(p)]
        if not pending:
            return PermissionResult(list(perms), [])
        self._ActivityCompat.requestPermissions(self._activity, pending, 1001)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(0.4)
            if all(self._runtime_check(p) for p in pending):
                break
        granted = [p for p in perms if self._runtime_check(p)]
        denied = [p for p in perms if not self._runtime_check(p)]
        return PermissionResult(granted, denied)

    def has_bluetooth(self) -> bool:
        return all(self._runtime_check(p) for p in self.BT_PERMS)

    def request_bluetooth(self) -> PermissionResult:
        return self._runtime_request(self.BT_PERMS, self.BT_REQUEST_TIMEOUT_S)

    def has_storage(self) -> bool:
        return self._Environment.isExternalStorageManager()

    def request_storage(self) -> PermissionResult:
        if self._Environment.isExternalStorageManager():
            return PermissionResult(["MANAGE_EXTERNAL_STORAGE"], [])
        self._open_all_files_settings()
        deadline = time.time() + self.STORAGE_SETTLE_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(1.0)
            if self._Environment.isExternalStorageManager():
                return PermissionResult(["MANAGE_EXTERNAL_STORAGE"], [])
        return PermissionResult([], ["MANAGE_EXTERNAL_STORAGE"])

    def _open_all_files_settings(self):
        pkg = self._activity.getPackageName()
        try:
            intent = self._Intent(
                self._Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION
            )
            intent.setData(self._Uri.parse(f"package:{pkg}"))
            intent.addFlags(0x10000000)  # FLAG_ACTIVITY_NEW_TASK
            self._activity.startActivity(intent)
        except Exception as e:
            _log(f"fallback intent: {e!r}")
            intent = self._Intent(
                self._Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION
            )
            intent.addFlags(0x10000000)
            self._activity.startActivity(intent)


def build_permission_gate() -> IPermissionGate:
    if not _is_android():
        return DesktopPermissionGate()
    try:
        return AndroidPermissionGate()
    except BaseException as e:
        _log(f"FALLBACK Desktop: {e!r}")
        return DesktopPermissionGate()