"""Opt-in WinSparkle update support for the Windows setup window."""

import base64
import ctypes
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Callable
from urllib.parse import urlsplit

from ccworks import __version__

REGISTRY_PATH = r"Software\ccworks\WinSparkle"
_CALLBACK = ctypes.CFUNCTYPE(None)
_CAN_SHUTDOWN = ctypes.CFUNCTYPE(ctypes.c_int)


@dataclass(frozen=True)
class UpdateConfig:
    feed_url: str
    public_key: str

    @classmethod
    def parse(cls, feed_url: str, public_key: str) -> "UpdateConfig | None":
        if not feed_url and not public_key:
            return None
        if not feed_url or not public_key:
            raise ValueError(
                "Set both CCWORKS_WINSPARKLE_FEED_URL and CCWORKS_WINSPARKLE_PUBLIC_KEY."
            )
        try:
            url = urlsplit(feed_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username is not None
                or url.query
                or url.fragment
                or not feed_url.isascii()
                or any(ord(char) < 33 or ord(char) == 127 for char in feed_url)
            ):
                raise ValueError
        except ValueError:
            raise ValueError(
                "The WinSparkle appcast must be a stable HTTPS URL."
            ) from None
        try:
            if len(base64.b64decode(public_key, validate=True)) != 32:
                raise ValueError
        except ValueError:
            raise ValueError(
                "The WinSparkle public key must be a base64-encoded Ed25519 key."
            ) from None
        return cls(feed_url, public_key)


class WinSparkle:
    def __init__(
        self,
        directory: Path,
        config: UpdateConfig,
        notify: Callable[[str], None],
        can_shutdown: Callable[[], bool] = lambda: True,
    ):
        dll = ctypes.CDLL(str(directory / "WinSparkle.dll"))
        self._dll = dll
        self._dismissed = _CALLBACK(lambda: notify("dismissed"))
        self._shutdown = _CALLBACK(lambda: notify("shutdown"))
        self._can_shutdown = _CAN_SHUTDOWN(lambda: int(can_shutdown()))
        functions = {
            "set_app_details": ([ctypes.c_wchar_p] * 3, None),
            "set_appcast_url": ([ctypes.c_char_p], None),
            "set_eddsa_public_key": ([ctypes.c_char_p], ctypes.c_int),
            "set_registry_path": ([ctypes.c_char_p], None),
            "set_automatic_check_for_updates": ([ctypes.c_int], None),
            "set_update_dismissed_callback": ([_CALLBACK], None),
            "set_shutdown_request_callback": ([_CALLBACK], None),
            "set_can_shutdown_callback": ([_CAN_SHUTDOWN], None),
            "init": ([], None),
            "cleanup": ([], None),
            "check_update_with_ui": ([], None),
        }
        for name, (arguments, result) in functions.items():
            function = getattr(dll, f"win_sparkle_{name}")
            function.argtypes = arguments
            function.restype = result
        if not dll.win_sparkle_set_eddsa_public_key(config.public_key.encode("ascii")):
            raise ValueError("WinSparkle rejected the update public key.")
        dll.win_sparkle_set_app_details("ccworks", "ccworks Desktop", __version__)
        dll.win_sparkle_set_appcast_url(config.feed_url.encode("ascii"))
        dll.win_sparkle_set_registry_path(REGISTRY_PATH.encode("ascii"))
        dll.win_sparkle_set_automatic_check_for_updates(0)
        dll.win_sparkle_set_update_dismissed_callback(self._dismissed)
        dll.win_sparkle_set_shutdown_request_callback(self._shutdown)
        dll.win_sparkle_set_can_shutdown_callback(self._can_shutdown)
        dll.win_sparkle_init()

    @classmethod
    def from_bundle(
        cls, notify: Callable[[str], None], can_shutdown: Callable[[], bool]
    ) -> "WinSparkle | None":
        bundle = getattr(sys, "_MEIPASS", None)
        if sys.platform != "win32" or bundle is None:
            return None
        directory = Path(bundle) / "ccworks-update"
        try:
            settings = json.loads(
                (directory / "settings.json").read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None
        config = UpdateConfig.parse(settings["feed_url"], settings["public_key"])
        if config is None:
            raise ValueError("Bundled Windows update settings are empty.")
        return cls(directory, config, notify, can_shutdown)

    def check(self) -> None:
        self._dll.win_sparkle_check_update_with_ui()

    def close(self) -> None:
        self._dll.win_sparkle_cleanup()


def remove_update_preferences() -> None:
    if sys.platform == "win32":
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH)
        except FileNotFoundError:
            pass
