"""校园网登录与移动热点的原生实现。

以前这两件事交给项目根目录的两个 PowerShell 脚本；现在脚本已经删掉，
所有逻辑都收在这里，只依赖标准库 + 系统自带的 PowerShell（仅用于 WinRT 开热点）。

对外只有两个入口：
    campus_login(cfg, ctx) -> bool
    enable_hotspot(ctx, timeout) -> bool
其中 ctx 提供 log(text) / aborted() / attach(proc) / detach()。
"""

from __future__ import annotations

import base64
import json
import os
import random
import time
import urllib.parse
from pathlib import Path

from . import probe

CREATE_NO_WINDOW = 0x08000000

# 热点开启后 192.168.137.x 出现需要一点时间，给足等待
_HOTSPOT_WAIT = 40
# 拨号后 NAS 建会话需要时间，脚本里是 4 秒；失败重试之间再等 5 秒
_LOGIN_SETTLE = 4
_LOGIN_RETRY_GAP = 5


# --------------------------------------------------------------------- 小工具
def _ssid_matches(current: str, wanted: str) -> bool:
    """ssid_filter 支持逗号分隔多个，子串匹配（不区分大小写）。"""
    if not wanted:
        return True
    if not current:
        return False
    current = current.strip().lower()
    for item in str(wanted).replace("，", ",").split(","):
        item = item.strip().lower()
        if item and item in current:
            return True
    return False


def _wait(delay: float, ctx) -> bool:
    """可被取消打断的 sleep；返回 False 表示该中止了。"""
    end = time.time() + max(0.0, delay)
    while time.time() < end:
        if ctx.aborted():
            return False
        time.sleep(min(0.3, max(0.0, end - time.time())))
    return not ctx.aborted()


# --------------------------------------------------------------------- 网络
def _net_ready(cfg: dict) -> dict | None:
    """主用网络是否就绪：有 IPv4、非 169.254，且（配了过滤时）SSID 匹配。"""
    info = probe.local_info()
    ip = (info.get("ip") or "").strip()
    if not ip or ip.startswith("169.254."):
        return None
    ssid_filter = (cfg.get("ssid_filter") or "").strip()
    ssid = probe.wifi_ssid()
    if ssid_filter and not _ssid_matches(ssid, ssid_filter):
        return None
    return {"ip": ip, "mac": info.get("mac", ""), "ssid": ssid, "adapter": info.get("adapter", "")}


def _api_base(cfg: dict) -> str:
    portal = (cfg.get("portal") or "http://10.10.10.10").strip()
    if "://" not in portal:
        portal = "http://" + portal
    portal = portal.rstrip("/")
    host = urllib.parse.urlsplit(portal).hostname or "10.10.10.10"
    try:
        port = int(cfg.get("api_port") or 801)
    except (TypeError, ValueError):
        port = 801
    if port <= 0:
        port = 801
    return f"http://{host}:{port}"


def _host_base(cfg: dict) -> str:
    portal = (cfg.get("portal") or "http://10.10.10.10").strip()
    if "://" not in portal:
        portal = "http://" + portal
    host = urllib.parse.urlsplit(portal.rstrip("/")).hostname or "10.10.10.10"
    return f"http://{host}"


def _online_state(cfg: dict, ip: str) -> str:
    """online / offline / unknown 三态。

    unknown 表示在线列表接口不可用（这个接口经常整片超时），需要再用 204 兜底。
    """
    result = probe.online_state(_api_base(cfg), ip, timeout=1.5, attempts=2)
    if result.get("source") == "net":
        return "unknown"
    if not result.get("ok"):
        return "unknown"
    return "online" if result.get("online") else "offline"


def _internet_ok() -> bool:
    try:
        return bool(probe.internet_state())
    except Exception:  # noqa: BLE001 - 探测失败按“不能上网”处理
        return False


# --------------------------------------------------------------------- 登录
def campus_login(cfg: dict, ctx) -> bool:
    log = ctx.log
    try:
        wait_seconds = int(cfg.get("wait_seconds") or 300)
    except (TypeError, ValueError):
        wait_seconds = 300
    wait_seconds = max(0, min(wait_seconds, 3600))
    username = (cfg.get("username") or "").strip()
    password = (cfg.get("password") or "").strip()
    suffix = (cfg.get("suffix") or "").strip()
    if not username or not password:
        log("请先在「配置」页填写校园网账号和密码，再登录。")
        return False

    # 1. 等待网络就绪
    log(f"等待网络就绪（最长 {wait_seconds} 秒）...")
    started = time.time()
    net = None
    while time.time() - started < wait_seconds:
        if ctx.aborted():
            log("已取消。")
            return False
        net = _net_ready(cfg)
        if net:
            break
        if not _wait(5, ctx):
            log("已取消。")
            return False
    if not net:
        log("超时：未检测到有效网络连接，本次退出。")
        return False
    waited = int(time.time() - started)
    parts = [f"IP={net['ip']}"]
    if net.get("mac"):
        parts.append(f"MAC={net['mac']}")
    if net.get("ssid"):
        parts.append(f"SSID={net['ssid']}")
    log(f"网络就绪：{' '.join(parts)}（等待 {waited} 秒）")

    # 2. 等待 Portal 可达
    portal = (cfg.get("portal") or "http://10.10.10.10").strip()
    log(f"等待 Portal {portal} 可达...")
    started = time.time()
    reachable = False
    while time.time() - started < wait_seconds:
        if ctx.aborted():
            log("已取消。")
            return False
        state = probe.portal_state(portal, timeout=4.0)
        if state.get("ok"):
            reachable = True
            break
        if not _wait(5, ctx):
            log("已取消。")
            return False
    if not reachable:
        # 原脚本这里也是 exit 0：很可能不在校园网，不该报成登录失败
        log("Portal 不可达（当前网络可能不是校园网），本次退出。")
        return True
    log("Portal 可达。")

    # 3. 在线检测
    state = _online_state(cfg, net["ip"])
    if state == "online":
        log(f"本机已在线（账号 {username} / IP {net['ip']}），无需登录。")
        return True
    if state == "unknown":
        if _internet_ok():
            log("在线列表接口不可用，但外网已通，判定为已在线，无需登录。")
            return True
        log("在线列表接口不可用且外网不通，继续尝试登录...")
    else:
        log("本机未在线，开始登录...")

    # 4. 拨号 + 回查
    account = username + suffix if suffix else username
    host_base = _host_base(cfg)
    retries = 3
    for attempt in range(1, retries + 1):
        if ctx.aborted():
            log("已取消。")
            return False
        query = (
            "callback=dr1003"
            + "&DDDDD=" + urllib.parse.quote(account, safe="")
            + "&upass=" + urllib.parse.quote(password, safe="")
            + "&0MKKey=123456&R1=0&R2=&R3=1&R6=0&para=00&v6ip="
            + "&terminal_type=1&lang=zh-cn&jsVersion=4.2.1"
            + "&v=" + str(random.randint(1000, 9999))
            + "&lang=zh"
        )
        url = f"{host_base}/drcom/login?{query}"
        log(f"第 {attempt}/{retries} 次拨号...")
        ok, status, body = probe.http_get(url, timeout=12.0)
        text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body or "")
        payload = probe._parse_jsonp(text) if text.strip() else None
        if isinstance(payload, dict):
            log(
                "拨号响应: result={0} hidn={1} msg={2} msga={3}".format(
                    payload.get("result", ""),
                    payload.get("hidn", ""),
                    payload.get("msg", ""),
                    payload.get("msga", ""),
                )
            )
        else:
            log(f"拨号响应为空（HTTP {status if ok else '超时'}）")

        if not _wait(_LOGIN_SETTLE, ctx):
            log("已取消。")
            return False
        state = _online_state(cfg, net["ip"])
        if state == "online":
            log("回查确认：本机已在线，登录成功！")
            _finish(ctx, net, account)
            return True
        if state == "unknown" and _internet_ok():
            log("在线列表接口不可用，但外网已通，判定登录成功。")
            _finish(ctx, net, account)
            return True
        if attempt < retries:
            log("回查尚未在线，等待重试...")
            if not _wait(_LOGIN_RETRY_GAP, ctx):
                log("已取消。")
                return False

    log("自动登录失败：多次拨号后本机仍未在线。")
    return False


def _finish(ctx, net: dict, account: str) -> None:
    ctx.log("自动登录流程完成（成功）。")
    ctx.refresh(online=True)


# --------------------------------------------------------------------- 热点
_TETHER_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$wait = {wait}
$interval = 2
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object {{ $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' }})[0]
function Await-WinRt {{
    param([object]$Operation, [type]$ResultType)
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($Operation))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
}}
$profile = $null
$sw = [System.Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $wait) {{
    $candidate = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]::GetInternetConnectionProfile()
    if ($candidate -and $candidate.GetNetworkConnectivityLevel().ToString() -eq 'InternetAccess') {{ $profile = $candidate; break }}
    Start-Sleep -Seconds $interval
}}
if ($null -eq $profile) {{
    '{{"ok":false,"stage":"network","message":"未检测到互联网连接"}}'
    exit 1
}}
$null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]
try {{
    $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
    if ($manager.TetheringOperationalState.ToString() -eq 'On') {{
        '{{"ok":true,"stage":"already","message":"移动热点已处于开启状态"}}'
        exit 0
    }}
    $result = Await-WinRt -Operation $manager.StartTetheringAsync() -ResultType ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    $status = $result.Status.ToString()
    if ($status -eq 'Success') {{
        '{{"ok":true,"stage":"started","message":"移动热点已开启"}}'
        exit 0
    }}
    $msg = $result.AdditionalErrorMessage
    if (-not $msg) {{ $msg = $status }}
    '{{"ok":false,"stage":"start","message":' + ($msg | ConvertTo-Json -Compress) + '}}'
    exit 1
}} catch {{
    '{{"ok":false,"stage":"error","message":' + ($_.Exception.Message | ConvertTo-Json -Compress) + '}}'
    exit 1
}}
"""


def _powershell_exe() -> str:
    candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.exists() else "powershell.exe"


def _run_tether_script(ctx, wait_seconds: int) -> dict:
    """跑一次内联 PowerShell（不落地成 .ps1 文件）开启移动热点。"""
    import subprocess

    script = _TETHER_SCRIPT.format(wait=int(wait_seconds))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    cmd = [_powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return {"ok": False, "message": f"启动 PowerShell 失败：{exc}"}
    ctx.attach(proc)
    try:
        while proc.poll() is None:
            if ctx.aborted():
                probe_kill(proc)
                return {"ok": False, "message": "已取消"}
            time.sleep(0.2)
        raw = proc.stdout.read() if proc.stdout else b""
    finally:
        ctx.detach()
    text = probe._decode_console(raw)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"ok": False, "message": text.strip() or "没有拿到热点的开启结果"}


def probe_kill(proc) -> None:
    import subprocess

    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def enable_hotspot(ctx, timeout: int = 120) -> bool:
    log = ctx.log
    state = probe.hotspot_state()
    if state.get("on"):
        log("移动热点已处于开启状态，无需操作。")
        ctx.refresh(hotspot=True)
        return True

    log("正在开启移动热点...")
    result = _run_tether_script(ctx, timeout)
    if not result.get("ok"):
        message = result.get("message") or "未知原因"
        log(f"开启失败：{message}")
        return False

    log(result.get("message") or "移动热点已开启。")
    # 等 192.168.137.x 真的出现，避免“命令成功了但网卡还没起来”
    deadline = time.time() + _HOTSPOT_WAIT
    while time.time() < deadline:
        if ctx.aborted():
            return False
        if probe.hotspot_state().get("on"):
            ctx.refresh(hotspot=True)
            return True
        time.sleep(1.0)
    log(f"热点命令已执行，但 {_HOTSPOT_WAIT} 秒内没看到 192.168.137.x，请稍后刷新状态。")
    return True
