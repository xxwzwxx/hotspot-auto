"""用 ctypes 直接调 Win32 实现系统托盘（不依赖 pystray / Pillow）。

托盘跑在自己的线程里，菜单点击通过 events 队列交给界面线程，避免跨线程碰 tkinter。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 21
WM_SET_TOOLTIP = WM_APP + 22

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
SM_CXSMICON, SM_CYSMICON = 49, 50
IDI_APPLICATION = 32512


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", wt.POINT),
    ]


def _setup_prototypes() -> None:
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wt.ATOM
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
    ]
    user32.CreateWindowExW.restype = wt.HWND
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.LoadImageW.restype = wt.HANDLE
    user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
    user32.LoadIconW.restype = wt.HICON
    user32.CreatePopupMenu.argtypes = []
    user32.CreatePopupMenu.restype = wt.HMENU
    user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    user32.AppendMenuW.restype = wt.BOOL
    user32.DestroyMenu.argtypes = [wt.HMENU]
    user32.DestroyMenu.restype = wt.BOOL
    user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p]
    user32.TrackPopupMenu.restype = wt.UINT
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.TranslateMessage.restype = wt.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.DestroyWindow.restype = wt.BOOL
    user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
    user32.UnregisterClassW.restype = wt.BOOL
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.GetCursorPos.restype = wt.BOOL
    user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wt.UINT
    shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wt.BOOL
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wt.HMODULE


_setup_prototypes()

_CLASS_NAME = "HotspotAutoDesktopTray"


class TrayIcon:
    """menu_items: [(id, 文本)]，None 表示分隔线。点击事件以 (kind, payload) 推入 events 队列。"""

    def __init__(self, icon_path: str, tooltip: str, menu_items: list, events, command_map: dict[str, int]):
        self._icon_path = str(icon_path)
        self._tooltip = (tooltip or "")[:127]
        self._menu_items = menu_items
        self._events = events
        self._command_map = command_map  # {'show': 1, 'run': 2, 'quit': 3}
        self._ready = False
        self._last_error: str | None = None
        self._hwnd = None
        self._hicon = None
        self._nid: NOTIFYICONDATAW | None = None
        self._thread: threading.Thread | None = None
        self._wndproc_ref = WNDPROC(self._wndproc)
        self._pending_tooltip: str | None = None
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    # ---------------------------------------------------------------- 对外
    def start(self, timeout: float = 4.0) -> bool:
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True, name="tray")
        self._thread.start()
        ready.wait(timeout)
        return self._ready

    def set_tooltip(self, text: str) -> None:
        self._pending_tooltip = (text or "")[:127]
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_SET_TOOLTIP, 0, 0)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def stop(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    # ---------------------------------------------------------------- 线程内
    def _run(self, ready: threading.Event) -> None:
        try:
            if not self._create_window():
                return
            if not self._add_icon():
                return
            self._ready = True
            ready.set()

            msg = MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # noqa: BLE001 - 线程里不能让异常打死进程
            self._last_error = f"{type(exc).__name__}: {exc}"
        finally:
            ready.set()
            self._cleanup()

    def _cleanup(self) -> None:
        self._ready = False
        if self._nid is not None and self._hwnd:
            try:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except OSError:
                pass
            self._nid = None
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        try:
            user32.UnregisterClassW(_CLASS_NAME, kernel32.GetModuleHandleW(None))
        except OSError:
            pass

    def _create_window(self) -> bool:
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            error = ctypes.get_last_error()
            if error != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                self._last_error = f"RegisterClassExW 失败（错误码 {error}）"
                return False

        hwnd = user32.CreateWindowExW(
            0, _CLASS_NAME, "HotspotAutoDesktopTray", 0,
            0, 0, 0, 0, None, None, hinst, None,
        )
        if not hwnd:
            self._last_error = f"CreateWindowExW 失败（错误码 {ctypes.get_last_error()}）"
            return False
        self._hwnd = hwnd
        return True

    def _load_icon(self):
        size = user32.GetSystemMetrics(SM_CXSMICON) or 16
        hicon = None
        try:
            hicon = user32.LoadImageW(None, self._icon_path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        except OSError:
            hicon = None
        if not hicon:
            hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        return hicon

    def _add_icon(self) -> bool:
        self._hicon = self._load_icon()
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self._tooltip
        self._nid = nid
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._last_error = f"Shell_NotifyIconW 失败（错误码 {ctypes.get_last_error()}）"
            self._nid = None
            return False
        nid.uVersion = 4
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        return True

    def _refresh_tooltip(self) -> None:
        if self._pending_tooltip is None or not self._hwnd or self._nid is None:
            return
        self._tooltip = self._pending_tooltip
        self._pending_tooltip = None
        self._nid.szTip = self._tooltip
        self._nid.uFlags = NIF_TIP
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            for item in self._menu_items:
                if item is None:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                else:
                    identifier, label = item
                    user32.AppendMenuW(menu, MF_STRING, identifier, label)
            user32.SetForegroundWindow(self._hwnd)
            point = wt.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                point = wt.POINT(0, 0)
            command = user32.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                point.x, point.y, 0, self._hwnd, None,
            )
            user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            if command:
                kind = next((k for k, v in self._command_map.items() if v == command), None)
                if kind:
                    self._events.put(("tray", kind))
        finally:
            user32.DestroyMenu(menu)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                code = int(lparam) & 0xFFFF
                if code in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._events.put(("tray", "show"))
                    return 0
                if code == WM_RBUTTONUP:
                    self._show_menu()
                    return 0
                return 0
            if msg == WM_SET_TOOLTIP:
                self._refresh_tooltip()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                self._add_icon()
                return 0
        except Exception:  # noqa: BLE001 - 窗口过程里必须吞掉异常
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
