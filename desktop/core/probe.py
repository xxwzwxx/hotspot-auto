"""状态探测：本机 IP / Wi-Fi SSID / Portal 可达性 / 校园网在线状态 / 移动热点状态。

全部用标准库实现，不引入 psutil、requests 之类的第三方包：
  * 网卡与 IP：ctypes 直接调 GetAdaptersAddresses；
  * SSID：ctypes 调 WlanAPI 读当前连接（读不到才回退 netsh，省掉每 5 秒起一个进程）；
  * Portal / 在线列表：裸 socket 发 HTTP/1.1 请求（显式直连，绕开系统代理，
    否则 10.x 内网会被代理拦掉）。不用 urllib 是因为它会连带 import
    http.client / email / ssl，占掉 3 MB 左右常驻内存。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import ipaddress
import json
import socket
import time

CREATE_NO_WINDOW = 0x08000000

# Windows 移动热点（ICS）默认网段
HOTSPOT_NETWORK = ipaddress.ip_network("192.168.137.0/24")

IF_TYPE_ETHERNET = 6
IF_TYPE_IEEE80211 = 71
IF_TYPE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131

_IF_OPER_STATUS_UP = 1


# ------------------------------------------------------------------ 网卡枚举
class _SOCKADDR(ctypes.Structure):
    _fields_ = [
        ("sa_family", ctypes.c_ushort),
        ("sa_data", ctypes.c_char * 14),
    ]


class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.POINTER(_SOCKADDR)),
        ("iSockaddrLength", ctypes.c_int),
    ]


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    # 原结构这里是 union { ULONGLONG Alignment; struct { ULONG Length; DWORD IfIndex; } }，
    # 用 c_ulonglong 占位可保持后续字段偏移一致。
    ("Alignment", ctypes.c_ulonglong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    # 结构里这里是 union { ULONG Flags; struct {...} }，4 字节；用 c_ulonglong 会让
    # 后面的 Mtu/IfType/OperStatus 全部错位（表现为所有网卡都显示“未连接”）。
    ("Flags", ctypes.c_ulong),
    ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", ctypes.c_ulong),
    ("ZoneIndices", ctypes.c_ulong * 16),
    ("FirstPrefix", ctypes.c_void_p),
]

_AF_UNSPEC = 0
_ERROR_BUFFER_OVERFLOW = 111


def _sockaddr_to_ip(pointer, length: int) -> str:
    if not pointer:
        return ""
    try:
        raw = ctypes.string_at(pointer, max(int(length), 2))
    except (ValueError, OSError):
        return ""
    if len(raw) < 8:
        return ""
    family = int.from_bytes(raw[0:2], "little")
    if family == socket.AF_INET:
        return socket.inet_ntoa(raw[4:8])
    if family == socket.AF_INET6 and len(raw) >= 24:
        try:
            return socket.inet_ntop(socket.AF_INET6, raw[8:24])
        except (ValueError, OSError):
            return ""
    return ""


def list_adapters() -> list[dict]:
    """返回所有网卡：[{name, desc, ips, up, if_type, mac}]，IPv4/IPv6 都在 ips 里。"""
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    except OSError:
        return []

    func = iphlpapi.GetAdaptersAddresses
    func.argtypes = [
        wt.ULONG,
        wt.ULONG,
        ctypes.c_void_p,
        ctypes.POINTER(_IP_ADAPTER_ADDRESSES),
        ctypes.POINTER(wt.ULONG),
    ]
    func.restype = wt.ULONG

    size = wt.ULONG(16 * 1024)
    buffer = ctypes.create_string_buffer(size.value)
    for _ in range(4):
        ret = func(_AF_UNSPEC, 0, None, ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_ADDRESSES)), ctypes.byref(size))
        if ret == _ERROR_BUFFER_OVERFLOW:
            buffer = ctypes.create_string_buffer(size.value)
            continue
        if ret != 0:
            return []
        break
    else:
        return []

    adapters: list[dict] = []
    pointer = ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    guard = 0
    while pointer and guard < 256:
        guard += 1
        item = pointer.contents
        ips: list[str] = []
        unicast = item.FirstUnicastAddress
        hops = 0
        while unicast and hops < 64:
            hops += 1
            entry = unicast.contents
            ip = _sockaddr_to_ip(entry.Address.lpSockaddr, entry.Address.iSockaddrLength)
            if ip:
                ips.append(ip)
            unicast = entry.Next

        length = int(item.PhysicalAddressLength)
        mac = "-".join(f"{b:02X}" for b in item.PhysicalAddress[:length]) if 0 < length <= 8 else ""
        adapters.append(
            {
                "name": item.FriendlyName or "",
                "desc": item.Description or "",
                "ips": ips,
                "up": item.OperStatus == _IF_OPER_STATUS_UP,
                "if_type": int(item.IfType),
                "mac": mac,
            }
        )
        pointer = item.Next
    return adapters


def _udp_probe_ip() -> str:
    """兜底：靠一次不会真正发包的 UDP connect 拿默认出口 IP。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("223.5.5.5", 53))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return ""


def local_info() -> dict:
    """推断当前主用网络：返回 {ip, ssid 交给上层, adapter, mac, up_adapters}。"""
    adapters = list_adapters()
    candidates: list[tuple[int, dict, str]] = []
    for adapter in adapters:
        if not adapter["up"]:
            continue
        if adapter["if_type"] in (IF_TYPE_LOOPBACK, IF_TYPE_TUNNEL):
            continue
        for ip in adapter["ips"]:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.version != 4 or addr.is_link_local or addr.is_loopback:
                continue
            if addr in HOTSPOT_NETWORK:
                continue
            # Wi-Fi / 以太网优先，其余（如蓝牙、虚拟网卡）排后面
            score = 0 if adapter["if_type"] in (IF_TYPE_ETHERNET, IF_TYPE_IEEE80211) else 1
            candidates.append((score, adapter, ip))

    up_adapters = sum(1 for a in adapters if a["up"] and a["if_type"] not in (IF_TYPE_LOOPBACK, IF_TYPE_TUNNEL))
    if not candidates:
        return {"ip": _udp_probe_ip(), "adapter": "", "mac": "", "up_adapters": up_adapters}

    candidates.sort(key=lambda item: item[0])
    _, adapter, ip = candidates[0]
    return {"ip": ip, "adapter": adapter["name"], "mac": adapter["mac"], "up_adapters": up_adapters}


def _hotspot_ip() -> tuple[str, str]:
    """192.168.137.x 上的本机地址（ICS 默认网段）。"""
    for adapter in list_adapters():
        if not adapter["up"]:
            continue
        for ip in adapter["ips"]:
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if addr.version == 4 and addr in HOTSPOT_NETWORK:
                name = adapter["name"]
                if "Wi-Fi Direct" in (adapter["desc"] or ""):
                    name = "移动热点网卡"
                return ip, name
    return "", ""


_TETHER_QUERY = r"""
$ErrorActionPreference = 'Stop'
try {
  $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
  $null = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
  $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
  if ($null -eq $profile) {
    '{"state":"NoProfile","clients":0}'
  } else {
    $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
    '{"state":"' + $manager.TetheringOperationalState + '","clients":' + $manager.ClientCount + '}'
  }
} catch {
  '{"state":"Error","clients":0}'
}
"""

_TETHER_TTL = 15.0
_TETHER_BUSY_TTL = 2.0
_tether_cache: dict = {"at": 0.0, "state": "", "clients": 0, "ttl": _TETHER_TTL}


def _tether_info(timeout: float = 10.0, force: bool = False) -> dict:
    """问 WinRT：热点开没开、几台设备连着（15 秒缓存，切换中只缓存 2 秒）。"""
    now = time.monotonic()
    if not force and _tether_cache["at"] and now - _tether_cache["at"] < float(
        _tether_cache.get("ttl") or _TETHER_TTL
    ):
        return dict(_tether_cache)

    import base64
    import subprocess

    encoded = base64.b64encode(_TETHER_QUERY.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return {"at": 0.0, "state": "", "clients": 0}

    text = _decode_console(proc.stdout or b"") + "\n" + _decode_console(proc.stderr or b"")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        state = str(data.get("state") or "")
        _tether_cache.update(
            at=time.monotonic(),
            state=state,
            clients=int(data.get("clients") or 0),
            ttl=_TETHER_BUSY_TTL if state == "InTransition" else _TETHER_TTL,
        )
        return dict(_tether_cache)
    return {"at": 0.0, "state": "", "clients": 0, "ttl": _TETHER_TTL}


def hotspot_state(use_winrt: bool = True) -> dict:
    """热点是否开启：192.168.137.x 有没有本机地址，再用 WinRT 交叉确认。"""
    ip, adapter = _hotspot_ip()
    info = _tether_info() if use_winrt else {"state": "", "clients": 0}
    state = str(info.get("state") or "")
    # 有 IP 但 WinRT 说关着，多半是缓存放早了（刚开启），强制重问一次
    if use_winrt and ip and state in ("Off", "InTransition", ""):
        info = _tether_info(force=True)
        state = str(info.get("state") or "")
    clients = int(info.get("clients") or 0)
    on = bool(ip) or state == "On"
    if ip:
        source = "ip"
    elif state == "On":
        source = "winrt"
    elif state == "Off":
        source = "winrt-off"
    elif state == "InTransition":
        source = "winrt-busy"
    else:
        source = ""
    return {
        "on": on,
        "ip": ip,
        "adapter": adapter,
        "source": source,
        "state": state,
        "clients": clients,
    }


# --------------------------------------------------------------------- SSID
class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wt.DWORD),
        ("Data2", wt.WORD),
        ("Data3", wt.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", _GUID),
        ("strInterfaceDescription", wt.WCHAR * 256),
        ("isState", wt.DWORD),
    ]


class _WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wt.DWORD),
        ("dwIndex", wt.DWORD),
        ("InterfaceInfo", _WLAN_INTERFACE_INFO * 1),
    ]


class _DOT11_SSID(ctypes.Structure):
    _fields_ = [
        ("uSSIDLength", wt.ULONG),
        ("ucSSID", ctypes.c_ubyte * 32),
    ]


class _WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("isState", wt.DWORD),
        ("wlanConnectionMode", wt.DWORD),
        ("strProfileName", wt.WCHAR * 256),
        ("dot11Ssid", _DOT11_SSID),
        ("dot11BssType", wt.DWORD),
        ("uNumberOfBssids", wt.ULONG),
        ("bNetworkConnectable", wt.BOOL),
        ("wlanNotConnectableReason", wt.DWORD),
        ("uNumberOfPhyTypes", wt.ULONG),
        ("dot11PhyTypes", wt.DWORD * 8),
        ("bMorePhyTypes", wt.BOOL),
        ("wlanSignalQuality", wt.ULONG),
        ("bSecurityEnabled", wt.BOOL),
        ("dot11DefaultAuthAlgorithm", wt.DWORD),
        ("dot11DefaultCipherAlgorithm", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("dwReserved", wt.DWORD),
    ]


_WLAN_INTF_OPCODE_CURRENT_CONNECTION = 7
_WLAN_OPCODE_VALUE_TYPE = 0x7FFFFFFF
_WLAN_CONNECTED = 1

_wlanapi = None
_ssid_cache: dict = {"at": 0.0, "value": ""}
_SSID_TTL = 4.0


def _wlanapi_lib():
    """惰性加载 wlanapi.dll；拿不到就返回 None（交给 netsh 兜底）。"""
    global _wlanapi
    if _wlanapi is False:
        return None
    if _wlanapi is None:
        try:
            lib = ctypes.WinDLL("wlanapi", use_last_error=True)
        except OSError:
            _wlanapi = False
            return None
        lib.WlanOpenHandle.argtypes = [
            wt.DWORD, ctypes.c_void_p, ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.HANDLE),
        ]
        lib.WlanOpenHandle.restype = wt.DWORD
        lib.WlanEnumInterfaces.argtypes = [
            wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)),
        ]
        lib.WlanEnumInterfaces.restype = wt.DWORD
        lib.WlanQueryInterface.argtypes = [
            wt.HANDLE, ctypes.POINTER(_GUID), wt.DWORD, ctypes.c_void_p,
            ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD),
        ]
        lib.WlanQueryInterface.restype = wt.DWORD
        lib.WlanFreeMemory.argtypes = [ctypes.c_void_p]
        lib.WlanFreeMemory.restype = None
        lib.WlanCloseHandle.argtypes = [wt.HANDLE, ctypes.c_void_p]
        lib.WlanCloseHandle.restype = wt.DWORD
        _wlanapi = lib
    return _wlanapi


def _ssid_from_wlanapi() -> tuple[bool, str]:
    """返回 (接口是否可用, 当前 SSID)。没有无线接口时 available=False，让调用方兜底。"""
    lib = _wlanapi_lib()
    if lib is None:
        return False, ""

    negotiated = wt.DWORD()
    handle = wt.HANDLE()
    if lib.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle)) != 0:
        return False, ""
    try:
        info_ptr = ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)()
        if lib.WlanEnumInterfaces(handle, None, ctypes.byref(info_ptr)) != 0:
            return False, ""
        try:
            count = int(info_ptr.contents.dwNumberOfItems)
            if count <= 0:
                return False, ""
            connected = False
            ssid = ""
            for index in range(count):
                info = info_ptr.contents.InterfaceInfo[index]
                data = ctypes.c_void_p()
                size = wt.DWORD()
                value_type = wt.DWORD(_WLAN_OPCODE_VALUE_TYPE)
                rc = lib.WlanQueryInterface(
                    handle,
                    ctypes.byref(info.InterfaceGuid),
                    _WLAN_INTF_OPCODE_CURRENT_CONNECTION,
                    None,
                    ctypes.byref(size),
                    ctypes.byref(data),
                    ctypes.byref(value_type),
                )
                if rc != 0 or not data:
                    continue
                try:
                    attrs = ctypes.cast(data, ctypes.POINTER(_WLAN_CONNECTION_ATTRIBUTES)).contents
                    if int(attrs.isState) != _WLAN_CONNECTED:
                        continue
                    length = min(int(attrs.dot11Ssid.uSSIDLength), 32)
                    if length <= 0:
                        continue
                    raw = bytes(attrs.dot11Ssid.ucSSID[:length])
                    connected = True
                    ssid = raw.decode("utf-8", "replace").strip("\x00").strip()
                    if ssid:
                        return True, ssid
                finally:
                    lib.WlanFreeMemory(data)
            # 有无线网卡但没连上
            return True, ssid if connected else ""
        finally:
            lib.WlanFreeMemory(info_ptr)
    finally:
        lib.WlanCloseHandle(handle, None)


def _ssid_from_netsh() -> str:
    """兜底：WlanAPI 不可用时调 netsh（会临时起一个进程，所以只在必要时用）。"""
    import subprocess

    try:
        completed = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            timeout=6,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    text = _decode_console(completed.stdout)
    ssid, state = "", ""
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key == "SSID":
            ssid = value
        elif key in ("状态", "State"):
            state = value
        if ssid:
            break
    if not ssid:
        return ""
    if state and state.lower() not in ("connected", "已连接"):
        return ""
    return ssid


def wifi_ssid() -> str:
    """当前 Wi-Fi 的 SSID；没连 Wi-Fi 或读不到时返回空串。结果缓存 4 秒。"""
    now = time.monotonic()
    if now - _ssid_cache["at"] < _SSID_TTL:
        return _ssid_cache["value"]

    available, ssid = _ssid_from_wlanapi()
    value = ssid if available else _ssid_from_netsh()
    _ssid_cache.update(at=now, value=value)
    return value


def reset_caches() -> None:
    """系统睡眠唤醒、切换网卡之后调用：丢掉缓存，下一次重新读一遍。"""
    _ssid_cache.update(at=0.0, value="")
    _tether_cache.update(at=0.0, state="", clients=0)


def _decode_console(raw: bytes) -> str:
    """控制台程序输出可能是 UTF-8、GBK 或系统 ANSI，逐个试。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk", "cp936", "mbcs"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# ------------------------------------------------------------ Portal / 在线
_MAX_BODY = 256 * 1024


def _split_url(url: str) -> tuple[str, str, int, str]:
    """(scheme, host, port, path)。只认 http/https，够用且不牵进 urllib。"""
    text = (url or "").strip()
    if not text:
        raise ValueError("empty url")
    if "://" not in text:
        text = "http://" + text
    scheme, _, rest = text.partition("://")
    scheme = scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {scheme}")
    slash = rest.find("/")
    host_port = rest if slash < 0 else rest[:slash]
    path = "/" if slash < 0 else rest[slash:]
    if not host_port:
        raise ValueError("empty host")

    if host_port.startswith("["):  # [::1]:801
        close = host_port.find("]")
        if close < 0:
            raise ValueError("bad ipv6 host")
        host = host_port[1:close]
        tail = host_port[close + 1 :]
        port_text = tail[1:] if tail.startswith(":") else ""
    elif ":" in host_port:
        host, _, port_text = host_port.partition(":")
    else:
        host, port_text = host_port, ""

    if port_text:
        port = int(port_text)
        if not 0 < port < 65536:
            raise ValueError("bad port")
    else:
        port = 443 if scheme == "https" else 80
    return scheme, host, port, path


def _absolute_url(base: str, location: str) -> str:
    if "://" in location:
        return location
    scheme, host, port, path = _split_url(base)
    host_port = host if port in (80, 443) else f"{host}:{port}"
    if location.startswith("/"):
        return f"{scheme}://{host_port}{location}"
    directory = path.rsplit("/", 1)[0]
    return f"{scheme}://{host_port}{directory}/{location}"


def _recv(sock: socket.socket, size: int, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("http deadline")
    sock.settimeout(remaining)
    return sock.recv(size)


def _read_chunked(sock: socket.socket, buffer: bytes, deadline: float) -> bytes:
    body = bytearray()
    data = bytearray(buffer)
    while len(body) < _MAX_BODY:
        while b"\r\n" not in data:
            chunk = _recv(sock, 4096, deadline)
            if not chunk:
                return bytes(body)
            data += chunk
        line, _, data = data.partition(b"\r\n")
        try:
            size = int(line.split(b";")[0].strip() or b"0", 16)
        except ValueError:
            return bytes(body)
        if size == 0:
            return bytes(body)
        while len(data) < size + 2:
            chunk = _recv(sock, 4096, deadline)
            if not chunk:
                return bytes(body)
            data += chunk
        body += data[:size]
        data = data[size + 2 :]
    return bytes(body)


def _read_response(
    sock: socket.socket, deadline: float
) -> tuple[int, dict[str, str], bytes]:
    buffer = b""
    while b"\r\n\r\n" not in buffer and len(buffer) < 64 * 1024:
        chunk = _recv(sock, 4096, deadline)
        if not chunk:
            break
        buffer += chunk
    head, sep, rest = buffer.partition(b"\r\n\r\n")
    if not sep:
        return 0, {}, b""

    lines = head.split(b"\r\n")
    parts = lines[0].decode("latin-1", "replace").split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, _, value = line.partition(b":")
        if key.strip():
            headers[key.decode("latin-1", "replace").strip().lower()] = value.decode(
                "latin-1", "replace"
            ).strip()

    if headers.get("transfer-encoding", "").lower().endswith("chunked"):
        return status, headers, _read_chunked(sock, rest, deadline)

    length_text = headers.get("content-length", "")
    if length_text.isdigit():
        wanted = min(int(length_text), _MAX_BODY)
        body = bytearray(rest[:wanted])
        while len(body) < wanted:
            chunk = _recv(sock, min(16384, wanted - len(body)), deadline)
            if not chunk:
                break
            body += chunk
        return status, headers, bytes(body[:wanted])

    body = bytearray(rest[:_MAX_BODY])
    while len(body) < _MAX_BODY:
        chunk = _recv(sock, 16384, deadline)
        if not chunk:
            break
        body += chunk
    return status, headers, bytes(body[:_MAX_BODY])


def _https_get(url: str, timeout: float) -> tuple[bool, int, bytes]:
    """https 才回退到 urllib（本项目的校园网地址都是 http，留着兜底）。"""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "HotspotAutoDesktop/1.0"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return True, int(response.status), response.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也算“可达”
        try:
            body = exc.read(64 * 1024)
        except Exception:  # noqa: BLE001
            body = b""
        return True, int(exc.code), body
    except (urllib.error.URLError, OSError, ValueError):
        return False, 0, b""


def _http_get(url: str, timeout: float, redirects: int = 3) -> tuple[bool, int, bytes]:
    try:
        scheme, host, port, path = _split_url(url)
    except (ValueError, TypeError):
        return False, 0, b""
    if scheme == "https":
        return _https_get(url, timeout)

    deadline = time.monotonic() + max(0.5, timeout)
    payload = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "User-Agent: HotspotAutoDesktop/1.0\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1", "replace")
    try:
        with socket.create_connection((host, port), timeout=max(0.5, timeout)) as sock:
            sock.sendall(payload)
            status, headers, body = _read_response(sock, deadline)
    except (OSError, ValueError):
        return False, 0, b""

    if status in (301, 302, 303, 307, 308) and redirects > 0:
        location = headers.get("location", "")
        if location:
            try:
                return _http_get(_absolute_url(url, location), timeout, redirects - 1)
            except ValueError:
                return True, status, body
    return status > 0, status, body


def http_get(url: str, timeout: float = 6.0) -> tuple[bool, int, bytes]:
    """公开的 GET：返回 (是否拿到响应, 状态码, 正文)。给 actions 拨号用。"""
    return _http_get(url, timeout)


def portal_state(portal: str, timeout: float = 2.5) -> dict:
    started = time.perf_counter()
    ok, status, _ = _http_get(portal or "", timeout)
    elapsed = int((time.perf_counter() - started) * 1000)
    return {"ok": ok, "status": status, "ms": elapsed if ok else None}


def _parse_jsonp(text: str):
    payload = text.strip()
    start, end = payload.find("("), payload.rfind(")")
    if start >= 0 and end > start:
        payload = payload[start + 1 : end]
    payload = payload.strip().rstrip(";").strip()
    if not payload:
        return None
    return json.loads(payload)


def internet_state(timeout: float = 3.0) -> bool:
    """能拿到 204 就说明已经认证上网（校园网没登录时会被强制门户拦下）。"""
    for url in ("http://connect.rom.miui.com/generate_204", "http://www.gstatic.com/generate_204"):
        ok, status, _ = _http_get(url, timeout)
        if ok and status == 204:
            return True
    return False


def online_state(api_base: str, local_ip: str, timeout: float = 1.2, attempts: int = 2) -> dict:
    """查 /eportal/portal/online_list，用本机 IP 是否在列表里判断在线（与 campus-login.ps1 同逻辑）。

    校园 Portal 的这个接口会间歇性“收下请求但不回包”（实测约一半概率卡到超时），
    所以这里用短超时 + 少量重试；仍然失败时用 204 探测兜底，
    因为“能上网”本身就等价于“已经通过认证”。
    """
    url = f"{api_base.rstrip('/')}/eportal/portal/online_list"
    ok, status, body = False, 0, b""
    tries = 0
    for _ in range(max(1, attempts)):
        tries += 1
        ok, status, body = _http_get(url, timeout)
        if ok:
            break
    if not ok:
        if internet_state():
            return {
                "ok": True,
                "online": True,
                "count": 0,
                "matched_ip": local_ip,
                "error": "",
                "source": "net",
                "url": url,
                "attempts": tries,
            }
        reason = "接口超时" if tries > 1 else "接口不可达"
        return {
            "ok": False,
            "online": False,
            "count": 0,
            "matched_ip": "",
            "error": f"{reason}（{tries} 次尝试）" if tries > 1 else reason,
            "source": "none",
            "url": url,
            "attempts": tries,
        }

    text = _decode_console(body)
    try:
        data = _parse_jsonp(text)
    except (json.JSONDecodeError, ValueError):
        if internet_state():
            return {
                "ok": True,
                "online": True,
                "count": 0,
                "matched_ip": local_ip,
                "error": "",
                "source": "net",
                "url": url,
                "attempts": tries,
            }
        return {"ok": False, "online": False, "count": 0, "matched_ip": "", "error": "返回不是 JSON", "source": "none", "url": url}

    items = []
    if isinstance(data, dict):
        raw_items = data.get("list") or data.get("data") or []
        if isinstance(raw_items, list):
            items = raw_items
    matched = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        ip = str(item.get("online_ip") or item.get("ip") or "").strip()
        if local_ip and ip == local_ip:
            matched = ip
            break
    if not matched and internet_state():
        # 列表能查、但里面没有本机 IP，可这台机器明明是通的。
        # 这个接口的列表并不可靠（实测会出现 total=0 却已经在线的情况），
        # 所以"能不能上网"优先级高于列表。
        return {
            "ok": True,
            "online": True,
            "count": len(items),
            "matched_ip": local_ip,
            "error": "",
            "source": "net",
            "url": url,
            "http_status": status,
            "attempts": tries,
        }
    return {
        "ok": True,
        "online": bool(matched),
        "count": len(items),
        "matched_ip": matched,
        "error": "",
        "source": "list",
        "url": url,
        "http_status": status,
        "attempts": tries,
    }


# ------------------------------------------------------------------ 汇总快照
def snapshot(cfg: dict, include_online: bool = True) -> dict:
    from . import config as cfgm  # 局部导入，避免循环依赖

    net = local_info()
    ssid = wifi_ssid()
    hotspot = hotspot_state()
    portal = portal_state(str(cfg.get("portal", "") or cfgm.DEFAULT_CONFIG["portal"]))

    online = {"ok": False, "online": False, "count": 0, "matched_ip": "", "error": "未检测"}
    if include_online:
        online = online_state(cfgm.api_base(cfg), net["ip"])

    return {
        "ts": time.time(),
        "net": net,
        "ssid": ssid,
        "hotspot": hotspot,
        "portal": portal,
        "online": online,
        "configured": cfgm.is_configured(cfg),
    }
