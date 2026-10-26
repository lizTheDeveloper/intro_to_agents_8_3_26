from url_safety import FetchError, resolve_public
from urllib.parse import urlparse
import select
import socket
import threading

## A tiny local HTTP/CONNECT proxy for the headless browser. Chromium hands it the hostname, so the
## DNS lookup, the public-address check, and the connection all happen here against one address.
## That closes the gap where Chromium resolves a name a second time and gets a different, internal answer.

ALLOWED_PORTS = {80, 443, 8080, 8443}
MAX_HEADER_BYTES = 65536
IDLE_TIMEOUT_SECONDS = 60


class PublicOnlyProxy:
    def __init__(self):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.port = self.server.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self):
        self.closed = True
        self.server.close()

    def _accept_loop(self):
        while not self.closed:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        upstream = None
        try:
            client.settimeout(IDLE_TIMEOUT_SECONDS)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk or len(head) > MAX_HEADER_BYTES:
                    return
                head += chunk
            header_block, _, leftover = head.partition(b"\r\n\r\n")
            request_line, _, rest_headers = header_block.partition(b"\r\n")
            method, target, version = request_line.decode("latin-1").split(" ", 2)

            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                port = int(port)
            else:
                parsed = urlparse(target)
                host, port = parsed.hostname, parsed.port or 80
                if parsed.scheme != "http" or not host:
                    raise FetchError("not a proxyable request")
            if port not in ALLOWED_PORTS:
                raise FetchError(f"port {port} not allowed")

            address = resolve_public(host.strip("[]"))
            upstream = socket.create_connection((address, port), timeout=20)
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
                upstream.sendall(f"{method} {path} {version}\r\n".encode("latin-1") + rest_headers + b"\r\n\r\n" + leftover)
            self._relay(client, upstream)
        except (FetchError, ValueError, OSError):
            try:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
        finally:
            client.close()
            if upstream:
                upstream.close()

    @staticmethod
    def _relay(a, b):
        sockets = [a, b]
        while True:
            readable, _, _ = select.select(sockets, [], [], IDLE_TIMEOUT_SECONDS)
            if not readable:
                return
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (b if source is a else a).sendall(data)
