"""Static guard for the safety requirements in the spec (section 2 and 14).

Scans the AST, so names in comments and docstrings do not trigger false positives,
but identifiers, attribute accesses, imports and exact string constants do.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "evewatch"

FORBIDDEN_NAMES = {
    "OpenProcess",
    "ReadProcessMemory",
    "WriteProcessMemory",
    "SendInput",
    "keybd_event",
    "mouse_event",
    "SetWindowsHookEx",
    "SetWindowsHookExA",
    "SetWindowsHookExW",
    "PostMessage",
    "PostMessageA",
    "PostMessageW",
    "SendMessage",
    "SendMessageA",
    "SendMessageW",
    "FindWindow",
    "FindWindowA",
    "FindWindowW",
    "FindWindowEx",
    "SetClipboardViewer",
    "AddClipboardFormatListener",
    "SetWinEventHook",
    "RegisterHotKey",
    "GetAsyncKeyState",
    "GetKeyboardState",
    "RegisterRawInputDevices",
    "PostThreadMessage",
    "SendMessageTimeout",
    "CreateRemoteThread",
    "VirtualAllocEx",
    "VirtualProtectEx",
    "NtReadVirtualMemory",
    "NtWriteVirtualMemory",
    "subprocess",
    "open_connection",
    "create_connection",
}
FORBIDDEN_MODULES = {"keyboard", "mouse", "pynput", "pyautogui", "pydirectinput"}
NETWORK_MODULES = {
    "socket",
    "ssl",
    "urllib",
    "http",
    "ftplib",
    "smtplib",
    "requests",
    "httpx",
    "aiohttp",
    "websocket",
    "websockets",
    "socketserver",
    "xmlrpc",
    "telnetlib",
    "imaplib",
    "poplib",
    "ws2_32",
    "wininet",
    "winhttp",
}
NETWORK_ALLOWED_FILES = {"kills.py", "universe.py", "types_db.py"}


def find_violations(source: str, filename: str = "<src>") -> list[str]:
    found = []
    tree = ast.parse(source, filename)
    for node in ast.walk(tree):
        names: list[str] = []
        modules: list[str] = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.append(node.value)
            modules.append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.alias):
            names.append(node.name.split(".")[-1])
            modules.append(node.name)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
            names.extend(a.name for a in node.names)
        for name in names:
            if name in FORBIDDEN_NAMES:
                found.append(f"{filename}:{getattr(node, 'lineno', '?')}: forbidden API {name}")
        for module in modules:
            top = module.split(".")[0]
            if top in FORBIDDEN_MODULES:
                found.append(f"{filename}:{getattr(node, 'lineno', '?')}: forbidden module {top}")
            if top in NETWORK_MODULES and Path(filename).name not in NETWORK_ALLOWED_FILES:
                found.append(f"{filename}:{getattr(node, 'lineno', '?')}: network module {top} outside allowed files")
    return found


def test_package_has_no_forbidden_apis():
    files = sorted(PACKAGE.rglob("*.py"))
    assert files, "package not found"
    violations = []
    for path in files:
        violations += find_violations(path.read_text(encoding="utf-8"), str(path.relative_to(PACKAGE.parent)))
    assert not violations, "\n".join(violations)


def test_scanner_detects_violations():
    assert find_violations("import ctypes\nctypes.windll.user32.SendInput(1)")
    assert find_violations("from ctypes import windll\nwindll.kernel32.OpenProcess(0, 0, 1)")
    assert find_violations("import keyboard")
    assert find_violations("from pynput import mouse")
    assert find_violations("getattr(u, 'SetWindowsHookEx')")
    assert find_violations("import socket")
    assert find_violations("import urllib.request")
    assert find_violations("def FindWindow(): pass")
    assert find_violations("import importlib\nimportlib.import_module('socket')")
    assert find_violations("import asyncio\nasyncio.open_connection('h', 1)")
    assert find_violations("import ctypes\nctypes.WinDLL('ws2_32')")
    assert find_violations("import ctypes\nctypes.windll.user32.RegisterHotKey(0, 1, 0, 0)")
    assert find_violations("import subprocess")


def test_scanner_ignores_comments_and_docstrings():
    assert not find_violations('"""Never call SendInput or OpenProcess."""\n# FindWindow is forbidden\nx = 1')


def test_network_modules_allowed_only_in_data_modules():
    assert not find_violations("import urllib.request", "universe.py")
    assert find_violations("import urllib.request", "app.py")
