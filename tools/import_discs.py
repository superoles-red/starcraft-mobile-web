#!/usr/bin/env python3
"""Import the English StarCraft retail CDs and 1.16.1 update into a new private folder.

Requires bsdtar and a separately installed StormLib shared library. The CDs and
update are read as archives; no Windows executable runs. Only game resources
used by the web conversion and the patch's reference executable are staged.
"""
import argparse
import contextlib
import ctypes as C
import ctypes.util
import hashlib
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import zlib

# The two-stream Ptc delta decoder is based on jaenster/libd2, formats/src/ptc.zig.
# MIT License
# Copyright (c) 2026 jaenster
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

INPUTS = {
    'SC.iso': '0b7ec59f2705cbcba46013b0e383ed69692b0f9bded6faf7318156e7c8ccfb92',
    'BROODWAR.iso': 'a438ad385a61188f9cec6e530f2d8e5228bd95c144e694210568ca11c40b4158',
    'BW-1161.exe': '96890f59b664eb54dbb3be634f2045e70a4a757e87b405ec4aeeb69d50fb7bb1',
}


def digest(path):
    with path.open('rb') as f:
        h = hashlib.sha256()
        for block in iter(lambda: f.read(2**20), b''):
            h.update(block)
        return h.hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def take(stream, count):
    out = stream.read(count)
    require(len(out) == count, 'Truncated patch token')
    return out


def variable(stream, signed=False):
    first = take(stream, 1)[0]
    if first < 128:
        bits, count = 7, 0
    elif first < 192:
        bits, count = 6, 1
    elif first < 224:
        bits, count = 5, 2
    else:
        bits, count = 5, 3
    value = (first & ((1 << bits) - 1)) | (int.from_bytes(take(stream, count), 'little') << bits)
    width = bits + count * 8
    return value - (1 << width) if signed and value & (1 << (width-1)) else value


def decode_delta(src, payload, size):
    length_a, length_b = struct.unpack_from('<II', payload)
    require(8 + length_a + length_b == len(payload), 'Wrong delta stream sizes')
    first = io.BytesIO(payload[8:8 + length_a])
    second = io.BytesIO(payload[8 + length_a:])
    differences = bytearray(src)
    word = struct.Struct('<H')
    for p in range(len(src) - 2, 1, -2):
        diff = word.unpack_from(src, p)[0] - word.unpack_from(src, p - 2)[0]
        word.pack_into(differences, p, diff & 65535)
    out = bytearray()
    cursor = 0
    while first.tell() < length_a:
        token = word.unpack(take(first, 2))[0]
        count, mode = token & 0x3fff, token >> 14
        require(len(out) + count <= size, 'Patch exceeds target size')
        if mode in (1, 2):
            cursor += variable(first, signed=True)
            require(0 <= cursor <= len(src) - count, 'Patch references invalid source offset')
        if mode == 0:
            out.extend(take(first, count))
        elif mode == 1:
            out.extend(src[cursor:cursor + count])
        elif mode == 2:
            require(count % 2 == 0, 'Odd patch difference run')
            prev = word.unpack(out[-2:])[0] if len(out) >= 2 else 0
            for p in range(cursor, cursor + count, 2):
                prev = (prev + word.unpack_from(differences, p)[0]) & 65535
                out.extend(word.pack(prev))
        else:
            out.extend(bytes(count))
        cursor += count
    require(len(out) == size, 'Patch produced wrong output length')
    out.append(0)  # A final 16-bit fixup can cover the last output byte.
    value = 0
    while second.tell() < length_b:
        # Sorted fixup values start at a signed offset, then advance monotonically.
        increment = variable(second, signed=second.tell() == 0)
        if not increment:
            break
        value += increment
        offset = variable(second)
        while True:
            require(offset < size, 'Patch fixup outside target')
            word.pack_into(out, offset, (word.unpack_from(out, offset)[0] + value) & 65535)
            step = variable(second)
            if not step:
                break
            offset += step
    return bytes(out[:size])


class Storm:
    def __init__(self, filename):
        self.lib = C.CDLL(filename)
        h, u, s, ptr = C.c_void_p, C.c_uint32, C.c_char_p, C.POINTER
        signatures = {
            'SFileOpenArchive': ([s, u, u, ptr(h)], C.c_bool),
            'SFileCreateArchive': ([s, u, u, ptr(h)], C.c_bool),
            'SFileCloseArchive': ([h], C.c_bool),
            'SFileOpenFileEx': ([h, s, u, ptr(h)], C.c_bool),
            'SFileGetFileSize': ([h, ptr(u)], u),
            'SFileReadFile': ([h, h, u, ptr(u), h], C.c_bool),
            'SFileCloseFile': ([h], C.c_bool),
            'SFileCreateFile': ([h, s, C.c_uint64, u, u, u, ptr(h)], C.c_bool),
            'SFileWriteFile': ([h, h, u, u], C.c_bool),
            'SFileFinishFile': ([h], C.c_bool),
        }
        for name, (args, result) in signatures.items():
            f = getattr(self.lib, name)
            f.argtypes, f.restype = args, result

    @contextlib.contextmanager
    def archive(self, path, create=False):
        handle = C.c_void_p()
        func = self.lib.SFileCreateArchive if create else self.lib.SFileOpenArchive
        flags, limit = (0x100000, 4096) if create else (0, 0x100)
        require(func(os.fsencode(path), flags, limit, C.byref(handle)), f'Cannot open {path}')
        try:
            yield handle
        finally:
            require(self.lib.SFileCloseArchive(handle), f'Cannot close {path}')

    def read(self, archive, name):
        handle = C.c_void_p()
        if not self.lib.SFileOpenFileEx(archive, name.encode('cp1252'), 0, C.byref(handle)):
            raise KeyError(name)
        try:
            high, got = C.c_uint32(), C.c_uint32()
            size = self.lib.SFileGetFileSize(handle, C.byref(high))
            require(not high.value and size < 128 * 2**20, f'Unexpected member size for {name}')
            buf = C.create_string_buffer(size)
            read_ok = self.lib.SFileReadFile(handle, buf, size, C.byref(got), None)
            require(read_ok and got.value == size, f'Cannot read complete {name}')
            return buf.raw
        finally:
            self.lib.SFileCloseFile(handle)

    def add(self, archive, name, data):
        handle = C.c_void_p()
        require(self.lib.SFileCreateFile(archive, name.encode('cp1252'), 0, len(data), 0, 0x200, C.byref(handle)), f'Cannot add {name}')
        try:
            require(self.lib.SFileWriteFile(handle, data, len(data), 2), f'Cannot write {name}')
        finally:
            require(self.lib.SFileFinishFile(handle), f'Cannot finish {name}')


def patched_file(storm, discs, name, record):
    header_size, kind, crc, source_size, size, _ = struct.unpack_from('<HHIIIQ', record)
    require(header_size == 24 and size < 128 * 2**20, f'Unexpected record header for {name}')
    if kind == 0x104:
        require(len(record) == header_size + size, f'Wrong whole-file size for {name}')
        return record[header_size:]
    require(kind == 4, f'Unsupported delta format for {name}')
    for arc in discs:
        try:
            src = storm.read(arc, name)
        except KeyError:
            continue
        if len(src) == source_size and zlib.crc32(src) == crc:
            return decode_delta(src, record[header_size:], size)
    raise ValueError(f'Missing matching delta base for {name}')


def build_patch(storm, update, outdir):
    order = ['BrooDat.mpq', 'StarDat.mpq', 'BroodWar.mpq', 'StarCraft.mpq']
    with contextlib.ExitStack() as stack:
        bases = [stack.enter_context(storm.archive(outdir / n)) for n in order]
        source = stack.enter_context(storm.archive(update))
        target = stack.enter_context(storm.archive(outdir / 'Patch_rt.mpq', create=True))
        # The installed game keeps the StarCraft disc's intro logo in its core files.
        # Resolve this before the expansion disc's differently encoded copy.
        logo_name = r'smk\blizzard.smk'
        logo = storm.read(bases[-1], logo_name)
        require(hashlib.sha256(logo).hexdigest() == '6b9255bba8dcfa60ddbd28bf4c58f4c15b95069cdb620cc8bca7125530881417',
                'Unexpected original StarCraft intro')
        storm.add(target, logo_name, logo)
        seen = set()
        for line in storm.read(source, 'patch.lst').decode('cp1252').splitlines():
            fields = line.split(';')
            if len(fields) != 3 or fields[2] != '0x0':
                continue  # Neutral English game records. Loose multiplayer maps have two columns.
            name, member, _ = fields
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            if name.lower().startswith('files\\') and name.lower() != 'files\\starcraft.exe':
                continue
            data = patched_file(storm, bases, name, storm.read(source, member))
            if name.lower() == 'files\\starcraft.exe':
                (outdir / 'StarCraft.exe').write_bytes(data)
            else:
                storm.add(target, name, data)


def check_patch(storm, staging):
    # Independent resource digests also validate the signed and unsigned delta paths.
    samples = {
        r'arr\units.dat': 'da2ee6f116b77329048ec6be9e02721a527fde23d4b05b1b1336e634490abcba',
        r'scripts\AISCRIPT.BIN': '635433e546998e05529a9d6acc806c06b528c7ce5075a206f6f2c8de44ffed6e',
        r'templates\templates.lst': '6ff08e52f2eefe4ee0c89786bec5e2b2d120ff037e78b77406fd8ee455e084a9',
    }
    with storm.archive(staging / 'Patch_rt.mpq') as archive:
        for name, expected in samples.items():
            require(hashlib.sha256(storm.read(archive, name)).hexdigest() == expected, f'Patched member checksum failed: {name}')
    require(digest(staging / 'StarCraft.exe') == 'ad6b58b27b8948845ccfa69bcfcc1b10d6aa7a27a371ee3e61453925288c6a46',
            'Unexpected 1.16.1 reference executable')


def import_discs(src, dest, storm):
    require(not dest.exists(), f'Choose a new output directory: {dest}')
    for name, expected in INPUTS.items():
        require(digest(src / name) == expected, f'Unsupported or damaged disc/update: {name}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        staging = Path(tmp) / 'install'
        staging.mkdir(mode=0o700)
        for iso, cd, nested in [('SC.iso', 'StarCraft.mpq', 'StarDat.mpq'), ('BROODWAR.iso', 'BroodWar.mpq', 'BrooDat.mpq')]:
            with (staging / cd).open('xb') as out:
                subprocess.run(['bsdtar', '-xOf', str(src / iso), 'INSTALL.EXE'], stdout=out, check=True)
            with storm.archive(staging / cd) as disc:
                (staging / nested).write_bytes(storm.read(disc, 'files\\' + nested))
        build_patch(storm, src / 'BW-1161.exe', staging)
        check_patch(storm, staging)
        staging.rename(dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--stormlib', default=os.getenv('KOPRULU_STORMLIB') or ctypes.util.find_library('storm'))
    args = parser.parse_args()
    if not args.stormlib or not shutil.which('bsdtar'):
        parser.error('Install bsdtar and StormLib, or set --stormlib to its shared library path.')
    try:
        import_discs(args.source.resolve(), args.output.resolve(), Storm(args.stormlib))
    except (OSError, ValueError, subprocess.CalledProcessError, KeyError, struct.error) as exc:
        parser.exit(1, f'Import failed: {exc}\n')
    for path in sorted(args.output.iterdir()):
        print(f'{path.name:14} {path.stat().st_size:12,d}  {digest(path)}')


if __name__ == '__main__':
    main()
