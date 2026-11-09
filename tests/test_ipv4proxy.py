import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from paperbot.ipv4proxy import IPv4Proxy


def echo_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def run():
        conn, _ = srv.accept()
        while data := conn.recv(1024):
            conn.sendall(data)
        conn.close()
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def ask(proxy_port, raw: bytes, then: bytes | None = None) -> bytes:
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout=5)
    s.sendall(raw)
    reply = s.recv(4096)
    if then is not None:
        s.sendall(then)
        reply += b"|" + s.recv(4096)
    s.close()
    return reply


def test_connect_tunnel_uses_ipv4_resolver():
    port = echo_server()
    asked = []

    def resolver(host, p):
        asked.append(host)
        return [("127.0.0.1", port)]

    with IPv4Proxy(resolver=resolver, allow_private=True) as proxy:
        reply = ask(proxy.port, b"CONNECT publisher.example:443 HTTP/1.1\r\n\r\n", b"ping")
    assert reply.startswith(b"HTTP/1.1 200") and reply.endswith(b"|ping")
    assert asked == ["publisher.example"]


def test_private_addresses_refused():
    with IPv4Proxy(resolver=lambda h, p: [("10.1.2.3", p)]) as proxy:
        reply = ask(proxy.port, b"CONNECT intranet.example:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 403")


def test_plain_http_is_forwarded_in_origin_form():
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["host"] = self.headers["Host"]
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    port = srv.server_address[1]
    with IPv4Proxy(resolver=lambda h, p: [("127.0.0.1", port)], allow_private=True) as proxy:
        reply = ask(proxy.port, b"GET http://site.example/a?b=1 HTTP/1.1\r\nHost: site.example\r\n"
                                b"Proxy-Connection: keep-alive\r\n\r\n")
    srv.server_close()
    assert reply.startswith(b"HTTP/1.0 200") and reply.endswith(b"ok")
    assert seen == {"path": "/a?b=1", "host": "site.example"}
