"""Verify the PWA and reproduce its release-scoped service worker for static hosting."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil

SITE = Path(__file__).resolve().parents[1] / "site"
WASM = "engine/koprulu.wasm"
INDEX = "library/index.json"
WORKER = "local-files.js"
SHELL = {
    "index.html", "launch.js", "manifest.webmanifest", "engine/koprulu.js", "engine/NOTICES.txt",
    WASM, INDEX, "ui/icon-180.png", "ui/icon-192.png", "ui/icon-512.png", "ui/progress.js",
    "ui/loading.png", "ui/sprites.png", "ui/font-metrics.js",
}


def verify_library():
    index = json.loads((SITE / INDEX).read_bytes())
    if index["version"] != 1:
        raise ValueError("Unsupported library version")
    archives = index["archives"]
    names = {archive["name"] for archive in archives}
    if len(names) != len(archives) or not {"koprulu-core.mpq", "koprulu-music.mpq", "koprulu-cinematics.mpq"} <= names:
        raise ValueError("Missing or duplicate campaign archives")
    parts = {}
    urls = set()
    for archive in archives:
        if not re.fullmatch(r"koprulu-[a-z0-9-]+\.mpq", archive["name"]):
            raise ValueError("Invalid archive name")
        if not archive["parts"] or archive["size"] != sum(part["size"] for part in archive["parts"]):
            raise ValueError("Invalid archive size: " + archive["name"])
        for part in archive["parts"]:
            url = part["url"]
            if not re.fullmatch(r"library/(?:[a-z0-9-]+/)*[a-f0-9]{64}(?:-gz)?\.bin", url):
                raise ValueError("Invalid asset URL: " + url)
            if not (0 < part["size"] <= 8 * 1024 * 1024) or part.get("encoding", "identity") not in {"identity", "gzip"}:
                raise ValueError("Invalid asset encoding or size: " + url)
            if part["hash"] in parts:
                if parts[part["hash"]] != part:
                    raise ValueError("Inconsistent repeated part: " + url)
                continue
            if url in urls:
                raise ValueError("Duplicate asset URL: " + url)
            data = (SITE / url).read_bytes()
            if part.get("encoding") == "gzip":
                data = gzip.decompress(data)
            if hashlib.sha256(data).hexdigest() != part["hash"] or len(data) != part["size"]:
                raise ValueError("Damaged part: " + url)
            parts[part["hash"]] = part
            urls.add(url)
    return urls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="create a new static site directory")
    args = parser.parse_args()
    urls = verify_library()
    shell_sources = {name: SITE / (name + ".gz" if name == WASM else name) for name in SHELL}
    expected = urls | {WORKER} | {p.relative_to(SITE).as_posix() for p in shell_sources.values()}
    actual = {f.relative_to(SITE).as_posix() for f in SITE.rglob("*") if f.is_file()}
    if expected != actual or any(f.is_symlink() for f in SITE.rglob("*")):
        raise ValueError(f"Unexpected or missing files: {sorted(actual ^ expected)}")
    shell = {name: path.read_bytes() for name, path in shell_sources.items()}
    shell[WASM] = gzip.decompress(shell[WASM])
    if shell[WASM][:8] != bytes.fromhex("0061736d01000000"):
        raise ValueError("Invalid WASM header")
    shell_hashes = {path: hashlib.sha256(shell[path]).hexdigest() for path in sorted(shell)}
    worker = (SITE / WORKER).read_bytes()
    config = json.dumps(shell_hashes, sort_keys=True, separators=(",", ":"))
    release = hashlib.sha256(config.encode() + worker).hexdigest()[:20]
    header = (f'self.SC_RELEASE = "{release}";\nself.SC_SHELL_HASHES = {config};\n'
              'self.SC_SHELL = Object.keys(self.SC_SHELL_HASHES);\n')
    size = sum((SITE / url).stat().st_size for url in urls)
    print(f"Release {release}, {len(urls)} unique media parts, {size:,} media bytes")
    if args.export:
        args.export.mkdir(parents=True, exist_ok=False)
        for name, data in shell.items():
            target = args.export / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (args.export / WORKER).write_bytes(header.encode() + worker)
        for url in urls:
            target = args.export / url
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SITE / url, target)


if __name__ == "__main__":
    main()
