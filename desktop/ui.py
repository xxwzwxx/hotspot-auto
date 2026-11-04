"""自绘控件：圆角按钮、状态药丸、圆角卡片、细滚动条。

tkinter 原生控件都是方角，这里用 Canvas 自己画圆角，让界面看起来更接近
Windows 11 那一类现代桌面程序；控件数量很少（十几二十个），内存代价可以忽略。
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

# ------------------------------------------------------------------ 设计令牌
BG = "#F5F7FA"
SURFACE = "#FFFFFF"
CARD = "#FFFFFF"
CARD_BORDER = "#E3E8EF"
FIELD = "#FBFCFE"
FIELD_BORDER = "#DCE3EC"
TEXT = "#0F172A"
TEXT_SOFT = "#334155"
SUBTLE = "#64748B"
FAINT = "#94A3B8"
ACCENT = "#2563EB"
ACCENT_HOVER = "#1D4ED8"
ACCENT_PRESS = "#1E40AF"
ACCENT_SOFT = "#EAF1FE"
OK = "#0E9F6E"
OK_SOFT = "#E6F7F0"
WARN = "#B45309"
WARN_SOFT = "#FDF3E3"
ERR = "#DC2626"
ERR_SOFT = "#FDECEC"
IDLE = "#94A3B8"
IDLE_SOFT = "#F1F5F9"

SOFT_TINTS = {
    OK: OK_SOFT,
    WARN: WARN_SOFT,
    ERR: ERR_SOFT,
    ACCENT: ACCENT_SOFT,
    SUBTLE: IDLE_SOFT,
    IDLE: IDLE_SOFT,
    FAINT: IDLE_SOFT,
}


def soft_tint(color: str) -> str:
    return SOFT_TINTS.get(color, IDLE_SOFT)


def rounded_points(x1: float, y1: float, x2: float, y2: float, radius: float, steps: int = 7) -> list[float]:
    """生成圆角矩形的多边形顶点（顺时针），用来填一个平滑的圆角面。"""
    r = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    if r <= 0.5:
        return [x1, y1, x2, y1, x2, y2, x1, y2]
    points: list[float] = []
    arcs = (
        (x2 - r, y1 + r, -math.pi / 2, 0.0),  # 右上
        (x2 - r, y2 - r, 0.0, math.pi / 2),  # 右下
        (x1 + r, y2 - r, math.pi / 2, math.pi),  # 左下
        (x1 + r, y1 + r, math.pi, math.pi * 1.5),  # 左上
    )
    for cx, cy, start, end in arcs:
        for index in range(steps + 1):
            angle = start + (end - start) * index / steps
            points.extend((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return points


def _parent_bg(widget, fallback: str = BG) -> str:
    try:
        return str(widget.cget("bg"))
    except tk.TclError:
        return fallback


def measure(font_spec, text: str) -> int:
    return tkfont.Font(font=font_spec).measure(text)


# ------------------------------------------------------------------ 圆角按钮
class RoundedButton(tk.Canvas):
    """自绘圆角按钮；支持 configure(state=...)、invoke()、pack/grid。"""

    KINDS = {
        "primary": {
            "fill": ACCENT,
            "hover": ACCENT_HOVER,
            "press": ACCENT_PRESS,
            "fg": "#FFFFFF",
            "fg_disabled": "#F1F5F9",
            "fill_disabled": "#AFC1DE",
            "border": "",
        },
        "danger": {
            "fill": "#DC2626",
            "hover": "#B91C1C",
            "press": "#991B1B",
            "fg": "#FFFFFF",
            "fg_disabled": FAINT,
            "fill_disabled": "#EEF1F5",
            "border": "",
        },
        "secondary": {
            "fill": "#FFFFFF",
            "hover": "#F3F6FA",
            "press": "#E9EEF5",
            "fg": TEXT,
            "fg_disabled": FAINT,
            "fill_disabled": "#F4F6F9",
            "border": CARD_BORDER,
        },
        "ghost": {
            "fill": "",
            "hover": "#EDF1F6",
            "press": "#E3E9F1",
            "fg": TEXT_SOFT,
            "fg_disabled": FAINT,
            "fill_disabled": "",
            "border": "",
        },
    }

    def __init__(
        self,
        parent,
        text: str,
        command=None,
        *,
        kind: str = "secondary",
        font=None,
        scale: float = 1.0,
        height: float = 36,
        padx: float = 18,
        min_width: float = 0,
        parent_bg: str | None = None,
        radius: float = 9,
    ) -> None:
        self._bg_parent = parent_bg or _parent_bg(parent)
        self._kind = kind if kind in self.KINDS else "secondary"
        self._palette = self.KINDS[self._kind]
        self._text = text
        self._command = command
        self._font = font
        self._radius = max(2.0, radius * scale)
        self._padx = padx * scale
        self._min_width = min_width * scale
        self._height = max(18.0, height * scale)
        self._state = "normal"
        self._hover = False
        self._pressed = False
        self._focused = False

        width = max(self._min_width, self._natural_width())
        super().__init__(
            parent,
            width=width,
            height=self._height,
            bg=self._bg_parent,
            highlightthickness=0,
            bd=0,
            takefocus=1,
            cursor="hand2",
        )
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Return>", lambda _event: self.invoke())
        self.bind("<space>", lambda _event: self.invoke())
        self.bind("<Configure>", lambda _event: self._draw())

    # ---------------------------------------------------------- 尺寸与绘制
    def _natural_width(self) -> float:
        return measure(self._font, self._text) + self._padx * 2

    def _fill(self) -> str:
        palette = self._palette
        if self._state == "disabled":
            return palette["fill_disabled"] or self._bg_parent
        if self._pressed:
            return palette["press"]
        if self._hover:
            return palette["hover"] or self._bg_parent
        return palette["fill"] or self._bg_parent

    def _fg(self) -> str:
        if self._state == "disabled":
            return self._palette["fg_disabled"]
        return self._palette["fg"]

    def _draw(self) -> None:
        self.delete("all")
        width = self.winfo_width() or int(self["width"])
        height = self.winfo_height() or int(self["height"])
        if width <= 1 or height <= 1:
            return
        fill = self._fill()
        border = self._palette["border"] if self._state != "disabled" else ""
        points = rounded_points(0.5, 0.5, width - 0.5, height - 0.5, self._radius)
        self.create_polygon(points, fill=fill, outline=border or fill, width=1 if border else 0)
        if self._focused:
            ring = rounded_points(1.5, 1.5, width - 1.5, height - 1.5, max(2.0, self._radius - 1.5))
            self.create_polygon(ring, fill="", outline=ACCENT, width=2)
        self.create_text(
            width / 2,
            height / 2,
            text=self._text,
            font=self._font,
            fill=self._fg(),
        )

    def _on_focus_in(self, _event=None) -> None:
        self._focused = True
        self._draw()

    def _on_focus_out(self, _event=None) -> None:
        self._focused = False
        self._pressed = False
        self._draw()

    # ---------------------------------------------------------- 事件
    def _on_enter(self, _event=None) -> None:
        if self._state == "disabled":
            return
        self._hover = True
        self._draw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _event=None) -> None:
        if self._state == "disabled":
            return
        self._pressed = True
        self._draw()

    def _on_release(self, _event=None) -> None:
        if self._state == "disabled":
            return
        was_pressed = self._pressed
        self._pressed = False
        self._draw()
        if was_pressed and self._command is not None:
            self._command()

    # ---------------------------------------------------------- 兼容 tk.Button 的用法
    def configure(self, cnf=None, **kwargs):  # noqa: D401 - 仿 tkinter 签名
        if isinstance(cnf, dict):
            merged = dict(cnf)
            merged.update(kwargs)
            kwargs = merged
            cnf = None
        if "state" in kwargs:
            self._state = str(kwargs.pop("state"))
            super().configure(cursor="arrow" if self._state == "disabled" else "hand2")
            self._draw()
        if "text" in kwargs:
            self._text = str(kwargs.pop("text"))
            self._resize()
        if kwargs:
            super().configure(cnf, **kwargs)
        return self

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().cget(key)

    def invoke(self) -> None:
        if self._state != "disabled" and self._command is not None:
            self._command()

    def set_text(self, text: str) -> None:
        self._text = text
        self._resize()

    def _resize(self) -> None:
        width = max(self._min_width, self._natural_width())
        super().configure(width=width)
        self._draw()


# ------------------------------------------------------------------ 状态药丸
class Pill(tk.Canvas):
    """圆角状态药丸：小圆点 + 文案，颜色跟着状态走。"""

    def __init__(self, parent, label: str, *, scale: float = 1.0, font=None, parent_bg: str | None = None) -> None:
        self._bg_parent = parent_bg or _parent_bg(parent)
        self._label = label
        self._state = "--"
        self._tone = SUBTLE
        self._font = font
        self._pad_x = 10 * scale
        self._dot = 6 * scale
        self._gap = 6 * scale
        self._height = 24 * scale
        width = self._natural_width()
        super().__init__(
            parent,
            width=width,
            height=self._height,
            bg=self._bg_parent,
            highlightthickness=0,
            bd=0,
        )
        self.bind("<Configure>", lambda _event: self._draw())

    def _text(self) -> str:
        return f"{self._label} · {self._state}"

    def _natural_width(self) -> float:
        return measure(self._font, self._text()) + self._pad_x * 2 + self._dot + self._gap

    def _draw(self) -> None:
        self.delete("all")
        height = self.winfo_height() or int(self["height"])
        width = self.winfo_width() or int(self["width"])
        if width <= 1 or height <= 1:
            return
        points = rounded_points(0.5, 0.5, width - 0.5, height - 0.5, height / 2)
        self.create_polygon(points, fill=soft_tint(self._tone), outline=soft_tint(self._tone), width=0)
        cy = height / 2
        x = self._pad_x + self._dot / 2
        self.create_oval(x - self._dot / 2, cy - self._dot / 2, x + self._dot / 2, cy + self._dot / 2,
                         fill=self._tone, outline=self._tone)
        self.create_text(
            x + self._dot / 2 + self._gap,
            cy,
            text=self._text(),
            anchor="w",
            font=self._font,
            fill=TEXT_SOFT if self._tone in (IDLE, FAINT, SUBTLE) else self._tone,
        )

    def set(self, state: str, tone: str) -> None:
        self._state = state
        self._tone = tone
        width = max(self._natural_width(), 40)
        self.configure(width=width)
        self._draw()


# ------------------------------------------------------------------ 圆角卡片
class Card(tk.Frame):
    """圆角白卡片：Canvas 画底，内容放在内嵌的 Frame 里。"""

    def __init__(
        self,
        parent,
        *,
        scale: float = 1.0,
        pad: float = 16,
        radius: float = 12,
        fill: str = CARD,
        border: str = CARD_BORDER,
        parent_bg: str | None = None,
    ) -> None:
        self._bg_parent = parent_bg or _parent_bg(parent)
        super().__init__(parent, bg=self._bg_parent)
        self._pad = int(round(pad * scale))
        self._radius = radius * scale
        self._fill = fill
        self._border = border
        self.canvas = tk.Canvas(self, bg=self._bg_parent, highlightthickness=0, bd=0, height=100)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=fill)
        self._window = self.canvas.create_window(self._pad, self._pad, anchor="nw", window=self.body)
        self._fitted = 0
        self.canvas.bind("<Configure>", self._on_configure)
        # 卡片内容变化（或被映射到屏幕上）时自动长高，pack(fill="x") 也不会把内容裁掉
        self.body.bind("<Configure>", lambda _event: self._fit_height())
        self.bind("<Map>", lambda _event: self.after_idle(self._fit_height))
        self.after_idle(self._fit_height)

    def _on_configure(self, event) -> None:
        self.canvas.delete("card_bg")
        points = rounded_points(0.5, 0.5, event.width - 0.5, event.height - 0.5, self._radius)
        self.canvas.create_polygon(points, fill=self._fill, outline=self._border, width=1, tags="card_bg")
        self.canvas.tag_lower("card_bg")
        self.canvas.itemconfigure(self._window, width=max(1, event.width - 2 * self._pad))

    def _fit_height(self) -> None:
        """让 canvas 的请求高度跟内容走（内容变高变矮都重新算）。"""
        self.body.update_idletasks()
        height = max(self.body.winfo_reqheight(), self.body.winfo_height()) + 2 * self._pad
        if height <= 0 or abs(height - self._fitted) < 1:
            return
        self._fitted = height
        self.canvas.configure(height=height)

    def autosize(self) -> None:
        """按内容高度定高（兼容旧调用）。"""
        self.body.update_idletasks()
        self._fit_height()


# ------------------------------------------------------------------ 细滚动条
class ThinScrollbar(tk.Canvas):
    """窄滚动条：一个圆角滑块，替代 ttk 的方角滚动条。"""

    def __init__(self, parent, command=None, *, scale: float = 1.0, width: float = 8,
                 parent_bg: str | None = None) -> None:
        self._bg_parent = parent_bg or _parent_bg(parent)
        self._command = command
        self._first = 0.0
        self._last = 1.0
        self._drag_origin = None
        super().__init__(
            parent,
            width=max(4, width * scale),
            bg=self._bg_parent,
            highlightthickness=0,
            bd=0,
        )
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set(self, first, last) -> None:
        try:
            self._first = float(first)
            self._last = float(last)
        except (TypeError, ValueError):
            self._first, self._last = 0.0, 1.0
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        height = self.winfo_height()
        width = self.winfo_width()
        if height <= 2 or width <= 2:
            return
        if self._last - self._first >= 0.999:
            return
        top = max(0.0, self._first * height)
        bottom = min(float(height), self._last * height)
        if bottom - top < 18:
            bottom = min(float(height), top + 18)
        points = rounded_points(1, top + 2, width - 1, bottom - 2, (width - 2) / 2, steps=4)
        self.create_polygon(points, fill="#CBD5E1", outline="#CBD5E1")

    def _on_press(self, event) -> None:
        height = max(1, self.winfo_height())
        if not (self._first * height <= event.y <= self._last * height):
            # 点空白处：翻一页
            page = max(0.05, self._last - self._first)
            target = self._first + (page if event.y > self._last * height else -page)
            self._scroll_to(max(0.0, min(1.0 - page, target)))
        self._drag_origin = (event.y, self._first)

    def _on_drag(self, event) -> None:
        if not self._drag_origin:
            return
        start_y, start_first = self._drag_origin
        height = max(1, self.winfo_height())
        delta = (event.y - start_y) / height
        self._scroll_to(max(0.0, min(1.0 - (self._last - self._first), start_first + delta)))

    def _on_release(self, _event=None) -> None:
        self._drag_origin = None

    def _scroll_to(self, first: float) -> None:
        if self._command is not None:
            self._command("moveto", str(max(0.0, min(1.0, first))))
