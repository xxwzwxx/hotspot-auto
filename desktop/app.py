"""热点自动助手 —— 桌面管理器主程序。

一个窗口搞定全部：编辑账号配置、执行登录与开热点（原生 Python 实现，不依赖外部脚本）、
展示实时日志与在线状态、托盘常驻、开机自启、断网自动重连。

运行：python app.py           调试时带控制台
      pythonw app.py          无控制台
      pythonw app.py --minimized   启动后直接缩到托盘
"""

from __future__ import annotations

import collections
import ctypes
import ctypes.wintypes as wintypes
import gc
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from tkinter import font as tkfont
from tkinter import messagebox

DESKTOP_DIR = Path(__file__).resolve().parent
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

from core import actions, autostart, config, notify, probe, runner, tray  # noqa: E402
import ui  # noqa: E402

APP_NAME = "热点自动助手"
APP_VERSION = "1.0.0"

# ------------------------------------------------------------------ 配色与字体
BG = ui.BG
SURFACE = ui.SURFACE
CARD = ui.CARD
FIELD = ui.FIELD
BORDER = ui.CARD_BORDER
FIELD_BORDER = ui.FIELD_BORDER
TEXT = ui.TEXT
TEXT_SOFT = ui.TEXT_SOFT
SUBTLE = ui.SUBTLE
FAINT = ui.FAINT
ACCENT = ui.ACCENT
ACCENT_HOVER = ui.ACCENT_HOVER
ACCENT_SOFT = ui.ACCENT_SOFT
OK = ui.OK
WARN = ui.WARN
ERR = ui.ERR
IDLE = ui.IDLE

LIVE_SOURCE = "实时执行"

# 日志最多留多少行：界面控件和内存缓冲同步裁剪，常驻一整天也不会慢慢涨
MAX_LOG_LINES = 1200
# 空闲多久之后把工作集还给系统（秒），以及两次整理之间的最小间隔
IDLE_TRIM_SECONDS = 90
IDLE_TRIM_INTERVAL = 300
# 收在托盘里（窗口不可见）时多久整理一次
HIDDEN_TRIM_INTERVAL = 60


# ------------------------------------------------------------------ 基础设施
def apply_dpi_awareness() -> float:
    """声明系统级 DPI 感知，返回 96 DPI 为 1.0 的缩放系数。"""
    user32 = ctypes.windll.user32
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - 老系统没有 shcore
        try:
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    dpi = 96
    try:
        dpi = int(user32.GetDpiForSystem())
    except Exception:  # noqa: BLE001
        try:
            dc = user32.GetDC(0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(dc, 88))
            user32.ReleaseDC(0, dc)
        except Exception:  # noqa: BLE001
            dpi = 96
    if not 72 <= dpi <= 480:
        dpi = 96
    return max(1.0, min(dpi / 96.0, 3.0))


def freeze_heap() -> None:
    """界面建好后把已有对象移出 GC 分代。

    常驻托盘程序里，启动期创建的控件都活到最后，没必要被反复扫描；
    冻结之后每轮回收只扫新产生的小对象，老年代扫描的固定开销基本消失。
    """
    try:
        gc.collect()
        gc.freeze()
    except Exception:  # noqa: BLE001 - Python < 3.7 没有 freeze
        pass


def trim_working_set() -> bool:
    """把闲置进程的工作集还给系统。

    私有内存（提交量）不变，只是把暂时用不到的物理页交回给系统的可用内存池，
    任务管理器「内存」一栏会明显下降；再次点开窗口时按需换入，不影响功能。
    """
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet.restype = wintypes.BOOL
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        return bool(psapi.EmptyWorkingSet(kernel32.GetCurrentProcess()))
    except Exception:  # noqa: BLE001 - 拿不到就当作没整理，不影响主流程
        return False

class SingleInstance:
    """用本地端口做单实例锁；第二次启动时把已有窗口叫出来。"""

    PORT = 47820

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def acquire(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.PORT))
            sock.listen(2)
        except OSError:
            sock.close()
            return False
        self._sock = sock
        return True

    def notify_existing(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.PORT), timeout=1.5) as conn:
                conn.sendall(b"show")
            return True
        except OSError:
            return False

    def listen(self, callback) -> None:
        if not self._sock:
            return

        def worker() -> None:
            while True:
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    return
                try:
                    data = conn.recv(16)
                except OSError:
                    data = b""
                finally:
                    conn.close()
                if data.startswith(b"show"):
                    callback()

        self._thread = threading.Thread(target=worker, daemon=True, name="single-instance")
        self._thread.start()

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class TabView:
    """自绘页签栏：扁平标签 + 底部高亮线，避免 ttk 主题在不同 DPI 下的错位。"""

    def __init__(self, parent, app: "App") -> None:
        self.app = app
        self.outer = tk.Frame(parent, bg=BG)
        self.outer.pack(fill="both", expand=True, padx=app.s(22), pady=(0, app.s(10)))
        self.bar = tk.Frame(self.outer, bg=BG)
        self.bar.pack(fill="x")
        self.rule = tk.Frame(self.outer, bg=BORDER, height=app.s(1))
        self.rule.pack(fill="x")
        self.body = tk.Frame(self.outer, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.entries: list[dict] = []
        self.current: tk.Frame | None = None

    def add(self, page: tk.Frame, text: str) -> None:
        app = self.app
        holder = tk.Frame(self.bar, bg=BG)
        holder.pack(side="left")
        label = tk.Label(
            holder,
            text=text,
            font=app.f_tab,
            bg=BG,
            fg=SUBTLE,
            padx=app.s(18),
            pady=app.s(10),
            cursor="hand2",
        )
        label.pack()
        accent = tk.Frame(holder, bg=BG, height=max(2, app.s(2)))
        accent.pack(fill="x", padx=app.s(10))
        entry = {"page": page, "holder": holder, "label": label, "accent": accent}
        self.entries.append(entry)
        label.bind("<Button-1>", lambda _event, item=entry: self.select(item["page"]))
        label.bind("<Enter>", lambda _event, item=entry: self._hover(item, True))
        label.bind("<Leave>", lambda _event, item=entry: self._hover(item, False))

    def _hover(self, entry: dict, inside: bool) -> None:
        if entry is self.current:
            return
        entry["label"].configure(fg=TEXT_SOFT if inside else SUBTLE)

    def select(self, page) -> None:
        if isinstance(page, int):
            page = self.entries[page]["page"]
        target = None
        for entry in self.entries:
            if entry["page"] is page:
                target = entry
                break
        if target is None or target is self.current:
            return
        if self.current is not None:
            self.current["page"].pack_forget()
            self.current["label"].configure(fg=SUBTLE, font=self.app.f_tab)
            self.current["accent"].configure(bg=BG)
        target["page"].pack(fill="both", expand=True)
        target["label"].configure(fg=ACCENT, font=self.app.f_tab_bold)
        target["accent"].configure(bg=ACCENT)
        self.current = target


class ToggleBox(tk.Frame):
    """自绘勾选框：Canvas 方块 + 文字，整行可点。"""

    def __init__(self, parent, app: "App", text: str, variable, command=None, background=None, font=None):
        background = background or CARD
        super().__init__(parent, bg=background)
        self.app = app
        self.var = variable
        self.command = command
        self.size = app.s(15)
        self.canvas = tk.Canvas(
            self, width=self.size, height=self.size, bg=background, highlightthickness=0, bd=0, cursor="hand2"
        )
        self.canvas.pack(side="left", pady=(app.s(2), 0))
        self.label = tk.Label(
            self,
            text=text,
            font=font or app.f_body,
            bg=background,
            fg=TEXT,
            cursor="hand2",
        )
        self.label.pack(side="left", padx=(app.s(8), 0))
        for widget in (self.canvas, self.label):
            widget.bind("<Button-1>", self._toggle)
        self.var.trace_add("write", lambda *_args: self._draw())
        self._draw()

    def _draw(self) -> None:
        size = self.size
        on = bool(self.var.get())
        line = max(1.5, self.app.s(1.2))
        self.canvas.delete("all")
        self.canvas.create_polygon(
            ui.rounded_points(line, line, size - line, size - line, size * 0.24, steps=4),
            fill=ACCENT if on else SURFACE,
            outline=ACCENT if on else "#CBD5E1",
            width=line,
        )
        if on:
            self.canvas.create_line(
                size * 0.26,
                size * 0.52,
                size * 0.43,
                size * 0.69,
                size * 0.75,
                size * 0.32,
                fill="#FFFFFF",
                width=max(1.6, self.app.s(1.6)),
                capstyle="round",
                joinstyle="round",
            )

    def _toggle(self, _event=None) -> None:
        self.var.set(not self.var.get())
        if self.command is not None:
            self.command()


class ScrollPage(tk.Frame):
    """竖向滚动的页面容器：内容比窗口高时右边才出现滚动条。

    tkinter 没有现成的滚动 Frame，只能拿 Canvas 包一层；滚轮要绑到每个子控件上，
    否则指针停在文字或输入框上时滚不动。
    """

    def __init__(self, parent, app: "App") -> None:
        super().__init__(parent, bg=BG)
        self.app = app
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.bar = ui.ThinScrollbar(self, command=self.canvas.yview, scale=app.scale, parent_bg=BG)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.body.after(200, self.bind_wheel)

    def _on_body(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._win, width=event.width)

    def _on_scroll(self, first: str, last: str) -> None:
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.bar.pack_forget()
        else:
            self.bar.pack(side="right", fill="y", before=self.canvas)
        self.bar.set(first, last)

    def bind_wheel(self, widget=None) -> None:
        widget = widget if widget is not None else self.body
        widget.bind("<MouseWheel>", self._wheel, add="+")
        for child in widget.winfo_children():
            self.bind_wheel(child)

    def _wheel(self, event):
        if self.body.winfo_height() <= self.canvas.winfo_height():
            return "break"
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"


# ------------------------------------------------------------------ 主程序
class App:
    def __init__(self, root: tk.Tk, scale: float = 1.0, start_minimized: bool = False) -> None:
        self.root = root
        self.scale = scale
        self.cfg = config.load_config()
        self.settings = config.load_settings()
        self.queue: queue.Queue = queue.Queue()
        self.runner = runner.ScriptRunner(self.queue.put, refresh=self._request_refresh)
        self.log_lines: collections.deque[str] = collections.deque(maxlen=MAX_LOG_LINES)
        self.recent_lines: collections.deque[str] = collections.deque(maxlen=6)
        self.last_snap: dict | None = None
        # 空闲整理内存用：最后一次界面活动的时间，以及上次整理的时间
        self._last_activity = time.time()
        self._last_trim = 0.0
        # 收在托盘时暂存的最新快照，窗口叫出来时补渲染
        self._pending_snap: dict | None = None
        # 校园 Portal 的在线列表接口会间歇性不回包，这里缓存最近一次查到的结果，
        # 查询超时时继续显示它（并标注是多久前的结果），避免卡片来回跳。
        self._online_known: dict | None = None
        # 上一次的热点开关状态，用来在日志里记录"开了/关了"的变化
        self._last_hotspot_on: bool | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._busy = False
        # 睡眠/休眠会让定时器整体停摆。下面三个时间戳用来在唤醒后补一次检测：
        # _last_tick 是事件泵上次跑的时间，_last_snap_ts 是最近一次拿到状态的时间，
        # _poll_gen 让旧轮询线程在重启后自己退出，避免两个线程同时探测。
        self._last_tick = time.time()
        self._last_snap_ts = time.time()
        self._last_resume_at = 0.0
        self._resume_started_at = 0.0
        self._resume_wait = 0
        self._closing = False
        self._poll_gen = 0
        self._poller: threading.Thread | None = None
        # 断网自动重连看门狗的状态。_auto_ready_at 是下一次允许尝试的时间戳，
        # _auto_attempts 用来做指数退避，避免一直拨号把校园网账号打爆。
        self._auto_ready_at = 0.0
        self._auto_attempts = 0
        # 当前这轮流程是谁发起的：manual（手动点）/ reconnect（断网看门狗）/ startup（启动自动跑）
        self._origin = "manual"
        self._auto_just_ok = False
        self._offline_seen = False
        self._tray: tray.TrayIcon | None = None
        self._tray_ready = False
        self._instance: SingleInstance | None = None
        self._poller: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self.cfg_vars: dict[str, tk.StringVar] = {}

        self._build_fonts()
        self._build_ui()
        self._load_config_form()
        self._load_settings_form()
        self._start_tray()
        self._start_poller()
        self._start_watchdog()
        self._pump()
        self.root.after(200, self.refresh_now)
        self.root.after(1500, self._maybe_autorun)

        if start_minimized:
            if self._tray_ready:
                self.root.withdraw()
            else:
                self.root.iconify()

    # -------------------------------------------------------------- 小工具
    def s(self, px: float) -> int:
        return max(1, int(round(px * self.scale)))

    def _build_fonts(self) -> None:
        families = set(tkfont.families(self.root))
        family = "Segoe UI"
        for name in ("Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei", "PingFang SC"):
            if name in families:
                family = name
                break
        mono = "Consolas" if "Consolas" in families else family
        self.f_title = (family, 16, "bold")
        self.f_subtitle = (family, 9)
        self.f_tab = (family, 10)
        self.f_tab_bold = (family, 10, "bold")
        self.f_button = (family, 10)
        self.f_button_primary = (family, 11, "bold")
        self.f_body = (family, 10)
        self.f_body_bold = (family, 10, "bold")
        self.f_section = (family, 11, "bold")
        self.f_label = (family, 9)
        self.f_card_title = (family, 9)
        self.f_value = (family, 13, "bold")
        self.f_hint = (family, 8)
        self.f_pill = (family, 8)
        self.f_dot = (family, 9)
        self.f_mono = (mono, 9)

    def _init_ttk_styles(self) -> None:
        """把 ttk 的下拉框/微调框调成和自绘控件一致的扁平风格。"""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            return
        pad = self.s(4)
        style.configure(
            "TCombobox",
            fieldbackground=FIELD,
            background=FIELD,
            foreground=TEXT,
            bordercolor=FIELD_BORDER,
            lightcolor=FIELD_BORDER,
            darkcolor=FIELD_BORDER,
            arrowcolor=SUBTLE,
            arrowsize=self.s(11),
            padding=pad,
            relief="flat",
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", FIELD)],
            background=[("readonly", FIELD), ("active", FIELD)],
            bordercolor=[("focus", ACCENT), ("hover", FAINT)],
            arrowcolor=[("hover", TEXT_SOFT)],
            selectbackground=[("readonly", FIELD)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=FIELD,
            background=FIELD,
            foreground=TEXT,
            bordercolor=FIELD_BORDER,
            lightcolor=FIELD_BORDER,
            darkcolor=FIELD_BORDER,
            arrowcolor=SUBTLE,
            arrowsize=self.s(9),
            padding=self.s(3),
            relief="flat",
        )
        style.map(
            "TSpinbox",
            fieldbackground=[("readonly", FIELD)],
            bordercolor=[("focus", ACCENT), ("hover", FAINT)],
            arrowcolor=[("hover", TEXT_SOFT), ("disabled", FAINT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", SURFACE)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", ACCENT)

    def _button(self, parent, text: str, command, kind: str = "secondary", font=None, height: float = 36):
        return ui.RoundedButton(
            parent,
            text,
            command,
            kind=kind,
            font=font or self.f_button,
            scale=self.scale,
            height=height,
            parent_bg=ui._parent_bg(parent, BG),
        )

    # -------------------------------------------------------------- 窗口骨架
    def _apply_geometry(self) -> None:
        width, height = self.s(1040), self.s(720)
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = min(width, screen_w - self.s(40))
        height = min(height, screen_h - self.s(90))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(self.s(900), screen_w - self.s(40)), min(self.s(620), screen_h - self.s(90)))

    def _build_ui(self) -> None:
        root = self.root
        root.title(f"{APP_NAME} {APP_VERSION}")
        root.configure(bg=BG)
        self._init_ttk_styles()
        try:
            root.iconbitmap(str(config.ASSETS_DIR / "app.ico"))
        except tk.TclError:
            pass
        self._apply_geometry()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 用任务栏还原窗口时 Tk 不会走 show_window()，这里兜住积攒的快照
        root.bind("<Map>", self._on_mapped, add="+")
        # 用户一动就重置空闲计时，避免正看着界面时被整理内存拖慢
        for sequence in ("<Button>", "<Key>", "<MouseWheel>"):
            try:
                root.bind_all(sequence, self._touch_activity, add="+")
            except tk.TclError:
                pass

        shell = tk.Frame(root, bg=BG)
        shell.pack(fill="both", expand=True)

        self._build_header(shell)
        self._build_actionbar(shell)
        self._build_statusbar(shell)
        self._build_notebook(shell)

    def _brand_badge(self, parent, size: float = 34) -> tk.Canvas:
        """标题左边的小图标：圆角方块 + WiFi 弧线，和 exe 图标同一个视觉。"""
        px = max(20.0, size * self.scale)
        canvas = tk.Canvas(parent, width=px, height=px, bg=ui._parent_bg(parent, SURFACE),
                           highlightthickness=0, bd=0)
        canvas.create_polygon(ui.rounded_points(0, 0, px, px, px * 0.28), fill=ACCENT, outline=ACCENT)
        cx, cy = px * 0.5, px * 0.70
        for radius in (px * 0.30, px * 0.45, px * 0.60):
            canvas.create_arc(
                cx - radius, cy - radius, cx + radius, cy + radius,
                start=45, extent=90, style="arc", outline="#FFFFFF", width=max(1.4, px * 0.075),
            )
        dot = px * 0.055
        canvas.create_oval(cx - dot, cy - dot, cx + dot, cy + dot, fill="#FFFFFF", outline="#FFFFFF")
        return canvas

    def _build_header(self, parent) -> None:
        header = tk.Frame(parent, bg=SURFACE)
        header.pack(fill="x")
        inner = tk.Frame(header, bg=SURFACE)
        inner.pack(fill="x", padx=self.s(22), pady=self.s(15))

        brand = tk.Frame(inner, bg=SURFACE)
        brand.pack(side="left")
        self.badge = self._brand_badge(brand)
        self.badge.pack(side="left", padx=(0, self.s(11)))

        titles = tk.Frame(brand, bg=SURFACE)
        titles.pack(side="left")
        tk.Label(titles, text=APP_NAME, font=self.f_title, bg=SURFACE, fg=TEXT).pack(anchor="w")
        tk.Label(
            titles,
            text="校园网自动登录 · 移动热点一键开启 · 配置与日志集中管理",
            font=self.f_subtitle,
            bg=SURFACE,
            fg=SUBTLE,
        ).pack(anchor="w", pady=(self.s(3), 0))

        pills = tk.Frame(inner, bg=SURFACE)
        pills.pack(side="right")
        self.pills: dict[str, ui.Pill] = {}
        for key, label in (("net", "网络"), ("online", "校园网"), ("hotspot", "热点"), ("ssid", "Wi-Fi")):
            pill = ui.Pill(pills, label, scale=self.scale, font=self.f_pill, parent_bg=SURFACE)
            pill.pack(side="left", padx=(self.s(7), 0))
            self.pills[key] = pill

        tk.Frame(header, bg=BORDER, height=1).pack(fill="x")

    def _build_actionbar(self, parent) -> None:
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill="x", padx=self.s(22), pady=(self.s(14), self.s(6)))

        self.btn_run_all = self._button(bar, "一键登录并开热点", self.run_all, "primary", self.f_button_primary)
        self.btn_run_all.pack(side="left")
        self.btn_campus = self._button(bar, "仅登录校园网", self.run_campus)
        self.btn_campus.pack(side="left", padx=(self.s(8), 0))
        self.btn_hotspot = self._button(bar, "仅开启热点", self.run_hotspot)
        self.btn_hotspot.pack(side="left", padx=(self.s(8), 0))
        self.btn_cancel = self._button(bar, "取消执行", self.cancel_run, "danger")
        self.btn_cancel.configure(state="disabled")
        self.btn_cancel.pack(side="left", padx=(self.s(8), 0))

        right = tk.Frame(bar, bg=BG)
        right.pack(side="right")
        self.btn_refresh = self._button(right, "刷新状态", self.refresh_now, "ghost")
        self.btn_refresh.pack(side="right")
        self.lbl_updated = tk.Label(right, text="等待首次检测…", font=self.f_hint, bg=BG, fg=SUBTLE)
        self.lbl_updated.pack(side="right", padx=(0, self.s(10)))

    def _build_statusbar(self, parent) -> None:
        bar = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        bar.pack(fill="x", side="bottom")
        inner = tk.Frame(bar, bg=CARD)
        inner.pack(fill="x", padx=self.s(22), pady=self.s(6))
        self.status_label = tk.Label(inner, text="就绪", font=self.f_hint, bg=CARD, fg=SUBTLE, anchor="w")
        self.status_label.pack(side="left")
        self.progress_label = tk.Label(inner, text="", font=self.f_hint, bg=CARD, fg=SUBTLE, anchor="e")
        self.progress_label.pack(side="right")

    def _build_notebook(self, parent) -> None:
        self.nb = TabView(parent, self)

        self.page_home = tk.Frame(self.nb.body, bg=BG)
        self.page_config = tk.Frame(self.nb.body, bg=BG)
        self.page_log = tk.Frame(self.nb.body, bg=BG)
        self.page_settings = tk.Frame(self.nb.body, bg=BG)
        self.nb.add(self.page_home, "概览")
        self.nb.add(self.page_config, "配置")
        self.nb.add(self.page_log, "日志")
        self.nb.add(self.page_settings, "设置")

        self._build_home_page()
        self._build_config_page()
        self._build_log_page()
        self._build_settings_page()
        self.nb.select(self.page_home)

    def _panel(self, parent, *, pad: float = 18) -> ui.Card:
        """统一的白色圆角面板；内容放到返回对象的 .body 里。"""
        return ui.Card(parent, scale=self.scale, pad=pad, radius=12,
                       parent_bg=ui._parent_bg(parent, BG))

    def _card(self, parent, title: str, value: str = "--", hint: str = ""):
        card = self._panel(parent, pad=16)
        body = card.body

        head = tk.Frame(body, bg=CARD)
        head.pack(fill="x")
        dot = tk.Label(head, text="●", font=self.f_dot, bg=CARD, fg=IDLE)
        dot.pack(side="left")
        tk.Label(head, text=title, font=self.f_card_title, bg=CARD, fg=SUBTLE).pack(side="left", padx=(self.s(6), 0))

        value_label = tk.Label(body, text=value, font=self.f_value, bg=CARD, fg=TEXT, anchor="w", justify="left")
        value_label.pack(fill="x", pady=(self.s(6), self.s(3)))
        hint_label = tk.Label(
            body,
            text=hint,
            font=self.f_hint,
            bg=CARD,
            fg=SUBTLE,
            anchor="w",
            justify="left",
            wraplength=self.s(360),
        )
        hint_label.pack(fill="x")
        card.autosize()
        return card, dot, value_label, hint_label

    # -------------------------------------------------------------- 概览页
    def _build_home_page(self) -> None:
        page = self.page_home
        outer = tk.Frame(page, bg=BG)
        outer.pack(fill="both", expand=True, padx=self.s(2), pady=self.s(14))
        outer.columnconfigure(0, weight=1, uniform="card")
        outer.columnconfigure(1, weight=1, uniform="card")
        outer.rowconfigure(1, minsize=self.s(104))
        outer.rowconfigure(2, minsize=self.s(104))
        outer.rowconfigure(3, weight=1)

        self.banner = ui.Card(outer, scale=self.scale, pad=13, radius=10,
                              fill=ui.WARN_SOFT, border="#F1DCC0", parent_bg=BG)
        banner_row = tk.Frame(self.banner.body, bg=ui.WARN_SOFT)
        banner_row.pack(fill="x")
        tk.Label(banner_row, text="●", font=self.f_dot, bg=ui.WARN_SOFT, fg=WARN).pack(side="left")
        tk.Label(
            banner_row,
            text="还没有填写账号密码，先在「配置」页填好，再点一键登录。",
            font=self.f_body,
            bg=ui.WARN_SOFT,
            fg=WARN,
            anchor="w",
        ).pack(side="left", padx=(self.s(8), 0))
        self.banner.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, self.s(12)))
        self.banner.grid_remove()

        self.cards: dict[str, tuple] = {}
        specs = (
            ("net", "网络连接", "未检测", "正在读取网卡信息…"),
            ("online", "校园网认证", "未检测", "正在查询在线列表…"),
            ("hotspot", "移动热点", "未检测", "正在检查 192.168.137.0/24…"),
            ("ssid", "当前 Wi-Fi", "未检测", "正在读取 netsh 输出…"),
        )
        for index, (key, title, value, hint) in enumerate(specs):
            row, column = 1 + index // 2, index % 2
            card, dot, value_label, hint_label = self._card(outer, title, value, hint)
            padx = (0, self.s(6)) if column == 0 else (self.s(6), 0)
            pady = (0, self.s(12))
            card.grid(row=row, column=column, sticky="nsew", padx=padx, pady=pady)
            self.cards[key] = (dot, value_label, hint_label)

        recent_card = self._panel(outer, pad=16)
        recent_card.grid(row=3, column=0, columnspan=2, sticky="nsew")
        recent_body = recent_card.body
        recent_head = tk.Frame(recent_body, bg=CARD)
        recent_head.pack(fill="x", pady=(0, self.s(4)))
        tk.Label(
            recent_head, text="最近动态", font=self.f_body_bold, bg=CARD, fg=TEXT, anchor="w"
        ).pack(side="left")
        self.btn_recent_log = self._button(
            recent_head, "查看完整日志", lambda: self.nb.select(self.page_log), "ghost", height=28
        )
        self.btn_recent_log.pack(side="right")
        self.recent_text = tk.Text(
            recent_body,
            height=4,
            width=1,
            font=self.f_mono,
            bg=CARD,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=self.s(12),
            pady=self.s(10),
            wrap="none",
            state="disabled",
            cursor="arrow",
        )
        self.recent_text.pack(fill="both", expand=True)
        self.recent_text.tag_configure("ok", foreground=OK)
        self.recent_text.tag_configure("err", foreground=ERR)
        self.recent_text.tag_configure("warn", foreground=WARN)
        self.recent_text.tag_configure("info", foreground=SUBTLE)
        self._render_recent()

    # -------------------------------------------------------------- 配置页
    def _build_config_page(self) -> None:
        page = self.page_config

        bar = tk.Frame(page, bg=BG)
        bar.pack(fill="x", padx=self.s(2), pady=(self.s(14), self.s(6)))
        left = tk.Frame(bar, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="校园网账号", font=self.f_section, bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(
            left,
            text="修改后会直接写入程序目录下的 campus-config.json，下次登录立即生效。",
            font=self.f_hint,
            bg=BG,
            fg=SUBTLE,
        ).pack(anchor="w", pady=(self.s(2), 0))

        actions = tk.Frame(bar, bg=BG)
        actions.pack(side="right")
        self._button(actions, "打开配置文件", self.open_config_file, "ghost", height=30).pack(side="right")
        self._button(actions, "测试 Portal", self.test_portal, "ghost", height=30).pack(
            side="right", padx=(0, self.s(6))
        )
        self._button(actions, "撤销修改", self._load_config_form, height=30).pack(
            side="right", padx=(0, self.s(6))
        )
        self.btn_save_cfg = self._button(actions, "保存配置", self.save_config_form, "primary", height=30)
        self.btn_save_cfg.pack(side="right", padx=(0, self.s(6)))

        scroller = ScrollPage(page, self)
        scroller.pack(fill="both", expand=True)
        self.config_scroll = scroller
        outer = tk.Frame(scroller.body, bg=BG)
        outer.pack(fill="both", expand=True, padx=self.s(2), pady=self.s(2))

        card = self._panel(outer, pad=20)
        card.pack(fill="x")
        body = card.body
        body.columnconfigure(0, weight=1)

        rows = (
            ("portal", "Portal 地址", "认证页面地址，可只写 10.10.10.10", False),
            ("api_port", "API 端口", "在线列表接口端口，默认 801", False),
            ("username", "登录账号", "手机号或学号，原样提交", False),
            ("password", "登录密码", "只保存在本地配置文件里", True),
            ("suffix", "账号后缀", "部分学校需要 @telecom / @cmcc，没有就留空", False),
            ("ssid_filter", "SSID 过滤", "只在这些 Wi-Fi 下登录，多个用逗号分隔", False),
            ("wait_seconds", "等待网络就绪（秒）", "开机后最多等多久网络就绪（0-3600）", False),
        )

        fields = tk.Frame(body, bg=CARD)
        fields.grid(row=0, column=0, sticky="ew")
        fields.columnconfigure(0, weight=1, uniform="field")
        fields.columnconfigure(1, weight=1, uniform="field")
        for index, (key, label, hint, secret) in enumerate(rows):
            row, column = divmod(index, 2)
            cell = tk.Frame(fields, bg=CARD)
            cell.grid(
                row=row,
                column=column,
                sticky="ew",
                padx=(0, self.s(26)) if column == 0 else (0, 0),
                pady=(0, self.s(14)),
            )
            cell.columnconfigure(0, weight=1)
            head = tk.Frame(cell, bg=CARD)
            head.grid(row=0, column=0, sticky="ew", pady=(0, self.s(5)))
            tk.Label(head, text=label, font=self.f_body, bg=CARD, fg=TEXT_SOFT, anchor="w").pack(side="left")
            var = tk.StringVar()
            entry = tk.Entry(
                cell,
                textvariable=var,
                font=self.f_body,
                bg=FIELD,
                fg=TEXT,
                relief="flat",
                highlightthickness=1,
                highlightbackground=FIELD_BORDER,
                highlightcolor=ACCENT,
                insertbackground=TEXT,
                show="*" if secret else "",
            )
            entry.grid(row=1, column=0, sticky="ew", ipady=self.s(5))
            entry.bind("<FocusIn>", lambda _event, widget=entry: widget.configure(highlightbackground=ACCENT))
            entry.bind("<FocusOut>", lambda _event, widget=entry: widget.configure(highlightbackground=FIELD_BORDER))

            foot = tk.Frame(cell, bg=CARD)
            foot.grid(row=2, column=0, sticky="ew", pady=(self.s(4), 0))
            tk.Label(
                foot, text=hint, font=self.f_hint, bg=CARD, fg=SUBTLE, anchor="w", justify="left"
            ).pack(side="left")
            if secret:
                self.var_show_password = tk.BooleanVar(value=False)
                ToggleBox(
                    foot, self, "显示", self.var_show_password, self._toggle_password,
                    background=CARD, font=self.f_label,
                ).pack(side="right")
            self.cfg_vars[key] = var
            if secret:
                self._password_entry = entry

        info_cell = tk.Frame(fields, bg=CARD)
        info_cell.grid(row=3, column=1, sticky="new", pady=(0, self.s(14)))
        tk.Label(info_cell, text="配置文件", font=self.f_body, bg=CARD, fg=TEXT_SOFT, anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        tk.Label(
            info_cell, text=str(config.CONFIG_PATH), font=self.f_hint, bg=CARD, fg=SUBTLE,
            anchor="w", justify="left", wraplength=self.s(430),
        ).grid(row=1, column=0, sticky="w", pady=(self.s(5), 0))
        tk.Label(
            info_cell, text=f"运行目录 {config.APP_DIR}", font=self.f_hint, bg=CARD, fg=SUBTLE,
            anchor="w", justify="left", wraplength=self.s(430),
        ).grid(row=2, column=0, sticky="w", pady=(self.s(3), 0))

        self.config_note = tk.Label(body, text="", font=self.f_hint, bg=CARD, fg=SUBTLE, anchor="w")
        self.config_note.grid(row=1, column=0, sticky="w", pady=(self.s(2), 0))
        card.autosize()

    # -------------------------------------------------------------- 日志页
    def _build_log_page(self) -> None:
        page = self.page_log
        outer = tk.Frame(page, bg=BG)
        outer.pack(fill="both", expand=True, padx=self.s(2), pady=self.s(12))

        bar = tk.Frame(outer, bg=BG)
        bar.pack(fill="x", pady=(0, self.s(8)))
        tk.Label(bar, text="日志来源", font=self.f_body, bg=BG, fg=TEXT).pack(side="left")
        self.log_source_var = tk.StringVar(value=LIVE_SOURCE)
        self.log_labels = [config.LOG_LABELS[key] for key in ("campus", "hotspot")]
        self.log_source_keys = {config.LOG_LABELS[key]: key for key in ("campus", "hotspot")}
        self.combo_source = ttk.Combobox(
            bar,
            textvariable=self.log_source_var,
            values=[LIVE_SOURCE, *self.log_labels],
            state="readonly",
            font=self.f_body,
            width=26,
        )
        self.combo_source.pack(side="left", padx=(self.s(8), self.s(10)))
        self.combo_source.bind("<<ComboboxSelected>>", lambda _event: self._render_log_view())

        self._button(bar, "刷新", self._render_log_view, "ghost").pack(side="left")
        self._button(bar, "清空显示", self.clear_log_view, "ghost").pack(side="left", padx=(self.s(6), 0))
        self._button(bar, "打开日志目录", self.open_project_dir, "ghost").pack(side="left", padx=(self.s(6), 0))

        self.auto_var = tk.BooleanVar(value=bool(self.settings.get("autoscroll_log", True)))
        self.auto_check = ToggleBox(
            bar, self, "自动滚动", self.auto_var, self._on_autoscroll, background=BG
        )
        self.auto_check.pack(side="right")

        box = ui.Card(outer, scale=self.scale, pad=4, radius=12, fill=FIELD,
                      border=FIELD_BORDER, parent_bg=BG)
        box.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            box.body,
            font=self.f_mono,
            width=1,
            height=1,
            bg=FIELD,
            fg=TEXT_SOFT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=self.s(14),
            pady=self.s(12),
            wrap="word",
            spacing1=self.s(2),
            state="disabled",
        )
        self.log_bar = ui.ThinScrollbar(box.body, command=self.log_text.yview, scale=self.scale, parent_bg=CARD)
        self.log_text.configure(yscrollcommand=self.log_bar.set)
        self.log_bar.pack(side="right", fill="y", padx=(0, self.s(4)), pady=self.s(6))
        self.log_text.pack(side="left", fill="both", expand=True)

        self.log_text.tag_configure("info", foreground=TEXT)
        self.log_text.tag_configure("cmd", foreground=ACCENT)
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("warn", foreground=WARN)
        self.log_text.tag_configure("err", foreground=ERR)
        self.log_text.tag_configure("ts", foreground=IDLE)
        self.log_text.tag_configure("hint", foreground=FAINT)

        self.log_note = tk.Label(outer, text="执行流程时这里会实时滚动脚本输出。", font=self.f_hint, bg=BG, fg=SUBTLE, anchor="w")
        self.log_note.pack(fill="x", pady=(self.s(6), 0))
        self._render_log_view()

    # -------------------------------------------------------------- 设置页
    def _build_settings_page(self) -> None:
        scroller = ScrollPage(self.page_settings, self)
        scroller.pack(fill="both", expand=True)
        self.settings_scroll = scroller
        outer = tk.Frame(scroller.body, bg=BG)
        outer.pack(fill="both", expand=True, padx=self.s(2), pady=self.s(14))

        columns = tk.Frame(outer, bg=BG)
        columns.pack(fill="x")
        columns.columnconfigure(0, weight=3)
        columns.columnconfigure(1, weight=2)

        card = self._panel(columns, pad=18)
        card.grid(row=0, column=0, sticky="new", padx=(0, self.s(14)))
        body = card.body
        body.columnconfigure(0, weight=1)

        tk.Label(body, text="启动与通知", font=self.f_section, bg=CARD, fg=TEXT).grid(
            row=0, column=0, sticky="w", pady=(0, self.s(12))
        )

        self.var_autostart = tk.BooleanVar(value=False)
        self.var_minimize = tk.BooleanVar(value=True)
        self.var_start_min = tk.BooleanVar(value=False)
        self.var_notify = tk.BooleanVar(value=True)
        self.var_auto_reconnect = tk.BooleanVar(value=True)
        self.var_run_on_start = tk.BooleanVar(value=True)
        self.var_resume_autostart = tk.BooleanVar(value=True)

        self._check_row(
            body, 1, "开机时自动启动（仅当前用户）", self.var_autostart, self._toggle_autostart,
            "写入 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，不需要管理员权限。",
            wrap=self.s(470),
        )
        self._check_row(
            body, 2, "关闭窗口时最小化到系统托盘", self.var_minimize, None,
            "直接点窗口右上角的 ×，程序继续在托盘里待命。", wrap=self.s(470),
        )
        self._check_row(
            body, 3, "启动后直接最小化到托盘", self.var_start_min, None,
            "下次启动生效，适合放进开机自启。", wrap=self.s(470),
        )
        self._check_row(
            body, 4, "流程结束后弹出系统通知", self.var_notify, None,
            "登录成功或失败都会弹一条通知，配合托盘运行时很有用。", wrap=self.s(470),
        )
        self._check_row(
            body, 5, "启动后自动登录并开热点", self.var_run_on_start, None,
            "启动后立刻跑一次完整流程，不用再点按钮；没填账号或正在执行时跳过。",
            wrap=self.s(470),
        )
        self._check_row(
            body, 6, "睡眠唤醒后自动连网并开热点", self.var_resume_autostart, None,
            "合盖睡眠、休眠唤醒后自动补一次：热点被系统关掉就重开，网断了就重登。",
            wrap=self.s(470),
        )

        card2 = self._panel(columns, pad=18)
        card2.grid(row=0, column=1, sticky="new")
        body2 = card2.body
        body2.columnconfigure(0, weight=1)

        tk.Label(body2, text="网络守护与超时", font=self.f_section, bg=CARD, fg=TEXT).grid(
            row=0, column=0, sticky="w", pady=(0, self.s(12))
        )
        self._check_row(
            body2, 1, "断网后自动重连校园网", self.var_auto_reconnect, None,
            "每隔一段时间探一次外网；连续两次不通、且校园网 Portal 可达时，自动重新登录。",
            wrap=self.s(300),
        )

        numbers = tk.Frame(body2, bg=CARD)
        numbers.grid(row=2, column=0, sticky="ew", pady=(self.s(6), 0))
        self.var_interval = tk.StringVar()
        self.var_reconnect_interval = tk.StringVar()
        self.var_campus_timeout = tk.StringVar()
        self.var_hotspot_timeout = tk.StringVar()
        spin_specs = (
            ("状态刷新间隔（秒）", self.var_interval, 2, 120, 1),
            ("断网检测间隔（秒）", self.var_reconnect_interval, 10, 300, 5),
            ("校园网步骤超时（秒）", self.var_campus_timeout, 60, 3600, 30),
            ("热点步骤超时（秒）", self.var_hotspot_timeout, 30, 1800, 30),
        )
        for index, (label, var, low, high, step) in enumerate(spin_specs):
            cell = tk.Frame(numbers, bg=CARD)
            cell.grid(row=index, column=0, sticky="ew", pady=self.s(4))
            cell.columnconfigure(0, weight=1)
            tk.Label(cell, text=label, font=self.f_body, bg=CARD, fg=TEXT_SOFT, anchor="w").grid(
                row=0, column=0, sticky="w"
            )
            ttk.Spinbox(
                cell, from_=low, to=high, increment=step, textvariable=var, width=6, font=self.f_body
            ).grid(row=0, column=1, sticky="e")

        actions = tk.Frame(outer, bg=BG)
        actions.pack(fill="x", pady=(self.s(14), 0))
        self._button(actions, "保存设置", self.save_settings_form, "primary").pack(side="left")
        self._button(actions, "测试通知", self.test_notify, "ghost").pack(side="left", padx=(self.s(8), 0))
        self._button(actions, "打开项目文件夹", self.open_project_dir, "ghost").pack(
            side="left", padx=(self.s(8), 0)
        )

        self.settings_note = tk.Label(outer, text="", font=self.f_hint, bg=BG, fg=SUBTLE, anchor="w")
        self.settings_note.pack(fill="x", pady=(self.s(10), 0))

        about = tk.Frame(outer, bg=BG)
        about.pack(fill="x", pady=(self.s(8), 0))
        runtime = "单文件版" if getattr(sys, "frozen", False) else f"Python {sys.version.split()[0]} + tkinter"
        about_lines = (
            f"{APP_NAME} {APP_VERSION} · {runtime} · 无第三方依赖",
            f"运行目录：{config.PROJECT_DIR}",
        )
        for line in about_lines:
            tk.Label(about, text=line, font=self.f_hint, bg=BG, fg=SUBTLE, anchor="w").pack(fill="x")

    def _check_row(self, parent, row: int, text: str, var, command, hint: str, wrap: int = 0) -> None:
        box = tk.Frame(parent, bg=CARD)
        box.grid(row=row, column=0, sticky="ew", pady=(0, self.s(6)))
        check = ToggleBox(box, self, text, var, command)
        check.pack(anchor="w")
        tk.Label(
            box, text=hint, font=self.f_hint, bg=CARD, fg=SUBTLE, anchor="w", justify="left",
            wraplength=wrap or 0,
        ).pack(anchor="w", padx=(self.s(24), 0))

    # -------------------------------------------------------------- 表单同步
    def _load_config_form(self) -> None:
        data = config.load_config()
        self.cfg = data
        for key, var in self.cfg_vars.items():
            var.set(str(data.get(key, "")))
        self.config_note.configure(text=f"当前读取：{config.CONFIG_PATH.name}", fg=SUBTLE)
        self._update_banner()

    def _load_settings_form(self) -> None:
        self.settings = config.load_settings()
        self.var_autostart.set(autostart.is_enabled())
        self.var_minimize.set(bool(self.settings.get("minimize_to_tray", True)))
        self.var_start_min.set(bool(self.settings.get("start_minimized", False)))
        self.var_notify.set(bool(self.settings.get("notify_on_finish", True)))
        self.var_auto_reconnect.set(bool(self.settings.get("auto_reconnect", True)))
        self.var_run_on_start.set(bool(self.settings.get("run_on_start", True)))
        self.var_resume_autostart.set(bool(self.settings.get("resume_autostart", True)))
        self.var_interval.set(str(self.settings.get("poll_interval", 5)))
        self.var_reconnect_interval.set(str(self.settings.get("reconnect_interval", 20)))
        self.var_campus_timeout.set(str(self.settings.get("campus_timeout", 900)))
        self.var_hotspot_timeout.set(str(self.settings.get("hotspot_timeout", 300)))
        command = autostart.current_command()
        self.settings_note.configure(
            text=("自启命令：" + command) if command else "未设置开机自启。",
            fg=SUBTLE,
        )

    def _update_banner(self) -> None:
        if config.is_configured(self.cfg):
            self.banner.grid_remove()
        else:
            self.banner.grid()

    def _toggle_password(self) -> None:
        entry = getattr(self, "_password_entry", None)
        if entry is not None:
            entry.configure(show="" if self.var_show_password.get() else "*")

    def _toggle_autostart(self) -> None:
        enabled = bool(self.var_autostart.get())
        ok = autostart.enable() if enabled else autostart.disable()
        if not ok:
            self.var_autostart.set(not enabled)
            messagebox.showerror("设置失败", "写入注册表失败，可能是权限或安全软件拦截。", parent=self.root)
            return
        self._append_log("已" + ("开启" if enabled else "关闭") + "开机自启")
        self._load_settings_form()

    def _on_autoscroll(self) -> None:
        value = bool(self.auto_var.get())
        self.settings["autoscroll_log"] = value
        config.save_settings({"autoscroll_log": value})
        if value:
            self.log_text.see("end")

    # -------------------------------------------------------------- 配置动作
    def save_config_form(self) -> None:
        values = config.normalize_config({key: var.get() for key, var in self.cfg_vars.items()})
        try:
            self.cfg = config.save_config(values)
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入 {config.CONFIG_PATH}：\n{exc}", parent=self.root)
            return
        self._load_config_form()
        self.config_note.configure(text="已保存 · " + time.strftime("%H:%M:%S"), fg=OK)
        account = str(self.cfg.get("username") or "").strip()
        self._append_log(f"配置已保存到 campus-config.json（账号 {account or '空'}）")
        self.refresh_now()

    def open_config_file(self) -> None:
        self._open_path(config.CONFIG_PATH)

    def open_project_dir(self) -> None:
        self._open_path(config.PROJECT_DIR)

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # noqa: S606 - 就是要把文件交给资源管理器
        except OSError as exc:
            messagebox.showerror("打开失败", f"{path}\n{exc}", parent=self.root)

    def test_portal(self) -> None:
        portal = self.cfg.get("portal", "")
        self._append_log(f"正在测试 Portal 连通性：{portal}")
        threading.Thread(target=self._portal_worker, args=(portal,), daemon=True, name="portal-test").start()

    def _portal_worker(self, portal: str) -> None:
        result = probe.portal_state(portal, timeout=4.0)
        self.queue.put(("portal_test", result))

    def save_settings_form(self) -> None:
        def as_int(var: tk.StringVar, default: int, low: int, high: int) -> int:
            try:
                value = int(str(var.get()).strip())
            except (TypeError, ValueError):
                value = default
            return max(low, min(value, high))

        values = {
            "minimize_to_tray": bool(self.var_minimize.get()),
            "notify_on_finish": bool(self.var_notify.get()),
            "autoscroll_log": bool(self.auto_var.get()),
            "start_minimized": bool(self.var_start_min.get()),
            "auto_reconnect": bool(self.var_auto_reconnect.get()),
            "run_on_start": bool(self.var_run_on_start.get()),
            "resume_autostart": bool(self.var_resume_autostart.get()),
            "poll_interval": as_int(self.var_interval, 5, 2, 120),
            "reconnect_interval": as_int(self.var_reconnect_interval, 20, 10, 300),
            "campus_timeout": as_int(self.var_campus_timeout, 900, 60, 3600),
            "hotspot_timeout": as_int(self.var_hotspot_timeout, 300, 30, 1800),
        }
        try:
            self.settings = config.save_settings(values)
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入 {config.SETTINGS_PATH}：\n{exc}", parent=self.root)
            return
        self._load_settings_form()
        self.settings_note.configure(text="设置已保存 · " + time.strftime("%H:%M:%S"), fg=OK)
        if values["auto_reconnect"]:
            watch = f"断网自动重连已开启（每 {values['reconnect_interval']} 秒检测）"
        else:
            watch = "断网自动重连已关闭"
        startup = "启动时自动连接已开启" if values["run_on_start"] else "启动时自动连接已关闭"
        self._append_log(f"程序设置已保存（刷新间隔 {values['poll_interval']} 秒；{watch}；{startup}）")
        self._wake.set()

    def test_notify(self) -> None:
        notify.send_toast_async(APP_NAME, "通知测试：能看到这条说明系统通知正常。")
        self._append_log("已发送测试通知")

    # -------------------------------------------------------------- 执行流程
    def _campus_steps(self) -> list:
        timeout = int(self.settings.get("campus_timeout") or 900)
        cfg = dict(self.cfg)
        return [
            runner.Step(
                label="校园网登录",
                func=lambda ctx: actions.campus_login(cfg, ctx),
                timeout=max(60, timeout),
                log_path=config.LOG_FILES["campus"],
            )
        ]

    def _hotspot_steps(self) -> list:
        hotspot_timeout = int(self.settings.get("hotspot_timeout") or 300)
        return [
            runner.Step(
                label="开启移动热点",
                func=lambda ctx: actions.enable_hotspot(ctx, max(30, hotspot_timeout)),
                timeout=max(60, hotspot_timeout + 60),
                log_path=config.LOG_FILES["hotspot"],
            )
        ]

    def _guard(self) -> bool:
        if self._busy:
            self._append_log("已有一个流程在执行，请先等待或取消。")
            return False
        if not config.is_configured(self.cfg):
            messagebox.showwarning(
                "还差一步",
                "请先在「配置」页填写校园网账号和密码，再执行登录。",
                parent=self.root,
            )
            self.nb.select(self.page_config)
            return False
        return True

    def run_all(self) -> None:
        if not self._guard():
            return
        self._start_run(self._campus_steps() + self._hotspot_steps(), "一键登录并开热点")

    def run_campus(self) -> None:
        if not self._guard():
            return
        self._start_run(self._campus_steps(), "仅登录校园网")

    def run_hotspot(self) -> None:
        if self._busy:
            self._append_log("已有一个流程在执行，请先等待或取消。")
            return
        self._start_run(self._hotspot_steps(), "仅开启移动热点")

    def _start_run(self, steps: list, title: str, auto: bool = False, origin: str = "manual") -> None:
        if not self.runner.run(steps, title):
            self._append_log("启动失败：上一个流程还没结束。")
            return
        self._origin = origin
        if not auto:
            # 自动重连/开机自动连接时不要抢用户的视线：他可能正盯着配置页抄账号
            self.nb.select(self.page_log)
            self.log_source_var.set(LIVE_SOURCE)
        self._append_log(f"===== 开始：{title} =====")

    def cancel_run(self) -> None:
        if not self._busy:
            return
        self.runner.cancel()
        self._append_log("已请求取消，正在终止脚本…")

    # -------------------------------------------------------------- 状态轮询
    def _start_poller(self) -> None:
        self._poll_gen += 1
        gen = self._poll_gen
        self._last_snap_ts = time.time()
        self._poller = threading.Thread(
            target=self._poll_worker, args=(gen,), daemon=True, name="status-poller"
        )
        self._poller.start()

    def _poll_worker(self, gen: int = 0) -> None:
        while not self._stop.is_set() and gen == self._poll_gen:
            interval = max(2, int(self.settings.get("poll_interval", 5) or 5))
            self._wake.wait(interval)
            self._wake.clear()
            if self._stop.is_set() or gen != self._poll_gen:
                return
            try:
                snap = probe.snapshot(self.cfg, include_online=True)
            except Exception as exc:  # noqa: BLE001 - 轮询线程不能死
                snap = {"ts": time.time(), "error": str(exc)}
            self._last_snap_ts = time.time()
            if self._stop.is_set() or gen != self._poll_gen:
                return
            self.queue.put({"kind": "status", "snap": snap})

    def _ensure_poller(self) -> None:
        """轮询线程死掉、或者卡在一次探测里很久没回来，就换一个新的。

        睡眠唤醒后旧线程可能还挂在某个 socket 上，靠这个兜底把状态卡片重新拉活。
        """
        if self._stop.is_set():
            return
        interval = max(2, int(self.settings.get("poll_interval", 5) or 5))
        limit = max(60, interval * 6)
        stalled = time.time() - self._last_snap_ts > limit
        dead = self._poller is None or not self._poller.is_alive()
        if not stalled and not dead:
            return
        if stalled:
            self._append_log(f"状态检测超过 {int(limit)} 秒没有结果，重启检测线程。", "warn")
        self._start_poller()
        self._wake.set()

    def _on_resume(self, gap: float) -> None:
        """检测到系统刚睡醒：清缓存、把卡片打回"检测中"、立刻重新探测一次。"""
        probe.reset_caches()
        self._online_known = None
        self._auto_attempts = 0
        self._auto_ready_at = 0.0
        self._offline_seen = False
        self._pending_snap = None
        minutes = gap / 60.0
        span = f"{minutes:.0f} 分钟" if minutes >= 1 else f"{int(gap)} 秒"
        self._append_log(f"程序从睡眠中恢复（间隔 {span}），正在重新检测状态。", "warn")
        self.status_label.configure(text="已从睡眠恢复，正在刷新状态…", fg=TEXT)
        self.lbl_updated.configure(text="正在刷新…", fg=SUBTLE)
        self._last_snap_ts = time.time()
        self._resume_started_at = self._last_snap_ts
        self._resume_wait = 0
        self.refresh_now()
        self.root.after(1500, self._resume_step, self._ensure_poller)
        # 等这一轮探测出结果，再决定要不要补一次登录/开热点
        self.root.after(4000, self._resume_step, self._maybe_resume_run)

    def _resume_step(self, callback) -> None:
        """唤醒后延迟执行的收尾动作；程序正在退出时直接丢弃。"""
        if self._closing:
            return
        try:
            callback()
        except tk.TclError:
            pass

    def _maybe_resume_run(self) -> None:
        """唤醒后补一次：热点被系统关了重开，网断了重登，都正常就什么也不做。"""
        if not bool(self.settings.get("resume_autostart", True)):
            return
        if self._busy or self.runner.busy:
            self._append_log("唤醒后自动连接：已有流程在执行，本次跳过。", "warn")
            return
        if not config.is_configured(self.cfg):
            self._append_log("唤醒后自动连接：还没有填写账号密码，本次跳过。", "warn")
            return

        snap = self.last_snap or {}
        # 唤醒后的第一份快照可能还没回来，别拿睡眠前那份去判断要不要重新登录
        if float(snap.get("ts") or 0.0) < self._resume_started_at:
            if self._resume_wait < 4:
                self._resume_wait += 1
                self.root.after(2000, self._resume_step, self._maybe_resume_run)
                return
            self._resume_wait = 0
        try:
            hotspot_on = bool(probe.hotspot_state().get("on"))
        except Exception:  # noqa: BLE001 - 探测失败就当作没开，让流程去确认
            hotspot_on = False
        online = snap.get("online") or {}
        need_hotspot = not hotspot_on
        need_login = not (bool(online.get("ok")) and bool(online.get("online")))

        if not need_hotspot and not need_login:
            self._append_log("唤醒后检查：网络和热点都在，无需操作。")
            return

        parts = []
        if need_login:
            parts.append("登录校园网")
        if need_hotspot:
            parts.append("开启热点")
        self._append_log(f"唤醒后检查：需要{' + '.join(parts)}，开始处理…", "cmd")

        steps = []
        if need_login:
            steps += self._campus_steps()
        if need_hotspot:
            steps += self._hotspot_steps()
        self._start_run(steps, "唤醒后自动连接", auto=True, origin="resume")

    # -------------------------------------------------------------- 断网自动重连
    def _start_watchdog(self) -> None:
        self._watchdog = threading.Thread(target=self._watch_worker, daemon=True, name="net-watchdog")
        self._watchdog.start()

    def _watch_interval(self) -> int:
        try:
            value = int(self.settings.get("reconnect_interval", 20) or 20)
        except (TypeError, ValueError):
            value = 20
        return max(10, value)

    def _watch_worker(self) -> None:
        """每隔一会儿探一次外网。连续两次不通，且校园 Portal 可达时，自动重新登录。

        为什么不直接看状态卡片：卡片的在线判断会走在线列表接口兜底，而这里要的是
        「这台机器现在到底能不能上网」，用 204 探测最简单也最准。
        """
        streak = 0
        last_round = time.monotonic()
        while not self._stop.is_set():
            if self._stop.wait(self._watch_interval()):
                return
            now = time.monotonic()
            interval = self._watch_interval()
            if now - last_round > interval * 3:
                # 中间发生了睡眠/休眠，不能把这段空档算成"连续两次探测失败"
                streak = 0
            last_round = now
            if self._stop.is_set():
                return
            if not bool(self.settings.get("auto_reconnect", True)):
                streak = 0
                self._auto_ready_at = 0.0
                continue
            if not config.is_configured(self.cfg):
                streak = 0
                continue
            # 正在跑脚本、或处在退避期时，先不探，省得和拨号抢资源
            if self._busy or self.runner.busy or time.time() < self._auto_ready_at:
                streak = 0
                continue
            try:
                online = probe.internet_state(timeout=3.0)
            except Exception:  # noqa: BLE001 - 看门狗不能死
                online = False
            if online:
                streak = 0
                self._auto_attempts = 0
                self._auto_ready_at = 0.0
                if self._offline_seen:
                    self._offline_seen = False
                    self.queue.put(("watch", {"event": "online"}))
                continue
            streak += 1
            if streak < 2:
                continue
            streak = 0
            self._offline_seen = True
            try:
                portal = probe.portal_state(self.cfg.get("portal", ""), timeout=3.0)
            except Exception:  # noqa: BLE001 - Portal 测不通也只当不可达
                portal = {"ok": False}
            if not portal.get("ok"):
                self.queue.put(("watch", {"event": "outside"}))
                # 不在校园网就先安静两分钟，别刷屏
                self._auto_ready_at = time.time() + 120
                continue
            self._auto_attempts += 1
            self.queue.put(("watch", {"event": "reconnect", "attempt": self._auto_attempts}))
            backoff = min(600, 120 * (2 ** min(self._auto_attempts - 1, 2)))
            self._auto_ready_at = time.time() + backoff

    def _handle_watch_event(self, payload: dict) -> None:
        event = payload.get("event")
        if event == "online":
            self._append_log("外网已恢复。", "ok")
            self.status_label.configure(text="网络已恢复", fg=OK)
            if bool(self.settings.get("notify_on_finish", True)) and not self._auto_just_ok:
                notify.send_toast_async(APP_NAME, "网络已恢复")
            self._auto_just_ok = False
        elif event == "outside":
            self._append_log("检测到断网，但校园网 Portal 不可达（可能不在校园网），暂不自动重连。", "warn")
        elif event == "reconnect":
            attempt = int(payload.get("attempt") or 1)
            self._append_log(f"检测到断网，开始第 {attempt} 次自动重连…", "cmd")
            self._run_auto()

    def _run_auto(self) -> None:
        if self._busy:
            self._append_log("自动重连：已有流程在执行，本次跳过。", "warn")
            return
        if not config.is_configured(self.cfg):
            self._append_log("自动重连：还没有填写账号密码，跳过。", "warn")
            return
        self._start_run(self._campus_steps(), "自动重连校园网", auto=True, origin="reconnect")

    def _maybe_autorun(self) -> None:
        """开机自启时不用人点：窗口一起来就先把网接上。

        只在这一个进程里触发一次；已经在跑流程、或者还没填账号时安静跳过，
        不弹窗也不报错——开机场景里弹窗比失败更烦人。
        """
        if not bool(self.settings.get("run_on_start", True)):
            return
        if self._busy or self.runner.busy:
            return
        if not config.is_configured(self.cfg):
            self._append_log("启动自动连接：还没有填写账号密码，本次跳过。", "warn")
            return
        self._start_run(
            self._campus_steps() + self._hotspot_steps(),
            "启动自动连接",
            auto=True,
            origin="startup",
        )

    def refresh_now(self) -> None:
        self.lbl_updated.configure(text="正在检测…")
        self._wake.set()

    def _request_refresh(self) -> None:
        """步骤内部（比如刚登录成功）想让界面立刻刷新一次状态。"""
        self._wake.set()

    # -------------------------------------------------------------- 托盘
    def _start_tray(self) -> None:
        icon = config.ASSETS_DIR / "app.ico"
        if not icon.exists():
            self._append_log("找不到托盘图标 assets/app.ico，托盘未启用。")
            return
        menu = [
            (1, "显示主界面"),
            None,
            (2, "一键登录并开热点"),
            (3, "仅登录校园网"),
            (4, "仅开启热点"),
            None,
            (5, "退出"),
        ]
        command_map = {"show": 1, "run_all": 2, "campus": 3, "hotspot": 4, "quit": 5}
        try:
            self._tray = tray.TrayIcon(str(icon), f"{APP_NAME} {APP_VERSION}", menu, self.queue, command_map)
            self._tray_ready = self._tray.start(3.0)
        except Exception as exc:  # noqa: BLE001
            self._tray = None
            self._tray_ready = False
            self._append_log(f"托盘初始化失败：{exc}")
        if not self._tray_ready:
            reason = self._tray.last_error if self._tray is not None else None
            detail = f"（{reason}）" if reason else ""
            self._append_log(f"托盘图标不可用{detail}，关闭窗口将直接退出程序。")

    def _tray_tooltip(self, snap: dict | None) -> None:
        if not (self._tray and self._tray_ready and snap):
            return
        online = snap.get("online") or {}
        hotspot = snap.get("hotspot") or {}
        state = "校园网在线" if online.get("online") else "校园网未在线"
        hotspot_text = "热点已开" if hotspot.get("on") else "热点关闭"
        self._tray.set_tooltip(f"{APP_NAME} · {state} · {hotspot_text}")

    # -------------------------------------------------------------- 事件循环
    def _pump(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                try:
                    if isinstance(item, dict):
                        self._handle_runner_event(item)
                    elif isinstance(item, tuple) and len(item) == 2:
                        self._handle_ui_event(item[0], item[1])
                except Exception as exc:  # noqa: BLE001 - 单条事件出错不能拖垮界面
                    self._append_log(f"界面处理异常：{exc}")
        except queue.Empty:
            pass
        # 注意：状态轮询每几秒就会有事件进来，那不算"用户在操作"，
        # 所以这里不刷新 _last_activity，只由真实点击/按键和窗口唤起来记时。
        now = time.time()
        gap = now - self._last_tick
        self._last_tick = now
        # 事件泵每 120ms 跑一次，正常不会断档；能断到 90 秒以上只可能是系统睡眠/休眠，
        # 或者进程被整体挂起。唤醒后网络、网卡、热点多半都变了，得重新探一遍。
        if gap > 90 and now - self._last_resume_at > 60:
            self._last_resume_at = now
            self._on_resume(gap)
        elif not self._busy:
            self._ensure_poller()
        self._maybe_trim()
        self.root.after(120, self._pump)

    # -------------------------------------------------------------- 内存整理
    def _touch_activity(self, _event=None) -> None:
        """任何用户操作都算"还在用"，推迟空闲整理。"""
        self._last_activity = time.time()

    def _maybe_trim(self) -> None:
        """把用不到的物理内存交回系统。

        收在托盘里（窗口不可见）时每 60 秒整理一次，常驻占用最低；
        窗口开着的时候只在真正空闲 90 秒后才整理，且 5 分钟内最多一次，
        免得刚整理完又被下一次重绘全部换回来。
        """
        if self._busy or self.runner.busy:
            return
        now = time.time()
        hidden = not self.root.winfo_viewable()
        if hidden:
            interval = HIDDEN_TRIM_INTERVAL
        else:
            if now - self._last_activity < IDLE_TRIM_SECONDS:
                return
            interval = IDLE_TRIM_INTERVAL
        if now - self._last_trim < interval:
            return
        self._last_trim = now
        trim_working_set()

    def _handle_ui_event(self, kind: str, action) -> None:
        if kind == "tray":
            if action == "show":
                self.show_window()
            elif action == "quit":
                self.quit_app()
            elif action == "run_all":
                self.run_all()
            elif action == "campus":
                self.run_campus()
            elif action == "hotspot":
                self.run_hotspot()
        elif kind == "instance":
            self.show_window()
        elif kind == "watch":
            self._handle_watch_event(action if isinstance(action, dict) else {})
        elif kind == "portal_test":
            result = action if isinstance(action, dict) else {}
            if result.get("ok"):
                self._append_log(f"Portal 可达（HTTP {result.get('status')}，{result.get('ms')} ms）", "ok")
            else:
                self._append_log("Portal 不可达：检查是否连着校园网、地址是否写对。", "err")

    def _handle_runner_event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "run_start":
            self._set_busy(True)
            self.status_label.configure(text=f"正在执行：{event.get('title')}", fg=TEXT)
            self.progress_label.configure(text=f"0/{event.get('total')} 步")
        elif kind == "step_start":
            progress = f"{event.get('index')}/{event.get('total')} 步"
            self.progress_label.configure(text=f"{progress} · {event.get('label')}")
            self._append_log(f"[{progress}] {event.get('label')} 开始执行…", "cmd")
        elif kind == "line":
            self._append_log(str(event.get("text", "")))
        elif kind == "step_done":
            code = event.get("exit")
            if event.get("timed_out"):
                self._append_log(f"{event.get('label')} 超时被终止", "err")
            elif code == 0:
                self._append_log(f"{event.get('label')} 完成", "ok")
            else:
                self._append_log(f"{event.get('label')} 失败", "err")
        elif kind == "run_done":
            self._set_busy(False)
            ok = bool(event.get("ok"))
            message = str(event.get("message") or "")
            elapsed = event.get("elapsed", 0)
            auto = self._origin != "manual"
            origin = self._origin
            self._origin = "manual"
            self._auto_just_ok = bool(auto and ok)
            if auto and ok:
                # 重连成功：解除退避，下一次探测立刻验证网络是否真的回来了
                self._auto_ready_at = 0.0
            tag = "ok" if ok else "err"
            self._append_log(f"===== {'完成' if ok else '中断'}：{message}，用时 {elapsed} 秒 =====", tag)
            self.status_label.configure(
                text=("执行完成：" + message) if ok else ("执行中断：" + message),
                fg=OK if ok else ERR,
            )
            self.progress_label.configure(text="")
            if self.settings.get("notify_on_finish", True):
                if origin == "startup":
                    notify.send_toast_async(
                        APP_NAME,
                        "已自动登录校园网并开启热点" if ok else f"开机自动连接失败：{message or '脚本出错'}",
                    )
                elif origin == "reconnect":
                    notify.send_toast_async(
                        APP_NAME,
                        "断网后已自动重连成功" if ok else f"自动重连失败：{message or '脚本出错'}",
                    )
                elif origin == "resume":
                    notify.send_toast_async(
                        APP_NAME,
                        "睡眠唤醒后已自动连网并开启热点" if ok else f"唤醒后自动连接失败：{message or '脚本出错'}",
                    )
                else:
                    notify.send_toast_async(APP_NAME, message or ("流程已完成" if ok else "流程失败"))
            # 流程跑完、托盘里也没人看的时候顺手把工作集交回系统
            if not self.root.winfo_viewable() or self.root.state() != "normal":
                self._last_trim = time.time()
                trim_working_set()
            self.refresh_now()
        elif kind == "status":
            self._apply_snapshot(event.get("snap") or {})

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (self.btn_run_all, self.btn_campus, self.btn_hotspot, self.btn_save_cfg):
            button.configure(state=state)
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    # -------------------------------------------------------------- 快照渲染
    def _apply_snapshot(self, snap: dict) -> None:
        if not snap:
            return
        if snap.get("error"):
            self.lbl_updated.configure(text="检测出错 · " + time.strftime("%H:%M:%S"), fg=ERR)
            self._append_log(f"状态检测失败：{snap.get('error')}", "err")
            return

        self.last_snap = snap

        net = snap.get("net") or {}
        ssid = str(snap.get("ssid") or "")
        hotspot = snap.get("hotspot") or {}
        portal = snap.get("portal") or {}
        online = snap.get("online") or {}
        stale_age: int | None = None
        if online.get("ok"):
            self._online_known = {
                "ts": time.time(),
                "online": bool(online.get("online")),
                "count": int(online.get("count") or 0),
                "matched_ip": str(online.get("matched_ip") or ""),
            }
        elif self._online_known:
            age = int(time.time() - float(self._online_known.get("ts") or 0))
            if 0 <= age <= 300:
                stale_age = age
                online = dict(online)
                online.update(
                    {
                        "ok": True,
                        "online": self._online_known["online"],
                        "count": self._online_known["count"],
                        "matched_ip": self._online_known["matched_ip"],
                    }
                )
        # 收在托盘里时不刷界面：Tk 每次重绘都会重新占回十来兆工作集，
        # 只记下最新快照、更新托盘提示，等窗口真的被叫出来再一次性渲染。
        if not self.root.winfo_viewable():
            self._pending_snap = snap
            self._tray_tooltip(snap)
            return

        self.lbl_updated.configure(text="更新于 " + time.strftime("%H:%M:%S"), fg=SUBTLE)
        configured = bool(snap.get("configured"))
        ip = str(net.get("ip") or "")

        self._set_card(
            "net",
            ip or "未连接网络",
            (f"{net.get('adapter') or '未知网卡'} · {net.get('up_adapters', 0)} 块网卡在线" if ip else "没有可用的上行网卡"),
            OK if ip else ERR,
        )
        if not configured:
            online_value, online_color = "未配置账号", WARN
            online_hint = "去「配置」页填账号密码"
        elif online.get("online") and online.get("source") == "net":
            online_value, online_color = "已在线", OK
            if online.get("http_status"):
                online_hint = "在线列表没列出本机，外网已通"
            else:
                online_hint = "在线列表接口超时，外网已通"
        elif online.get("online"):
            online_value, online_color = "已在线", OK
            online_hint = f"本机 {online.get('matched_ip') or ip} 已认证"
        elif online.get("ok"):
            online_value, online_color = "未在线", WARN
            online_hint = f"{online.get('count', 0)} 台设备在线"
        else:
            online_value, online_color = "无法查询", IDLE
            online_hint = str(online.get("error") or "在线列表接口不可达")
        if stale_age is not None:
            online_hint = f"{online_hint} · {stale_age} 秒前"
        portal_hint = f"Portal {portal.get('ms')} ms" if portal.get("ok") else "Portal 不可达"
        self._set_card("online", online_value, f"{online_hint} · {portal_hint}", online_color)

        hotspot_on = bool(hotspot.get("on"))
        clients = int(hotspot.get("clients") or 0)
        if hotspot_on:
            ip_now = str(hotspot.get("ip") or "")
            if ip_now:
                hint = f"{ip_now} · " + (f"{clients} 台设备连接" if clients else "暂无设备连接")
            else:
                hint = "WinRT 报告已开启，还没分到 IP"
            self._set_card("hotspot", "已开启", hint, OK)
        else:
            state = str(hotspot.get("state") or "")
            if state == "Off":
                hint = "WinRT 报告已关闭"
            elif state == "InTransition":
                hint = "正在开启或关闭中…"
            else:
                hint = "192.168.137.0/24 上没有本机地址"
            self._set_card("hotspot", "未开启", hint, WARN if state == "InTransition" else IDLE)

        if self._last_hotspot_on is not None and hotspot_on != self._last_hotspot_on:
            if hotspot_on:
                extra = f"（{clients} 台设备连接）" if clients else ""
                self._append_log(f"检测到移动热点已开启{extra}", "ok")
            else:
                self._append_log("检测到移动热点已关闭", "warn")
        self._last_hotspot_on = hotspot_on

        if ssid:
            self._set_card("ssid", ssid, "当前已连接的无线网络", OK)
        else:
            self._set_card("ssid", "未连接", "没有已连接的 Wi-Fi；用有线网络同样可以登录", IDLE)

        self._set_pill("net", "网络", "已连接" if ip else "未连接", OK if ip else ERR)
        if not configured:
            self._set_pill("online", "校园网", "未配置", WARN)
        elif online.get("online"):
            self._set_pill("online", "校园网", "在线", OK)
        elif online.get("ok"):
            self._set_pill("online", "校园网", "未在线", WARN)
        else:
            self._set_pill("online", "校园网", "未知", IDLE)
        self._set_pill("hotspot", "热点", "已开启" if hotspot.get("on") else "已关闭", OK if hotspot.get("on") else IDLE)
        self._set_pill("ssid", "Wi-Fi", ssid or "未连接", OK if ssid else IDLE)

        self._update_banner()
        self._tray_tooltip(snap)

    def _set_card(self, key: str, value: str, hint: str, color: str) -> None:
        dot, value_label, hint_label = self.cards[key]
        dot.configure(fg=color)
        value_label.configure(text=value)
        hint_label.configure(text=hint)

    def _set_pill(self, key: str, label: str, state: str, color: str) -> None:
        self.pills[key].set(state, color)

    # -------------------------------------------------------------- 日志渲染
    def _append_log(self, text: str, tag: str | None = None) -> None:
        text = text.rstrip()
        if not text:
            return
        tag = tag or self._classify(text)
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp}  {text}"
        self.log_lines.append(line)
        self.recent_lines.append(line)
        if self.log_source_var.get() == LIVE_SOURCE:
            self._insert_log_line(line, tag)
        self._render_recent()

    def _insert_log_line(self, line: str, tag: str) -> None:
        widget = self.log_text
        widget.configure(state="normal")
        if widget.get("1.0", "end-1c").startswith("还没有输出"):
            widget.delete("1.0", "end")
        widget.insert("end", line + "\n", tag)
        excess = int(widget.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            widget.delete("1.0", f"{excess + 1}.0")
        widget.configure(state="disabled")
        if self.auto_var.get():
            widget.see("end")

    def _render_recent(self) -> None:
        widget = self.recent_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        lines = list(self.recent_lines)[-5:]
        if not lines:
            widget.insert("end", "还没有执行记录。点上面的「一键登录并开热点」，这里会显示每一步的进展。", "info")
        for line in lines:
            widget.insert("end", line + "\n", self._classify(line))
        widget.configure(state="disabled")
        widget.see("end")

    @staticmethod
    def _classify(text: str) -> str:
        if text.startswith("> "):
            return "cmd"
        if any(word in text for word in ("失败", "错误", "异常", "超时", "不可达", "ERROR", "中止")):
            return "err"
        if any(word in text for word in ("成功", "已在线", "已开启", "完成")):
            return "ok"
        if any(word in text for word in ("警告", "跳过", "WARN")):
            return "warn"
        return "info"

    def _render_log_view(self) -> None:
        source = self.log_source_var.get()
        widget = self.log_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if source == LIVE_SOURCE:
            for line in self.log_lines:
                widget.insert("end", line + "\n", self._classify(line))
            if not self.log_lines:
                widget.insert("end", "还没有输出。点「一键登录并开热点」或「仅登录校园网」，\n每一步的命令、耗时和结果都会实时打在这里。", "hint")
            self.log_note.configure(text="实时执行日志：脚本输出会立刻显示在这里。", fg=SUBTLE)
        else:
            key = self.log_source_keys.get(source)
            path = config.LOG_FILES.get(key or "")
            if not path or not path.exists():
                widget.insert("end", f"还没有 {Path(path).name if path else '该'} 日志文件。\n", "warn")
                self.log_note.configure(text=f"文件不存在：{path}", fg=WARN)
            else:
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raw = b""
                    widget.insert("end", f"读取失败：{exc}\n", "err")
                lines = probe._decode_console(raw).splitlines()[-3000:]
                for line in lines:
                    widget.insert("end", line + "\n", self._classify(line))
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
                self.log_note.configure(
                    text=f"{path.name} · {len(lines)} 行 · 最后修改 {stamp} · {path.stat().st_size} 字节",
                    fg=SUBTLE,
                )
        widget.configure(state="disabled")
        if self.auto_var.get():
            widget.see("end")

    def clear_log_view(self) -> None:
        if self.log_source_var.get() == LIVE_SOURCE:
            self.log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -------------------------------------------------------------- 窗口与退出
    def show_window(self) -> None:
        self._touch_activity()
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        try:
            self.root.focus_force()
            self.root.attributes("-topmost", True)
            self.root.after(200, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass
        self._flush_pending_snapshot()

    def _on_mapped(self, _event=None) -> None:
        """窗口被任务栏/系统还原时补渲染托盘期间攒下的快照。"""
        if self.root.winfo_viewable():
            self.root.after_idle(self._flush_pending_snapshot)

    def _flush_pending_snapshot(self) -> None:
        pending = self._pending_snap
        self._pending_snap = None
        if pending and self.root.winfo_viewable():
            self._apply_snapshot(pending)

    def _on_close(self) -> None:
        if self.settings.get("minimize_to_tray", True) and self._tray_ready:
            self.root.withdraw()
            self.status_label.configure(text="已最小化到托盘，双击托盘图标可恢复窗口。")
            # 收进托盘就把工作集交回系统，托盘里常驻时占的物理内存很小
            self._last_trim = time.time()
            trim_working_set()
            return
        self.quit_app()

    def quit_app(self) -> None:
        self._closing = True
        self._stop.set()
        self._wake.set()
        if self.runner.busy:
            self.runner.cancel()
        if self._tray and self._tray_ready:
            self._tray.stop()
        if self._instance:
            self._instance.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    start_minimized = "--minimized" in argv

    config.ensure_runtime_files()

    instance = SingleInstance()
    if not instance.acquire():
        instance.notify_existing()
        return 0

    scale = apply_dpi_awareness()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 4.0 * scale / 3.0)
    except tk.TclError:
        pass

    app = App(root, scale=scale, start_minimized=start_minimized)
    app._instance = instance
    instance.listen(lambda: app.queue.put(("instance", "show")))
    freeze_heap()
    try:
        root.mainloop()
    finally:
        instance.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
