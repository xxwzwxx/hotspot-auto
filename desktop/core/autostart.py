"""开机自启：写 HKCU\\...\\Run，不需要管理员。

源码运行时启动 pythonw app.py；打包成 exe 后直接启动 exe 自己（不必再找解释器）。
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "HotspotAutoManager"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_entry() -> Path:
    if _frozen():
        return Path(sys.executable).resolve()
    return Path(__file__).resolve().parents[1] / "app.py"


def _launch_command(entry: Path | None = None) -> str:
    if _frozen():
        target = Path(sys.executable).resolve()
        return f'"{target}" --minimized'
    entry = entry or app_entry()
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else exe
    return f'"{target}" "{entry}" --minimized'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def current_command() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except OSError:
        return ""


def enable(entry: Path | None = None) -> bool:
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _launch_command(entry))
        return True
    except OSError:
        return False


def disable() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False
