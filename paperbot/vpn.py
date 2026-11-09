"""Turning the Windows VPN connection on and off with rasdial (built into Windows)."""
from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)

RASDIAL_ERRORS = {
    "691": "the VPN rejected the username/password (if Windows' saved password changed, "
           "re-enter it in Settings > Network & internet > VPN)",
    "-1": "Windows has no saved sign-in for this VPN - save it in Settings > Network & "
          "internet > VPN > Advanced options, or run `paperbot set-password vpn`",
    "703": "Windows has no saved VPN password - run `paperbot set-password vpn`",
    "800": "couldn't reach the VPN server",
    "809": "the network between this PC and the VPN server blocked the connection",
    "868": "the VPN server name couldn't be resolved (no internet?)",
    "623": "no VPN connection with this name - check [vpn] name in config.toml",
    "720": "the VPN connected but no network protocol could be set up",
}


class VpnError(Exception):
    pass


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)


def _text(cp: subprocess.CompletedProcess) -> str:
    raw = (cp.stdout or b"") + (cp.stderr or b"")
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


def _error_code(cp: subprocess.CompletedProcess) -> str:
    """rasdial exits with the RAS error number and also prints 'Remote Access error 691'."""
    import re
    m = re.search(r"error (\d{3,4})", _text(cp), re.I)
    return m.group(1) if m else str(cp.returncode)


def saved_signin_present(name: str) -> bool:
    """Does Windows hold a saved password for this VPN connection? (Nothing is revealed:
    RasGetCredentials only reports which parts are saved.)"""
    import ctypes
    from ctypes import wintypes

    class RASCREDENTIALSW(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("dwMask", wintypes.DWORD),
                    ("szUserName", wintypes.WCHAR * 257), ("szPassword", wintypes.WCHAR * 257),
                    ("szDomain", wintypes.WCHAR * 16)]

    cred = RASCREDENTIALSW()
    cred.dwSize = ctypes.sizeof(cred)
    cred.dwMask = 0x1 | 0x2
    try:
        rc = ctypes.WinDLL("rasapi32").RasGetCredentialsW(None, name, ctypes.byref(cred))
        return rc == 0 and bool(cred.dwMask & 0x2)
    finally:
        ctypes.memset(ctypes.addressof(cred), 0, ctypes.sizeof(cred))


def ras_dial_saved(name: str) -> int:
    """Dial `name` with the sign-in Windows saved for it - what the Settings app's Connect
    button does. (`rasdial name` alone does NOT use those saved credentials.)
    The password never leaves Windows: RasGetCredentials hands out an opaque handle that
    RasDial accepts in its place. Returns 0 on success, a RAS error code, or -1 when Windows
    has no saved password for the entry."""
    import ctypes
    from ctypes import wintypes

    class RASCREDENTIALSW(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("dwMask", wintypes.DWORD),
                    ("szUserName", wintypes.WCHAR * 257), ("szPassword", wintypes.WCHAR * 257),
                    ("szDomain", wintypes.WCHAR * 16)]

    base = [("dwSize", wintypes.DWORD), ("szEntryName", wintypes.WCHAR * 257),
            ("szPhoneNumber", wintypes.WCHAR * 129), ("szCallbackNumber", wintypes.WCHAR * 129),
            ("szUserName", wintypes.WCHAR * 257), ("szPassword", wintypes.WCHAR * 257),
            ("szDomain", wintypes.WCHAR * 16), ("dwSubEntry", wintypes.DWORD),
            ("dwCallbackId", ctypes.c_size_t)]

    class DialParams(ctypes.Structure):          # RASDIALPARAMSW, WINVER 0x401 layout
        _fields_ = base

    class DialParams7(ctypes.Structure):         # WINVER 0x601 layout (+ dwIfIndex)
        _fields_ = base + [("dwIfIndex", wintypes.DWORD)]

    ras = ctypes.WinDLL("rasapi32")
    ras.RasGetCredentialsW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    ras.RasGetCredentialsW.restype = wintypes.DWORD
    ras.RasDialW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE)]
    ras.RasDialW.restype = wintypes.DWORD
    ras.RasHangUpW.argtypes = [wintypes.HANDLE]

    cred = RASCREDENTIALSW()
    cred.dwSize = ctypes.sizeof(cred)
    cred.dwMask = 0x1 | 0x2 | 0x4  # user name, password, domain
    rc = ras.RasGetCredentialsW(None, name, ctypes.byref(cred))
    if rc:
        return rc
    if not cred.dwMask & 0x2:
        return -1
    try:
        for layout in (DialParams, DialParams7):
            p = layout()
            p.dwSize = ctypes.sizeof(p)
            p.szEntryName = name
            for field in ("szUserName", "szPassword", "szDomain"):  # copy raw, incl. the handle
                ctypes.memmove(ctypes.addressof(p) + getattr(layout, field).offset,
                               ctypes.addressof(cred) + getattr(RASCREDENTIALSW, field).offset,
                               getattr(RASCREDENTIALSW, field).size)
            h = wintypes.HANDLE(0)
            rc = ras.RasDialW(None, None, ctypes.byref(p), 0, None, ctypes.byref(h))
            ctypes.memset(ctypes.addressof(p), 0, ctypes.sizeof(p))
            if rc and h.value:
                ras.RasHangUpW(h)  # a failed dial still holds resources
            if rc != 632:  # 632 = ERROR_INVALID_SIZE: try the newer struct layout
                return rc
        return rc
    finally:
        ctypes.memset(ctypes.addressof(cred), 0, ctypes.sizeof(cred))


def local_source_ip(probe: str = "8.8.8.8") -> str | None:
    """The local IPv4 address Windows would send internet traffic from. A UDP connect() only
    consults the routing table; no packet is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((probe, 53))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class Vpn:
    def __init__(self, name: str, timeout_s: float = 90, runner=_run,
                 credentials=lambda: None, dialer=None, source_ip=None,
                 route_timeout_s: float = 45):
        self.name = name
        self.timeout_s = timeout_s
        self.run = runner
        self.credentials = credentials  # -> (username, password) from `set-password vpn`, or None
        self.dialer = dialer            # ras_dial_saved in production; None in tests
        self.source_ip = source_ip      # local_source_ip in production; None skips the check
        self.route_timeout_s = route_timeout_s

    def is_connected(self) -> bool:
        out = _text(self.run(["rasdial"], 30))
        return any(line.strip().lower() == self.name.lower() for line in out.splitlines())

    def connect(self) -> None:
        log.info("connecting VPN %s", self.name)
        home_ip = self.source_ip() if self.source_ip else None
        if self.dialer is not None:
            code = str(self.dialer(self.name))  # Windows' saved sign-in
            detail = ""
        else:
            cp = self.run(["rasdial", self.name], self.timeout_s)
            code = "0" if cp.returncode == 0 else _error_code(cp)
            detail = _text(cp).strip()[-300:]
        if code in ("703", "691", "-1"):
            creds = self.credentials()  # fall back to credentials stored with PaperBot
            if creds:
                cp = self.run(["rasdial", self.name, creds[0], creds[1]], self.timeout_s)
                code = "0" if cp.returncode == 0 else _error_code(cp)
                detail = _text(cp).strip()[-300:]
        if code != "0":
            raise VpnError(f"VPN connect failed (error {code}): "
                           f"{RASDIAL_ERRORS.get(code, detail or 'see Windows RAS error codes')}")
        for _ in range(10):
            if self.is_connected():
                break
            time.sleep(1)
        else:
            raise VpnError("rasdial reported success but the connection isn't listed")
        self._wait_for_route(home_ip)

    def _wait_for_route(self, home_ip: str | None) -> None:
        """Windows can keep using the home connection for 10+ s after the VPN is up; anything
        fetched in that gap would reach publishers from the home address."""
        if not self.source_ip:
            time.sleep(2)
            return
        deadline = time.monotonic() + self.route_timeout_s
        while time.monotonic() < deadline:
            now = self.source_ip()
            if now and now != home_ip:
                log.info("internet traffic now leaves through the VPN (local address %s)", now)
                time.sleep(1)
                return
            time.sleep(0.5)
        self.disconnect()
        raise VpnError("the VPN connected, but Windows kept sending traffic through the home "
                       f"connection for {self.route_timeout_s:.0f}s - disconnected again")

    def disconnect(self) -> None:
        log.info("disconnecting VPN %s", self.name)
        self.run(["rasdial", self.name, "/disconnect"], 60)


@contextmanager
def vpn_session(vpn: Vpn, disconnect_when_done: bool = True):
    """Connect if needed; afterwards disconnect only if *we* connected it."""
    already = vpn.is_connected()
    if not already:
        vpn.connect()
    try:
        yield already
    finally:
        if not already and disconnect_when_done:
            try:
                vpn.disconnect()
            except Exception:  # noqa: BLE001 - never mask the real error
                log.exception("VPN disconnect failed")
