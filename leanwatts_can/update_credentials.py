"""Current-user Windows DPAPI storage for the private release download token."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import json
from pathlib import Path
import re


TOKEN_FILE = "update-github-token.dpapi"
AUTH_FILE = "update-github-auth.dpapi"


def _validate_token(token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]{20,512}", token):
        raise ValueError("Enter a valid GitHub read-only access token in the password field.")
    return token


def _crypt(data, *, protect):
    if os.name != "nt":
        raise RuntimeError("Private update credentials use Windows account encryption.")

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    protect_fn = crypt32.CryptProtectData
    protect_fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    protect_fn.restype = wintypes.BOOL
    unprotect_fn = crypt32.CryptUnprotectData
    unprotect_fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    unprotect_fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    try:
        if protect:
            ok = protect_fn(ctypes.byref(incoming), "Leanwatts Testbench Gen2 private updates",
                            None, None, None, 1, ctypes.byref(outgoing))
        else:
            ok = unprotect_fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
        if not ok:
            raise RuntimeError("Windows could not " + ("save" if protect else "unlock")
                               + " the update credential. Enter it again on this Windows account.")
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.memset(buffer, 0, len(data))
        if outgoing.pbData:
            ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
            kernel32.LocalFree(outgoing.pbData)


def has_token(data_dir):
    return (Path(data_dir) / TOKEN_FILE).is_file()


def save_token(data_dir, token):
    token = _validate_token(token.strip())
    encrypted = _crypt(token.encode("ascii"), protect=True)
    path = Path(data_dir) / TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    os.replace(temporary, path)


def load_token(data_dir):
    path = Path(data_dir) / TOKEN_FILE
    if not path.is_file():
        raise ValueError("Enter and save the private GitHub read-only token on this laptop first.")
    if path.stat().st_size > 16384:
        raise ValueError("The encrypted update credential is invalid. Save it again.")
    try:
        return _validate_token(_crypt(path.read_bytes(), protect=False).decode("ascii"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("The encrypted update credential is invalid. Save it again.") from exc


def remove_token(data_dir):
    path = Path(data_dir) / TOKEN_FILE
    if path.is_file():
        path.unlink()


def has_auth(data_dir):
    return (Path(data_dir) / AUTH_FILE).is_file()


def save_auth(data_dir, value):
    """Atomically replace the complete encrypted access/refresh-token pair."""
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("The GitHub sign-in record is invalid.")
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 16384:
        raise ValueError("The GitHub sign-in record is invalid.")
    encrypted = _crypt(encoded, protect=True)
    path = Path(data_dir) / AUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    os.replace(temporary, path)


def load_auth(data_dir):
    path = Path(data_dir) / AUTH_FILE
    if not path.is_file():
        raise ValueError("Sign in to GitHub on this laptop first.")
    if path.stat().st_size > 32768:
        raise ValueError("The saved GitHub sign-in is invalid. Sign in again.")
    try:
        value = json.loads(_crypt(path.read_bytes(), protect=False).decode("utf-8"))
        if not isinstance(value, dict) or value.get("schema") != 1:
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise ValueError("The saved GitHub sign-in is invalid. Sign in again.") from None


def remove_auth(data_dir):
    path = Path(data_dir) / AUTH_FILE
    if path.is_file():
        path.unlink()
