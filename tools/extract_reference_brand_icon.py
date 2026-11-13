"""Extract the supplied reference EXE's genuine icon without executing it."""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "LeanWatts-EoL-0.1.0-win64 (1)/LeanWatts EoL/LeanWatts EoL.exe"
ASSETS = ROOT / "leanwatts_can/assets"
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
kernel.LoadLibraryExW.restype = wintypes.HMODULE
kernel.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
kernel.FindResourceW.restype = wintypes.HANDLE
kernel.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
kernel.LoadResource.restype = wintypes.HANDLE
kernel.LockResource.argtypes = [wintypes.HANDLE]
kernel.LockResource.restype = ctypes.c_void_p
kernel.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HANDLE]
kernel.SizeofResource.restype = wintypes.DWORD
kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
kernel.FreeLibrary.restype = wintypes.BOOL
Callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMODULE, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p)
kernel.EnumResourceNamesW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, Callback, ctypes.c_void_p]
kernel.EnumResourceNamesW.restype = wintypes.BOOL

handle = kernel.LoadLibraryExW(str(SOURCE), None, 0x2 | 0x20)
if not handle:
    raise ctypes.WinError(ctypes.get_last_error())

def resource(name, kind):
    name_buffer = ctypes.create_unicode_buffer(name) if isinstance(name, str) else None
    pointer = ctypes.cast(name_buffer, ctypes.c_void_p) if name_buffer is not None else ctypes.c_void_p(name)
    found = kernel.FindResourceW(handle, pointer, ctypes.c_void_p(kind))
    if not found:
        raise ctypes.WinError(ctypes.get_last_error())
    size = kernel.SizeofResource(handle, found)
    memory = kernel.LockResource(kernel.LoadResource(handle, found))
    return ctypes.string_at(memory, size)

try:
    names = []
    @Callback
    def collect(module, kind, name, parameter):
        names.append(int(name) if int(name) <= 65535 else ctypes.wstring_at(name))
        return True
    if not kernel.EnumResourceNamesW(handle, ctypes.c_void_p(14), collect, None):
        raise ctypes.WinError(ctypes.get_last_error())
    if len(names) != 1:
        raise ValueError(f"Expected one branded icon group, found {names!r}.")
    group = resource(names[0], 14)
    reserved, kind, count = struct.unpack_from("<HHH", group)
    if reserved != 0 or kind != 1 or len(group) != 6 + 14 * count:
        raise ValueError("Unexpected icon resource format.")
    payloads, entries, sizes = [], [], []
    offset = 6 + 16 * count
    for index in range(count):
        width, height, colors, zero, planes, bits, expected_size, icon_id = struct.unpack_from("<BBBBHHIH", group, 6 + 14 * index)
        data = resource(icon_id, 3)
        if len(data) != expected_size:
            raise ValueError("Icon resource size differs from its directory entry.")
        entries.append(struct.pack("<BBBBHHII", width, height, colors, zero, planes, bits, len(data), offset))
        payloads.append(data)
        sizes.append([width or 256, height or 256])
        offset += len(data)
    ASSETS.mkdir(exist_ok=True)
    ico = struct.pack("<HHH", 0, 1, count) + b"".join(entries) + b"".join(payloads)
    (ASSETS / "leanwatts.ico").write_bytes(ico)
finally:
    kernel.FreeLibrary(handle)

largest = max(range(len(sizes)), key=lambda i: sizes[i][0] * sizes[i][1])
if payloads[largest].startswith(b"\x89PNG\r\n\x1a\n"):
    # Preserve the original largest PNG resource exactly when available.
    png = payloads[largest]
    (ASSETS / "leanwatts-symbol.png").write_bytes(png)
else:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication, QIcon
    app = QGuiApplication([])
    pixmap = QIcon(str(ASSETS / "leanwatts.ico")).pixmap(QSize(*sizes[largest]))
    if pixmap.isNull() or not pixmap.save(str(ASSETS / "leanwatts-symbol.png")):
        raise ValueError("The original icon could not be decoded as PNG.")
    png = (ASSETS / "leanwatts-symbol.png").read_bytes()

print(json.dumps({"source": str(SOURCE), "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
    "group": names[0], "sizes": sizes, "ico_sha256": hashlib.sha256(ico).hexdigest(),
    "png_sha256": hashlib.sha256(png).hexdigest(), "png_size": sizes[largest],
    "source_executed": False}, indent=2))
