"""Verify the exact contents of the site uploaded to Pages."""
from pathlib import Path

site = Path(__file__).resolve().parents[1] / "site"
public = {"index.html", "launch.js", "local-files.js", "engine/koprulu.js",
          "engine/koprulu.wasm", "engine/NOTICES.txt"}
actual = {f.relative_to(site).as_posix() for f in site.rglob("*") if f.is_file()}
if actual != public or any(f.is_symlink() for f in site.rglob("*")):
    raise SystemExit(f"Unexpected or missing public files: {sorted(actual ^ public)}")
if (site / "engine/koprulu.wasm").read_bytes()[:8] != bytes.fromhex("0061736d01000000"):
    raise SystemExit("Invalid WASM header")
print("Public bundle:", sum((site / name).stat().st_size for name in public), "bytes")
