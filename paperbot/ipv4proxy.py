"""A tiny local proxy that makes the browser use IPv4 only.

The institute VPN (L2TP) carries IPv4 only. Without this, publishers that also have IPv6
addresses (IEEE, Wiley, Taylor & Francis, doi.org, ...) are reached over the home
connection's IPv6, around the VPN, so they never see the institute's address. The browser is
pointed at this proxy, which resolves every host to IPv4 and refuses private / intranet
addresses (the VPN puts this PC inside the institute network). Chromium also drops QUIC when
a proxy is set, so no UDP traffic slips around it.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
IDLE_TIMEOUT_S = 300


class Blocked(Exception):
    pass


def _ipv4_addresses(host: str, port: int) -> list[tuple[str, int]]:
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][:2] for info in infos))


class IPv4Proxy:
    def __init__(self, resolver=_ipv4_addresses, allow_private: bool = False):
        self.resolver = resolver
        self.allow_private = allow_private
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(128)
        self.port = self.server.getsockname()[1]
        self._closed = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "IPv4Proxy":
        threading.Thread(target=self._serve, name="ipv4proxy", daemon=True).start()
        return self

    def stop(self) -> None:
        self._closed = True
        try:
            self.server.close()
        except OSError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # ------------------------------------------------------------------
    def _serve(self) -> None:
        while not self._closed:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _connect(self, host: str, port: int) -> socket.socket:
        addrs = self.resolver(host, port)
        if not addrs:
            raise OSError(f"{host} has no IPv4 address")
        if not self.allow_private and not all(
                ipaddress.ip_address(a[0]).is_global for a in addrs):
            raise Blocked(host)
        last: OSError | None = None
        for addr in addrs:
            try:
                return socket.create_connection(addr, timeout=30)
            except OSError as exc:
                last = exc
        raise last or OSError(f"cannot reach {host}")

    def _handle(self, client: socket.socket) -> None:
        upstream = None
        try:
            client.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(65536)
                if not chunk or len(head) > 65536:
                    return
                head += chunk
            block, _, rest = head.partition(b"\r\n\r\n")
            lines = block.split(b"\r\n")
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream = self._connect(host.strip("[]"), int(port or 443))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:  # plain http:// request in absolute form
                u = urlsplit(target)
                if u.scheme != "http" or not u.hostname:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    return
                upstream = self._connect(u.hostname, u.port or 80)
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                headers = [h for h in lines[1:] if not h.lower().startswith(
                    (b"proxy-connection:", b"proxy-authorization:", b"connection:"))]
                upstream.sendall(b"\r\n".join(
                    [f"{method} {path} {version}".encode("latin-1"), *headers,
                     b"Connection: close"]) + b"\r\n\r\n" + rest)
            self._pipe(client, upstream)
        except Blocked as exc:
            log.warning("proxy refused private/intranet address for %s", exc)
            _send_quietly(client, b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
        except (OSError, ValueError) as exc:
            log.debug("proxy: %s", exc)
            if upstream is None:
                _send_quietly(client, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        finally:
            for s in (client, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    @staticmethod
    def _pipe(a: socket.socket, b: socket.socket) -> None:
        a.settimeout(IDLE_TIMEOUT_S)
        b.settimeout(IDLE_TIMEOUT_S)
        t = threading.Thread(target=_relay, args=(a, b), daemon=True)
        t.start()
        _relay(b, a)
        t.join(timeout=IDLE_TIMEOUT_S)


def _relay(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _send_quietly(sock: socket.socket, data: bytes) -> None:
    try:
        sock.sendall(data)
    except OSError:
        pass
