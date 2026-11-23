#!/usr/bin/env python3
"""Serve the public shell and a private campaign library to this computer."""
import argparse
import gzip
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

SITE = Path(__file__).resolve().parents[1] / "site"
DEFAULT_PACK = Path.home() / "Documents" / "StarCraft Local Data" / "repack"
ARCHIVE = re.compile(r"koprulu-[a-z0-9-]+\.mpq")
PREFIX = "/generated/repack/"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".wasm": "application/wasm", ".txt": "text/plain; charset=utf-8"}
CHUNK = 256 * 1024


def byte_range(header, size):
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not any(match.groups()):
        raise ValueError
    first, last = match.groups()
    start = int(first) if first else max(0, size - int(last))
    end = min(size - 1, int(last)) if first and last else size - 1
    if not 0 <= start <= end:
        raise ValueError
    return start, end


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.serve(False)

    def do_GET(self):
        self.serve(True)

    def serve(self, send_body):
        path = urlsplit(self.path).path
        if path == "/private-library.json":
            data = json.dumps([{"name": f.name, "size": f.stat().st_size}
                               for f in sorted(self.server.pack.glob("koprulu-*.mpq"))
                               if ARCHIVE.fullmatch(f.name) and f.is_file() and not f.is_symlink()]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if send_body:
                self.wfile.write(data)
            return
        engine = path == "/engine/koprulu.wasm"
        encoded = False
        if engine:
            f = SITE / "engine/koprulu.wasm.gz"
            # Serve the stored representation directly to browsers, or the identical raw module to other clients.
            encodings = [item.strip() for item in self.headers.get("Accept-Encoding", "").lower().split(",")]
            encoded = "gzip" in encodings
        elif path.startswith(PREFIX) and ARCHIVE.fullmatch(path[len(PREFIX):]):
            f = self.server.pack / path[len(PREFIX):]
        else:
            f = SITE / ("index.html" if path == "/" else path.lstrip("/"))
            if SITE not in f.resolve().parents or f.suffix not in TYPES:
                return self.send_error(404)
        if not f.is_file() or f.is_symlink():
            return self.send_error(404)
        size = f.stat().st_size
        if engine and not encoded:
            with f.open("rb") as stored:
                stored.seek(-4, 2)
                size = int.from_bytes(stored.read(4), "little")  # gzip ISIZE, 32-bit module
        start, end = 0, size - 1
        header = self.headers.get("Range")
        if header:
            try:
                start, end = byte_range(header, size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        self.send_response(206 if header else 200)
        self.send_header("Content-Type", "application/wasm" if engine else TYPES.get(f.suffix, "application/octet-stream"))
        if engine:
            self.send_header("Vary", "Accept-Encoding")
            if encoded:
                self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if header:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if send_body:
            with (gzip.open(f, "rb") if engine and not encoded else f.open("rb")) as stream:
                stream.seek(start)
                left = end - start + 1
                try:
                    while left:
                        block = stream.read(min(CHUNK, left))
                        if not block:
                            break
                        self.wfile.write(block)
                        left -= len(block)
                except (BrokenPipeError, ConnectionResetError):
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, default=DEFAULT_PACK)
    parser.add_argument("--port", type=int, default=8847)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.pack = args.pack.expanduser().resolve()
        print(url, flush=True)
        if args.open:
            threading.Timer(.25, lambda: webbrowser.open(url)).start()
        server.serve_forever()


if __name__ == "__main__":
    main()
