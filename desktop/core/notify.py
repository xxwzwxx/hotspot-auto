"""结果通知：复用项目里已验证可用的 WinRT Toast，失败时回退托盘气泡。"""

from __future__ import annotations

import base64
import threading
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


def _powershell() -> str:
    candidate = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if candidate.exists():
        return str(candidate)
    return "powershell.exe"


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


_TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$t = {title}
$m = {message}
function San([string]$s) {{
    if (-not $s) {{ return '' }}
    return $s.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
}}
try {{
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
    $appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\WindowsPowerShell\v1.0\powershell.exe'
    $xml = '<toast duration="short"><visual><binding template="ToastGeneric"><text>' + (San $t) + '</text><text>' + (San $m) + '</text></binding></visual></toast>'
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    exit 0
}} catch {{
    try {{
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $icon = New-Object System.Windows.Forms.NotifyIcon
        $icon.Icon = [System.Drawing.SystemIcons]::Information
        $icon.Visible = $true
        $icon.ShowBalloonTip(6000, $t, $m, [System.Windows.Forms.ToolTipIcon]::Info)
        Start-Sleep -Milliseconds 6000
        $icon.Dispose()
        exit 0
    }} catch {{
        exit 1
    }}
}}
"""


def send_toast(title: str, message: str, timeout: float = 20.0) -> bool:
    """同步发送通知，返回是否成功。界面线程请用 send_toast_async。"""
    import subprocess

    script = _TOAST_SCRIPT.format(title=_ps_quote(title), message=_ps_quote(message))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-STA", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def send_toast_async(title: str, message: str) -> None:
    threading.Thread(target=send_toast, args=(title, message), daemon=True, name="toast").start()
