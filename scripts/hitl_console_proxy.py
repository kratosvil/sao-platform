#!/usr/bin/env python3
"""
Proxy local para navegar la consola HITL (Modulo 10) con clicks normales.

ModHeader fue removida de las stores de Chrome/Firefox -- esta es la
alternativa sin instalar nada en el navegador: un servidor local que
inyecta el header "Authorization: Bearer <token>" en cada request antes
de reenviarlo al API Gateway real. Los links y forms que sirve la consola
son todos relativos (/hitl/review/{token}), asi que navegar y aprobar/
rechazar/ajustar parametros funciona igual que pegandole directo a la API.

Uso:
  export HITL_CONSOLE_TOKEN=$(cd ../terraform && terraform output -raw hitl_console_token)
  export HITL_API_URL=$(cd ../terraform && terraform output -raw hitl_api_url)
  python3 hitl_console_proxy.py
  # abrir http://localhost:8888/hitl/pending en el navegador y dejar corriendo
"""
import os
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.environ.get("HITL_CONSOLE_TOKEN")
UPSTREAM = os.environ.get("HITL_API_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8888"))

if not TOKEN or not UPSTREAM:
    sys.exit(
        "Definir HITL_CONSOLE_TOKEN y HITL_API_URL como variables de entorno "
        "antes de correr esto (ver el docstring de este archivo)."
    )

# hop-by-hop headers que no hay que reenviar tal cual entre proxy y cliente
_SKIP_HEADERS = {"content-length", "transfer-encoding", "connection"}


class ProxyHandler(BaseHTTPRequestHandler):
    def _proxy(self):
        url = UPSTREAM + self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None

        req = urllib.request.Request(url, data=body, method=self.command)
        req.add_header("Authorization", f"Bearer {TOKEN}")
        ctype = self.headers.get("Content-Type")
        if body and ctype:
            req.add_header("Content-Type", ctype)

        try:
            with urllib.request.urlopen(req) as resp:
                self._send(resp.status, resp.getheaders(), resp.read())
        except urllib.error.HTTPError as e:
            self._send(e.code, e.headers.items(), e.read())

    def _send(self, status, headers, data):
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in _SKIP_HEADERS:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def log_message(self, fmt, *args):
        print("[proxy]", fmt % args)


if __name__ == "__main__":
    print(f"Proxy HITL escuchando en http://localhost:{PORT} -> {UPSTREAM}")
    print("Ctrl+C para cortar.")
    HTTPServer(("127.0.0.1", PORT), ProxyHandler).serve_forever()
