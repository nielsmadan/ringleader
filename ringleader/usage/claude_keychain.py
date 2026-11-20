from __future__ import annotations

import ctypes
import os
import pwd
import sys
import time
from collections.abc import Mapping
from contextlib import ExitStack
from typing import Any

from . import remote_http
from .provider_environment import CLAUDE_PROFILE_OVERRIDES

PROFILE_OVERRIDES = CLAUDE_PROFILE_OVERRIDES
MAX_CREDENTIAL_BYTES = 65536
KEYCHAIN_SECONDS = 3.0
REQUEST = b"read-claude-code-credentials-v1"
KEYCHAIN_ERRORS = frozenset(
    {"keychain_missing", "keychain_denied", "keychain_ambiguous", "keychain_invalid"}
)
ERR_SEC_ITEM_NOT_FOUND = -25300
CF_NUMBER_INT = 9
CF_STRING_UTF8 = 0x08000100


class KeychainError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def native_profile_allowed(environment: Mapping[str, str], platform: str) -> bool:
    if platform != "darwin" or any(key in environment for key in PROFILE_OVERRIDES):
        return False
    try:
        return environment.get("HOME") == pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return False


def read_credentials() -> bytes:
    try:
        output = remote_http.run_worker(
            [sys.executable, "-I", "-m", "ringleader.usage.claude_keychain"],
            REQUEST,
            deadline=time.monotonic() + KEYCHAIN_SECONDS,
            max_output=MAX_CREDENTIAL_BYTES + 1,
        )
    except remote_http.RemoteHttpError as exc:
        code = "keychain_timeout" if exc.code == "http_timeout" else "keychain_denied"
        raise KeychainError(code) from None
    if output.startswith(b"\x00") and len(output) > 1:
        return output[1:]
    code = output.decode("ascii", errors="replace")
    raise KeychainError(code if code in KEYCHAIN_ERRORS else "keychain_invalid")


def _load_frameworks() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    base = "/System/Library/Frameworks"
    return (
        ctypes.CDLL(f"{base}/Security.framework/Security"),
        ctypes.CDLL(f"{base}/CoreFoundation.framework/CoreFoundation"),
    )


def _configure(security: ctypes.CDLL, core: ctypes.CDLL) -> None:
    ptr = ctypes.c_void_p
    ptr_ref = ctypes.POINTER(ptr)
    index = ctypes.c_long
    type_id = ctypes.c_ulong
    signatures: tuple[tuple[Any, list[object], object], ...] = (
        (security.SecKeychainSetUserInteractionAllowed, [ctypes.c_ubyte], ctypes.c_int32),
        (security.SecItemCopyMatching, [ptr, ptr_ref], ctypes.c_int32),
        (core.CFStringCreateWithCString, [ptr, ctypes.c_char_p, ctypes.c_uint32], ptr),
        (core.CFNumberCreate, [ptr, ctypes.c_int, ptr], ptr),
        (core.CFDictionaryCreate, [ptr, ptr_ref, ptr_ref, index, ptr, ptr], ptr),
        (core.CFGetTypeID, [ptr], type_id),
        (core.CFArrayGetTypeID, [], type_id),
        (core.CFArrayGetCount, [ptr], index),
        (core.CFArrayGetValueAtIndex, [ptr, index], ptr),
        (core.CFDataGetTypeID, [], type_id),
        (core.CFDataGetLength, [ptr], index),
        (core.CFDataGetBytePtr, [ptr], ptr),
        (core.CFRelease, [ptr], None),
    )
    for function, args, result in signatures:
        function.argtypes = args
        function.restype = result


def _constant(library: ctypes.CDLL, name: str) -> int:
    value = ctypes.c_void_p.in_dll(library, name).value
    if value is None:
        raise KeychainError("keychain_invalid")
    return value


def _retained(core: ctypes.CDLL, stack: ExitStack, value: int | None) -> int:
    if not value:
        raise KeychainError("keychain_invalid")
    stack.callback(core.CFRelease, value)
    return value


def _single_data(core: ctypes.CDLL, array: int) -> bytes:
    if core.CFGetTypeID(array) != core.CFArrayGetTypeID():
        raise KeychainError("keychain_invalid")
    count = core.CFArrayGetCount(array)
    if count != 1:
        raise KeychainError("keychain_ambiguous" if count > 1 else "keychain_missing")
    data = core.CFArrayGetValueAtIndex(array, 0)
    if not data or core.CFGetTypeID(data) != core.CFDataGetTypeID():
        raise KeychainError("keychain_invalid")
    size = core.CFDataGetLength(data)
    address = core.CFDataGetBytePtr(data)
    if not address or not 0 < size <= MAX_CREDENTIAL_BYTES:
        raise KeychainError("keychain_invalid")
    return ctypes.string_at(address, size)


def _copy_matching(security: ctypes.CDLL, core: ctypes.CDLL) -> bytes:
    _configure(security, core)
    if security.SecKeychainSetUserInteractionAllowed(0) != 0:
        raise KeychainError("keychain_denied")
    with ExitStack() as stack:
        service = _retained(
            core,
            stack,
            core.CFStringCreateWithCString(None, b"Claude Code-credentials", CF_STRING_UTF8),
        )
        limit = _retained(
            core, stack, core.CFNumberCreate(None, CF_NUMBER_INT, ctypes.byref(ctypes.c_int(2)))
        )
        fields = {
            "kSecClass": _constant(security, "kSecClassGenericPassword"),
            "kSecAttrService": service,
            "kSecUseAuthenticationUI": _constant(security, "kSecUseAuthenticationUIFail"),
            "kSecMatchLimit": limit,
            "kSecReturnData": _constant(core, "kCFBooleanTrue"),
        }
        pointers = ctypes.c_void_p * len(fields)
        query = _retained(
            core,
            stack,
            core.CFDictionaryCreate(
                None,
                pointers(*(_constant(security, name) for name in fields)),
                pointers(*fields.values()),
                len(fields),
                None,
                None,
            ),
        )
        result = ctypes.c_void_p()
        status = security.SecItemCopyMatching(query, ctypes.byref(result))
        if result.value:
            stack.callback(core.CFRelease, result.value)
        if status == ERR_SEC_ITEM_NOT_FOUND:
            raise KeychainError("keychain_missing")
        if status:
            raise KeychainError("keychain_denied")
        if result.value is None:
            raise KeychainError("keychain_invalid")
        return _single_data(core, result.value)


def main() -> int:
    if sys.stdin.buffer.read(len(REQUEST) + 1) != REQUEST:
        return 2
    if not native_profile_allowed(os.environ, sys.platform):
        return 2
    try:
        output = b"\x00" + _copy_matching(*_load_frameworks())
    except KeychainError as exc:
        output = exc.code.encode("ascii")
    except (AttributeError, OSError, ValueError):
        output = b"keychain_denied"
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
