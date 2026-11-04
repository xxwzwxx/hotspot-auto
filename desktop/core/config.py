"""配置读写：campus-config.json（账号设置）与 settings.json（本程序私有）。

设计要点：
  * 写回 campus-config.json 时保留磁盘上已有的未知字段，避免手工加的自定义键被吃掉；
  * 先写临时文件再 os.replace，避免读到半截 JSON；
  * 打包成 exe 之后，所有路径都落在 exe 所在目录，而不是源码目录。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path

_LOCK = threading.RLock()


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _project_dir() -> Path:
    override = os.environ.get("HOTSPOT_AUTO_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


PROJECT_DIR: Path = _project_dir()
DESKTOP_DIR: Path = Path(__file__).resolve().parents[1]
# exe 里资源被解包到临时目录（sys._MEIPASS），源码运行时就在 desktop/assets
if _frozen():
    ASSETS_DIR: Path = Path(getattr(sys, "_MEIPASS", PROJECT_DIR)) / "assets"
else:
    ASSETS_DIR = DESKTOP_DIR / "assets"

# exe 模式下配置、日志都跟着 exe 走；源码模式仍在项目根目录
APP_DIR: Path = PROJECT_DIR
CONFIG_PATH: Path = APP_DIR / "campus-config.json"
if not _frozen():
    SETTINGS_PATH: Path = DESKTOP_DIR / "settings.json"
else:
    SETTINGS_PATH = APP_DIR / "settings.json"

LOG_FILES = {
    "campus": APP_DIR / "campus.log",
    "hotspot": APP_DIR / "hotspot.log",
}

LOG_LABELS = {
    "campus": "campus.log（校园网登录）",
    "hotspot": "hotspot.log（移动热点）",
}

DEFAULT_CONFIG = {
    "portal": "http://10.10.10.10",
    "api_port": 801,
    "username": "",
    "password": "",
    "suffix": "",
    "ssid_filter": "",
    "wait_seconds": 300,
}

DEFAULT_SETTINGS = {
    "minimize_to_tray": True,
    "notify_on_finish": True,
    "autoscroll_log": True,
    "poll_interval": 5,
    "campus_timeout": 900,
    "hotspot_timeout": 300,
    "start_minimized": False,
    "auto_reconnect": True,
    "reconnect_interval": 20,
    "run_on_start": True,
    "resume_autostart": True,
}


def _read_json(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    with _LOCK:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)


# ---------------------------------------------------------------- campus 配置
def _migration_candidates(name: str) -> list[Path]:
    """exe 第一次运行时，可能还放在源码目录旁边；按常见位置找一遍。"""
    here = Path(sys.executable).resolve().parent if _frozen() else PROJECT_DIR
    return [
        here / name,
        here.parent / name,
        here / "desktop" / name,
        here.parent / "desktop" / name,
        Path.cwd() / name,
        Path.cwd() / "desktop" / name,
        Path(r"D:\APP\AI\www\hotspot-auto") / name,
        Path(r"D:\APP\AI\www\hotspot-auto\desktop") / name,
    ]


def ensure_runtime_files() -> None:
    """打包后首次启动：把账号配置和程序设置从老位置复制到 exe 目录，免去重填。"""
    for target, name in ((CONFIG_PATH, "campus-config.json"), (SETTINGS_PATH, "settings.json")):
        if target.exists():
            continue
        for source in _migration_candidates(name):
            try:
                if source.is_file() and source.resolve() != target.resolve():
                    shutil.copyfile(source, target)
                    break
            except OSError:
                continue


def load_config() -> dict:
    """磁盘配置 + 默认值合并，未知字段一起带回来。"""
    raw = _read_json(CONFIG_PATH)
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    merged["_extra"] = {k: v for k, v in raw.items() if k not in DEFAULT_CONFIG}
    merged["_exists"] = CONFIG_PATH.exists()
    return merged


def save_config(values: dict) -> dict:
    """保存可编辑字段，保留磁盘上的其他键；返回写入后的完整内容。"""
    raw = _read_json(CONFIG_PATH)
    for key in DEFAULT_CONFIG:
        if key in values:
            raw[key] = values[key]
    _write_json(CONFIG_PATH, raw)
    return load_config()


def normalize_config(values: dict) -> dict:
    """把界面输入收成脚本能吃的类型。"""
    portal = str(values.get("portal", "") or "").strip()
    if portal and not portal.lower().startswith(("http://", "https://")):
        portal = "http://" + portal
    portal = portal.rstrip("/")

    try:
        api_port = int(str(values.get("api_port", 801)).strip() or 801)
    except (TypeError, ValueError):
        api_port = 801
    if not 1 <= api_port <= 65535:
        api_port = 801

    try:
        wait_seconds = int(str(values.get("wait_seconds", 300)).strip() or 300)
    except (TypeError, ValueError):
        wait_seconds = 300
    wait_seconds = max(0, min(wait_seconds, 3600))

    return {
        "portal": portal or DEFAULT_CONFIG["portal"],
        "api_port": api_port,
        "username": str(values.get("username", "") or "").strip(),
        "password": str(values.get("password", "") or ""),
        "suffix": str(values.get("suffix", "") or "").strip(),
        "ssid_filter": str(values.get("ssid_filter", "") or "").strip(),
        "wait_seconds": wait_seconds,
    }


def is_configured(cfg: dict) -> bool:
    return bool(str(cfg.get("username", "")).strip() and str(cfg.get("password", "")).strip())


def api_base(cfg: dict) -> str:
    """Portal 主机 + API 端口，例如 http://10.10.10.10:801。"""
    portal = str(cfg.get("portal", "") or DEFAULT_CONFIG["portal"]).strip()
    if "://" not in portal:
        portal = "http://" + portal
    host = portal.split("://", 1)[1].split("/", 1)[0]
    host = host.split(":", 1)[0]
    port = cfg.get("api_port", 801)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 801
    return f"http://{host}:{port}"


# ---------------------------------------------------------------- 程序设置
def load_settings() -> dict:
    raw = _read_json(SETTINGS_PATH)
    merged = dict(DEFAULT_SETTINGS)
    for key, default in DEFAULT_SETTINGS.items():
        value = raw.get(key, default)
        merged[key] = value if isinstance(value, type(default)) else default
    merged["_extra"] = {k: v for k, v in raw.items() if k not in DEFAULT_SETTINGS}
    if not SETTINGS_PATH.exists():
        # 首次运行就把默认值落盘，方便用户直接照着文件结构改，也让设置页的说明有处可查
        try:
            _write_json(SETTINGS_PATH, dict(DEFAULT_SETTINGS))
        except OSError:
            pass
    return merged


def save_settings(values: dict) -> dict:
    raw = _read_json(SETTINGS_PATH)
    for key in DEFAULT_SETTINGS:
        if key in values:
            raw[key] = values[key]
    _write_json(SETTINGS_PATH, raw)
    return load_settings()


def settings_public(values: dict) -> dict:
    return {k: v for k, v in values.items() if k in DEFAULT_SETTINGS}
