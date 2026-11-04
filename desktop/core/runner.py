"""流程执行器：串行跑若干个步骤，把进度与日志通过 emit(dict) 抛给界面。

每个步骤就是一个 Python 函数（以前是调 powershell 跑 .ps1）。执行器负责：
同一时间只跑一个流程、超时判定、取消、把步骤结果汇总成 run_done。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import logfile


@dataclass
class Step:
    """一个待执行的步骤：label 给界面看，func(ctx) 返回 True/False。"""

    label: str
    func: Callable[["RunContext"], bool]
    timeout: int = 900
    log_path: Path | None = None


class RunContext:
    """步骤拿到的东西：写日志、判断是否该停、登记子进程、请求刷新状态。"""

    def __init__(self, emit, cancel: threading.Event, deadline: float, log_path: Path | None, refresh):
        self._emit = emit
        self.cancel = cancel
        self.deadline = deadline
        self.log_path = log_path
        self._refresh = refresh
        self.process = None

    # ---------------------------------------------------------------- 日志
    def log(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        self._emit({"kind": "line", "text": text})
        logfile.append_log(self.log_path, text)

    # ---------------------------------------------------------------- 状态
    def aborted(self) -> bool:
        return self.cancel.is_set() or time.time() > self.deadline

    @property
    def timed_out(self) -> bool:
        return (not self.cancel.is_set()) and time.time() > self.deadline

    # ------------------------------------------------------------ 子进程
    def attach(self, proc) -> None:
        self.process = proc

    def detach(self) -> None:
        self.process = None

    # ------------------------------------------------------------ 刷新
    def refresh(self, **_flags) -> None:
        if self._refresh:
            self._refresh()


class ScriptRunner:
    """名字沿用旧的；现在跑的是 Python 步骤而不是脚本。"""

    def __init__(self, emit, refresh=None):
        self._emit = emit
        self._refresh = refresh
        self._state_lock = threading.Lock()
        self._busy = False
        self._cancel = threading.Event()
        self._ctx: RunContext | None = None
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    def run(self, steps: list[Step], title: str = "") -> bool:
        with self._state_lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel.clear()
        self._thread = threading.Thread(target=self._worker, args=(steps, title), daemon=True, name="step-runner")
        self._thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()
        ctx = self._ctx
        if ctx is not None and ctx.process is not None:
            from .actions import probe_kill

            probe_kill(ctx.process)

    # ------------------------------------------------------------------ 内部
    def _worker(self, steps: list[Step], title: str) -> None:
        started = time.time()
        results: list[dict] = []
        ok = True
        message = ""
        try:
            self._emit({"kind": "run_start", "title": title, "total": len(steps)})
            for index, step in enumerate(steps, 1):
                if self._cancel.is_set():
                    ok, message = False, "已取消"
                    break
                self._emit({"kind": "step_start", "label": step.label, "index": index, "total": len(steps)})
                code, timed_out, cancelled = self._run_one(step)
                results.append({"label": step.label, "exit": code, "timed_out": timed_out, "cancelled": cancelled})
                self._emit(
                    {
                        "kind": "step_done",
                        "label": step.label,
                        "exit": code,
                        "timed_out": timed_out,
                        "cancelled": cancelled,
                        "index": index,
                        "total": len(steps),
                    }
                )
                if cancelled:
                    ok, message = False, "已取消"
                    break
                if code != 0:
                    ok = False
                    message = f"{step.label}超时" if timed_out else f"{step.label}失败"
                    break
            else:
                message = f"{len(steps)} 个步骤全部完成"
            if ok and not steps:
                ok, message = False, "没有可执行的步骤"
        except Exception as exc:  # noqa: BLE001 - 线程里必须兜住，否则界面永远卡在执行中
            ok, message = False, f"执行异常：{exc}"
        finally:
            self._ctx = None
            with self._state_lock:
                self._busy = False
            self._emit(
                {
                    "kind": "run_done",
                    "ok": ok,
                    "message": message,
                    "elapsed": round(time.time() - started, 1),
                    "results": results,
                }
            )

    def _run_one(self, step: Step) -> tuple[int, bool, bool]:
        deadline = time.time() + max(step.timeout, 30)
        ctx = RunContext(
            emit=self._emit,
            cancel=self._cancel,
            deadline=deadline,
            log_path=step.log_path,
            refresh=self._refresh,
        )
        self._ctx = ctx
        ok = False
        try:
            ok = bool(step.func(ctx))
        except Exception as exc:  # noqa: BLE001 - 单步异常不该炸掉整个线程
            ctx.log(f"执行出错：{type(exc).__name__}: {exc}")
            ok = False
        finally:
            self._ctx = None
        timed_out = ctx.timed_out
        if timed_out:
            ctx.log(f"-- 超过 {step.timeout} 秒，该步骤已中止 --")
        cancelled = self._cancel.is_set()
        return (0 if ok else 1), timed_out, cancelled


# 兼容旧的导入方式
def kill_tree(pid: int) -> None:  # pragma: no cover - 只给历史代码留的壳
    from .actions import probe_kill

    class _P:
        pass

    holder = _P()
    holder.pid = pid
    probe_kill(holder)
