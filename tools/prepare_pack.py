#!/usr/bin/env python3
"""Build a private compact campaign library from a locally prepared classic installation."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

PIN = "5ff8905b1d0bc6ab52d6563b8ef7b0a75ec40133"
TOOLS = Path(__file__).resolve().parent
PRIVATE = Path.home() / "Documents" / "StarCraft Local Data"


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def prepare_converter(work, jobs):
    src = work / "KopruluSector-converter"
    if not src.exists():
        run("git", "init", "-q", str(src))
        run("git", "remote", "add", "origin", "https://github.com/n0madic/KopruluSector.git", cwd=src)
    revision = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=src,
                              capture_output=True, text=True)
    if revision.returncode:
        run("git", "fetch", "--depth=1", "origin", PIN, cwd=src)
        run("git", "checkout", "--detach", "FETCH_HEAD", cwd=src)
    elif revision.stdout.strip() != PIN:
        raise SystemExit(f"Expected KopruluSector {PIN} in {src}")

    patch = TOOLS / "compact-classic.patch"
    # The dedicated working copy is reusable. Never overwrite local modifications.
    reverse = subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=src,
                             capture_output=True)
    if reverse.returncode:
        run("git", "apply", "--check", str(patch), cwd=src)
        run("git", "apply", str(patch), cwd=src)
    run("bash", "tools/toolchain/fetch.sh", cwd=src)
    build = work / "native-converter"
    run("cmake", "-S", str(src), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release")
    run("cmake", "--build", str(build), "--target", "kp_convert", "--parallel", str(jobs))
    return src, build / "kp_convert"


def validate(src, stage, log, preview):
    manifest = json.loads((src / "defs/scr/inputs-bw-1.17.0.1.json").read_text())
    expected = {"koprulu-core.mpq", "koprulu-music.mpq"}
    for name, *_ in manifest["files"]:
        parts = name.lower().split("\\")
        if parts[0] == "campaign" and parts[-1] == "scenario.chk":
            expected.add(f"koprulu-{parts[1]}-{parts[2]}.mpq")
    if not preview:
        expected.add("koprulu-cinematics.mpq")
        movies = json.loads((src / "defs/campaign/cinematics.json").read_text())["names"]
        if not re.search(rf"^{len(movies)} movies as WebM ", log, re.MULTILINE):
            raise SystemExit("Incomplete cinematic library. See the private conversion log.")
    missing = sorted(name for name in expected if not (stage / name).is_file())
    if missing or "left out" in log or "damaged in the source" in log:
        raise SystemExit(f"Incomplete private library. Check the conversion log. Missing groups: {missing}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("install", nargs="?", type=Path, default=PRIVATE / "English1161")
    parser.add_argument("--out", type=Path, help="new output directory (defaults to the local launcher's library)")
    parser.add_argument("--work", type=Path, default=Path.home() / ".cache" / "starcraft-mobile-web")
    parser.add_argument("--preview", action="store_true", help="build a gameplay test library without cinematics")
    parser.add_argument("--jobs", type=int, default=min(6, os.cpu_count() or 1))
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    install = args.install.resolve()
    output = (args.out or PRIVATE / ("preview/repack" if args.preview else "repack")).resolve()
    if output.exists():
        parser.error(f"{output} already exists. Select a new --out directory for another build.")
    for filename in ("StarDat.mpq", "BrooDat.mpq", "StarCraft.mpq", "BroodWar.mpq", "Patch_rt.mpq"):
        if not (install / filename).is_file():
            parser.error(f"Missing {filename} in {install}")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    source, converter = prepare_converter(work, args.jobs)
    with tempfile.TemporaryDirectory(prefix=".sc-pack-", dir=output.parent) as temporary:
        stage = Path(temporary) / "repack"
        with tempfile.NamedTemporaryFile(mode="w", prefix="convert-", suffix=".log", dir=work,
                                         delete=False) as log:
            print(f"Converting local game data. Log: {log.name}", flush=True)
            cmd = [converter, "repack", install, "--out", stage, "--cache", work / "media-cache",
                   "--threads", str(args.jobs)]
            if args.preview:
                cmd.append("--gameplay-only")
            result = subprocess.run(cmd, cwd=source, stdout=log, stderr=subprocess.STDOUT)
        contents = Path(log.name).read_text()
        print(contents, end="")
        result.check_returncode()
        validate(source, stage, contents, args.preview)
        stage.rename(output)
    size = sum(file.stat().st_size for file in output.glob("*.mpq"))
    print(f"Ready: {output} ({size:,} bytes)")


if __name__ == "__main__":
    main()
