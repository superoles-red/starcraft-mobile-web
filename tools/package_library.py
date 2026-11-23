#!/usr/bin/env python3
"""Package a Koprulu campaign repack for static hosting, and verify every decoded archive.

Usage: python3 tools/package_library.py "/path/to/repack" ["/path/to/library"]
The output directory is the site's library/ directory. No external dependencies.
"""
import argparse
from collections import Counter
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import tempfile


PART_BYTES = 1024 * 1024
MIN_REPEAT = 32 * 1024
ARCHIVE_NAME = re.compile(r'koprulu-[a-z0-9-]+\.mpq')
CHUNK_FILE = re.compile(r'chunks/[a-f0-9]{2}/[a-f0-9]{64}(?:-gz)?\.bin')
UINT32 = 0xffffffff


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def storm_seed_table():
    """The last 256 entries of the MPQ crypt table used by block-table decryption."""
    seed = 0x100001
    table = []
    for _ in range(256):
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2aaaab
            high = (seed & 0xffff) << 16
            seed = (seed * 125 + 3) % 0x2aaaab
        table.append(high | (seed & 0xffff))
    return table


CRYPT_SEEDS = storm_seed_table()


def member_spans(stream, file_size):
    """Read v1 MPQ storage extents to find exact repeated members across the repack."""
    signature, header, length, version, _, _, table_at, _, count = struct.unpack(
        '<4sIIHHIIII', stream.read(32))
    if (signature != b'MPQ\x1a' or header != 32 or version != 0 or length != file_size
            or count > 1000000 or table_at + count * 16 > length):
        raise ValueError(f'Invalid classic MPQ header in {stream.name}')
    stream.seek(table_at)
    table = bytearray(stream.read(count * 16))
    key, seed = 0xec83b3a3, 0xeeeeeeee  # Storm hash of "(block table)", hash kind 3.
    for pos in range(0, len(table), 4):
        seed = (seed + CRYPT_SEEDS[key & 255]) & UINT32
        word = struct.unpack_from('<I', table, pos)[0] ^ ((key + seed) & UINT32)
        struct.pack_into('<I', table, pos, word)
        key = (((~key << 21) + 0x11111111) | (key >> 11)) & UINT32
        seed = (word + seed + (seed << 5) + 3) & UINT32
    spans = set()
    for offset, stored, _, flags in struct.iter_unpack('<IIII', table):
        if flags & 0x80000000 and MIN_REPEAT <= stored <= PART_BYTES:
            if offset < header or offset + stored > length:
                raise ValueError(f'Invalid member extent in {stream.name}')
            spans.add((offset, stored))
    return sorted(spans)


def repeated_members(archives):
    # Only hash candidate extents of a size that occurs twice. Media need no decoding.
    spans = {}
    lengths = Counter()
    for archive in archives:
        with archive.open('rb') as stream:
            spans[archive] = member_spans(stream, archive.stat().st_size)
        lengths.update(size for _, size in spans[archive])
    candidates = {}
    counts = Counter()
    for archive, entries in spans.items():
        candidates[archive] = []
        with archive.open('rb') as stream:
            for offset, size in entries:
                if lengths[size] < 2:
                    continue
                stream.seek(offset)
                digest = sha256(stream.read(size))
                counts[digest] += 1
                candidates[archive].append((offset, size, digest))
    return {archive: [(p, n) for p, n, digest in entries if counts[digest] > 1]
            for archive, entries in candidates.items()}


def pieces(stream, size, repeats):
    # Keep repeated members standalone. All other bytes, including MPQ headers,
    # file tables and padding, travel in sequential parts up to 1 MiB.
    for boundary, repeat_length in [*repeats, (size, 0)]:
        if stream.tell() > boundary:
            raise ValueError(f'Overlapping extents in {stream.name}')
        while stream.tell() < boundary:
            expected = min(PART_BYTES, boundary - stream.tell())
            data = stream.read(expected)
            if len(data) != expected:
                raise ValueError(f'Archive changed while packaging: {stream.name}')
            yield data
        if repeat_length:
            data = stream.read(repeat_length)
            if len(data) != repeat_length:
                raise ValueError(f'Archive changed while packaging: {stream.name}')
            yield data


def store_part(root, data, digest):
    encoded = io.BytesIO()
    with gzip.GzipFile(fileobj=encoded, mode='wb', compresslevel=9, mtime=0) as writer:
        writer.write(data)
    compressed = encoded.getvalue()
    # Skip inflating already compressed media for a sub-kilobyte saving.
    use_gzip = len(data) - len(compressed) >= 1024
    stored = compressed if use_gzip else data
    name = digest + ('-gz.bin' if use_gzip else '.bin')
    target = Path('chunks', digest[:2], name)
    (root / target).parent.mkdir(parents=True, exist_ok=True)
    (root / target).write_bytes(stored)
    part = {'url': 'library/' + target.as_posix(), 'hash': digest, 'size': len(data)}
    if use_gzip:
        part['encoding'] = 'gzip'
    return part


def publish_files(staged, output):
    # Contents first, manifest last. Stop on unrelated files before modifying output.
    if output.is_symlink():
        raise ValueError(f'Output is a symbolic link: {output}')
    output.mkdir(parents=True, exist_ok=True)
    items = list(output.rglob('*'))
    if any(f.is_symlink() for f in items):
        raise ValueError(f'Output contains a symbolic link: {output}')
    existing = {f.relative_to(output).as_posix() for f in items if f.is_file()}
    if any(name != 'index.json' and not CHUNK_FILE.fullmatch(name) for name in existing):
        raise ValueError(f'Output contains unrelated files: {output}')
    current = {f.relative_to(staged).as_posix() for f in staged.rglob('*.bin')}
    for name in sorted(current):
        dest = output / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        (staged / name).replace(dest)
    (staged / 'index.json').replace(output / 'index.json')
    for name in existing - current - {'index.json'}:
        (output / name).unlink()


def package(source, output):
    archives = sorted(source.glob('*.mpq'))
    required = {'koprulu-core.mpq', 'koprulu-music.mpq', 'koprulu-cinematics.mpq'}
    if not required.issubset({p.name for p in archives}):
        raise ValueError('The repack must include core, music and cinematics')
    if any(not p.is_file() or not ARCHIVE_NAME.fullmatch(p.name) for p in archives):
        raise ValueError('Unexpected archive name in repack')
    output.parent.mkdir(parents=True, exist_ok=True)
    repeats = repeated_members(archives)
    manifest = {'version': 1, 'archives': []}
    shared = {}
    source_bytes = references = 0
    with tempfile.TemporaryDirectory(prefix='.library-', dir=output.parent) as temp:
        stage = Path(temp)
        for archive in archives:
            parts = []
            digest = hashlib.sha256()
            size = archive.stat().st_size
            with archive.open('rb') as stream:
                for data in pieces(stream, size, repeats[archive]):
                    part_hash = sha256(data)
                    digest.update(data)
                    if part_hash not in shared:
                        shared[part_hash] = store_part(stage, data, part_hash)
                    parts.append(shared[part_hash])
            rebuilt = hashlib.sha256()
            nbytes = 0
            wire = 0
            for part in parts:
                payload = (stage / part['url'][len('library/'):]).read_bytes()
                wire += len(payload)
                data = gzip.decompress(payload) if 'encoding' in part else payload
                if len(data) != part['size'] or sha256(data) != part['hash']:
                    raise ValueError(f'Part mismatch for {archive.name}')
                rebuilt.update(data)
                nbytes += len(data)
            if nbytes != size or rebuilt.digest() != digest.digest():
                raise ValueError(f'Archive reconstruction failed for {archive.name}')
            manifest['archives'].append({'name': archive.name, 'size': size,
                                         'hash': digest.hexdigest(), 'parts': parts})
            if archive.name in required:
                print(f'{archive.name}: {size:,} decoded, {wire:,} encoded bytes', flush=True)
            source_bytes += size
            references += len(parts)
        index = stage / 'index.json'
        index.write_text(json.dumps(manifest, separators=(',', ':')) + '\n', encoding='utf-8')
        stored = sum(p.stat().st_size for p in stage.rglob('*.bin'))
        index_size = index.stat().st_size
        publish_files(stage, output)
    print(f'{len(archives)} archives, {references} references to {len(shared)} unique parts; '
          f'{source_bytes:,} source bytes, {stored:,} stored bytes + {index_size:,} index bytes. All SHA-256 verified.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repack', type=Path)
    parser.add_argument('output', type=Path, nargs='?',
                        default=Path(__file__).resolve().parents[1] / 'site/library')
    args = parser.parse_args()
    try:
        package(args.repack, args.output)
    except (OSError, ValueError, struct.error) as exc:
        parser.exit(1, f'Packaging failed: {exc}\n')
