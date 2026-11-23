#!/usr/bin/env bash
# Build a code-only runtime. Emscripten 6.0.12, CMake >=3.20, Ninja and git are required.
set -euo pipefail
out=${1:?usage: build-engine.sh OUTPUT_DIRECTORY [WORK_DIRECTORY]}
work=${2:-"${TMPDIR:-/tmp}/starcraft-web-build"}
mkdir -p "$work" "$out"
out=$(cd "$out" && pwd)
work=$(cd "$work" && pwd)
patch_file="$(cd "$(dirname "$0")" && pwd)/original-touch-console.patch"
src="$work/KopruluSector"
pin=5ff8905b1d0bc6ab52d6563b8ef7b0a75ec40133
if [ ! -d "$src/.git" ]; then
  git init -q "$src"
  git -C "$src" remote add origin https://github.com/n0madic/KopruluSector.git
  git -C "$src" fetch --depth=1 origin "$pin"
  git -C "$src" checkout --detach -q FETCH_HEAD
fi
if [ "$(git -C "$src" rev-parse HEAD)" != "$pin" ]; then
  echo "Unexpected revision in $src" >&2
  exit 1
fi
if git -C "$src" diff --quiet HEAD; then
  git -C "$src" apply "$patch_file"
elif ! git -C "$src" diff --binary HEAD | cmp -s - "$patch_file"; then
  echo "Unexpected changes in $src" >&2
  exit 1
fi
(cd "$src" && bash tools/toolchain/fetch.sh)
emcmake cmake -S "$src" -B "$work/build-web" -G Ninja -DCMAKE_BUILD_TYPE=Release -DKP_APP=ON -DKP_CXX_CHECK=OFF '-DKP_WEB_LINK='
cmake --build "$work/build-web" --target koprulu --parallel "${JOBS:-4}"
cp "$work/build-web/koprulu.js" "$work/build-web/koprulu.wasm" "$out/"
