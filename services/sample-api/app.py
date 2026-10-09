"""環境 (dev・prod) ごとに動かすサンプルの API。Backstage の環境ごとのタブの確かめ役 (docs/cluster/environments.md)。

標準ライブラリだけで書き、イメージは作らない。kustomize がこのファイルと openapi.yaml を ConfigMap にし、
python の公式イメージがそれを読んで動かす (kustomization.yaml)。
環境の名前は Pod の namespace (Downward API の APP_ENV) から取る。dev と prod で同じ manifest を使う。
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "0.1.0"
OPENAPI = Path(__file__).with_name("openapi.yaml")


def route(path: str, query: str, env: str) -> tuple[int, str, bytes]:
    """パスとクエリから (状態, Content-Type, 本文) を返す。HTTP から切り離して試験する"""
    if path == "/healthz":
        return 200, "text/plain", b"ok"
    if path == "/openapi.yaml":
        return 200, "application/yaml", OPENAPI.read_bytes()
    if path == "/info":
        return _json(200, {"service": "sample-api", "environment": env, "version": VERSION})
    if path == "/hello":
        name = parse_qs(query).get("name", ["world"])[0]
        return _json(200, {"message": f"hello, {name}", "environment": env})
    return _json(404, {"error": "not found"})


def _json(status: int, body: dict) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(body, ensure_ascii=False).encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        status, ctype, body = route(url.path, url.query, os.environ.get("APP_ENV", "local"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # /healthz の probe で埋まるので、アクセスログは出さない
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
