"""Secure local storage for MAX credentials on Windows."""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes

CREDENTIAL_TARGET = "Сборщик информации/MAX API"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168
MAX_CREDENTIAL_BLOB_SIZE = 5120


class _CredentialW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _wincred():
    if os.name != "nt":
        raise RuntimeError("Хранение MAX-учётных данных доступно только в Windows")
    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    library.CredWriteW.argtypes = [ctypes.POINTER(_CredentialW), wintypes.DWORD]
    library.CredWriteW.restype = wintypes.BOOL
    library.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CredentialW))]
    library.CredReadW.restype = wintypes.BOOL
    library.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    library.CredDeleteW.restype = wintypes.BOOL
    library.CredFree.argtypes = [ctypes.c_void_p]
    library.CredFree.restype = None
    return library


def _read_stored_credentials() -> dict[str, str]:
    if os.name != "nt":
        return {}
    library = _wincred()
    credential = ctypes.POINTER(_CredentialW)()
    if not library.CredReadW(CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
        error = ctypes.get_last_error()
        if error == ERROR_NOT_FOUND:
            return {}
        raise OSError(error, "Не удалось прочитать MAX-учётные данные из Диспетчера учётных данных Windows")
    try:
        raw = ctypes.string_at(credential.contents.CredentialBlob,
                               credential.contents.CredentialBlobSize)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            return {}
        return {key: item for key, item in value.items()
                if key in {"token", "channel_id"} and isinstance(item, str)}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Сохранённая запись MAX повреждена; задайте её заново командой credentials set-max")
    finally:
        library.CredFree(credential)


def save_max_credentials(token: str, channel_id: str) -> None:
    """Save secrets for the current Windows user without printing them."""
    token = token.strip()
    channel_id = channel_id.strip()
    if not token:
        raise ValueError("Токен MAX не может быть пустым")
    if not channel_id or len(channel_id) > 256 or any(char.isspace() for char in channel_id):
        raise ValueError("Укажите непустой ID или адрес канала MAX без пробелов (до 256 символов)")
    library = _wincred()
    blob = json.dumps({"token": token, "channel_id": channel_id}, ensure_ascii=False).encode("utf-8")
    if len(blob) > MAX_CREDENTIAL_BLOB_SIZE:
        raise ValueError("Набор MAX-учётных данных превышает ограничение Диспетчера учётных данных Windows")
    buffer = ctypes.create_string_buffer(blob)
    credential = _CredentialW(
        Flags=0,
        Type=CRED_TYPE_GENERIC,
        TargetName=CREDENTIAL_TARGET,
        Comment="Учётные данные API канала MAX для приложения «Сборщик информации»",
        LastWritten=wintypes.FILETIME(0, 0),
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName="MAX channel publisher",
    )
    if not library.CredWriteW(ctypes.byref(credential), 0):
        error = ctypes.get_last_error()
        raise OSError(error, "Не удалось сохранить MAX-учётные данные в Диспетчере учётных данных Windows")


def remove_max_credentials() -> bool:
    """Delete the app's MAX credential entry for the current Windows user."""
    library = _wincred()
    if library.CredDeleteW(CREDENTIAL_TARGET, CRED_TYPE_GENERIC, 0):
        return True
    error = ctypes.get_last_error()
    if error == ERROR_NOT_FOUND:
        return False
    raise OSError(error, "Не удалось удалить MAX-учётные данные из Диспетчера учётных данных Windows")


def get_max_credentials() -> tuple[str | None, str | None]:
    """Read MAX values from environment first, then Windows Credential Manager."""
    token = os.environ.get("MAX_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("MAX_CHANNEL_ID", "").strip()
    if token and channel_id:
        return token, channel_id
    stored = _read_stored_credentials()
    return token or stored.get("token"), channel_id or stored.get("channel_id")
