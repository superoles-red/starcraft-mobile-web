"""Check the code-only distribution and optionally export it for any static HTTP host."""
import argparse
import gzip
from pathlib import Path
import shutil

SITE = Path(__file__).resolve().parents[1] / "site"
ENGINE = "engine/koprulu.wasm.gz"
PUBLIC = {"index.html", "launch.js", "local-files.js", "engine/koprulu.js",
          ENGINE, "engine/NOTICES.txt"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, help="create a new static site directory")
    args = parser.parse_args()
    actual = {f.relative_to(SITE).as_posix() for f in SITE.rglob("*") if f.is_file()}
    if actual != PUBLIC or any(f.is_symlink() for f in SITE.rglob("*")):
        raise SystemExit(f"Unexpected or missing public files: {sorted(actual ^ PUBLIC)}")
    engine = gzip.decompress((SITE / ENGINE).read_bytes())
    if engine[:8] != bytes.fromhex("0061736d01000000"):
        raise SystemExit("Invalid WASM header")
    print("Code-only distribution:", sum((SITE / name).stat().st_size for name in PUBLIC), "bytes")
    if args.export:
        # Generic static hosts can apply their own HTTP compression to the normal WASM URL.
        args.export.mkdir(parents=True, exist_ok=False)
        (args.export / "engine").mkdir()
        for name in PUBLIC - {ENGINE}:
            shutil.copyfile(SITE / name, args.export / name)
        (args.export / "engine/koprulu.wasm").write_bytes(engine)


if __name__ == "__main__":
    main()
