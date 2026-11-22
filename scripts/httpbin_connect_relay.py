"""Opaque TCP relay through CONNECT; TLS remains verified by the test client."""
import select
import socket
import socketserver
import threading


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        port = self.server.server_address[1]
        local = self.server.server_address == ('127.0.0.2', 80)
        host = {'127.0.0.3': 'www.google.co.uk', '127.0.0.4': 'www.google.com'}.get(self.server.server_address[0], 'httpbin.org')
        with socket.create_connection(('127.0.0.1', 18081 if local else 7897), timeout=20) as upstream:
            if not local:
                upstream.sendall((f'CONNECT {host}:{port} HTTP/1.1\r\n'
                                  f'Host: {host}:{port}\r\n\r\n').encode('ascii'))
            header = bytearray()
            while not local and not header.endswith(b'\r\n\r\n'):
                part = upstream.recv(1)
                if not part or len(header) > 16384:
                    raise ConnectionError('Incomplete proxy handshake')
                header.extend(part)
            if not local and header.split(b'\r\n', 1)[0].split()[1] != b'200':
                raise ConnectionError(bytes(header))
            upstream.settimeout(None)
            peers = [self.request, upstream]
            while True:
                ready, _, _ = select.select(peers, [], [], 120)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


servers = [Server((address, port), Handler) for address in ['127.0.0.2', '127.0.0.3', '127.0.0.4'] for port in (80, 443)]
for server in servers:
    threading.Thread(target=server.serve_forever, daemon=True).start()
print('CONNECT relays listening on 127.0.0.2:80 and :443', flush=True)
threading.Event().wait()
