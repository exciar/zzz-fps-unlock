#!/usr/bin/env python3
"""Zenless Zone Zero (Android, arm64) FPS patcher.

Patches Unity's Application.targetFrameRate setter in libunity.so so the game's
60 FPS setting runs at --fps (default 120); lower requests (30 FPS loading
screens) are left alone. The setter is found by its instruction pattern rather than a
fixed address, so new game versions work as long as Unity's setter is unchanged.
If the pattern is not found exactly once, the patcher refuses to write anything.

  python zzz_fps_patcher.py build --apks IN_DIR --out OUT_DIR --build-tools BT_DIR \
      --keystore my.p12 --ks-pass file:my.p12.password
  python zzz_fps_patcher.py lib libunity.so libunity-patched.so

The package name is unchanged, so HyperOS/Game Turbo treat it as the real game.
Output is signed with YOUR key: the official app must be uninstalled once, then later
versions patched with the same key install over it with `adb install-multiple -r`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

PACKAGE = 'com.HoYoverse.Nap'
# Google Play app-signing certificate of the official ZZZ APKs (verified on 3.2.0).
OFFICIAL_SIGNER = 'a91ce15666de81e288e0e79600b94b5ef342ee33f1117d169165d97e4ea89f35'
LIB = 'lib/arm64-v8a/libunity.so'
MIN_REQUEST = 60          # requests >= this are raised; 30 FPS loading requests stay
PAGE_ALIGNMENT = 0x4000   # 16 KiB pages
PHDR = struct.Struct('<IIQQQQQQ')
PT_LOAD, PT_PHDR, PF_X = 1, 6, 1

# Unity Application.targetFrameRate setter, as (mask, value) per instruction:
#   stp x30,x19,[sp,#-16]!; mov w19,w0; bl force45; mov w8,#45; tst w0,#1;
#   csel w8,w8,w19,ne; adrp x9,..; str w8,[x9,..]; ldp x30,x19,[sp],#16; b swappy
SETTER_PATTERN = [
    (0xFFFFFFFF, 0xA9BF4FFE), (0xFFFFFFFF, 0x2A0003F3), (0xFC000000, 0x94000000),
    (0xFFFFFFFF, 0x528005A8), (0xFFFFFFFF, 0x7200001F), (0xFFFFFFFF, 0x1A931108),
    (0x9F00001F, 0x90000009), (0xFFC003FF, 0xB9000128), (0xFFFFFFFF, 0xA8C14FFE),
    (0xFC000000, 0x14000000),
]


def align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) & -alignment


def arm64_b(address: int, destination: int) -> bytes:
    distance = destination - address
    if distance % 4 or not -(1 << 27) <= distance < (1 << 27):
        raise ValueError('AArch64 branch cannot reach destination')
    return struct.pack('<I', 0x14000000 | ((distance >> 2) & 0x3FFFFFF))


def parse_phdrs(data):
    if data[:16] != bytes.fromhex('7f454c46020101000000000000000000'):
        raise ValueError('Expected a little-endian ELF64 library')
    if struct.unpack_from('<H', data, 18)[0] != 183:
        raise ValueError('Expected an arm64 (EM_AARCH64) library')
    offset = struct.unpack_from('<Q', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 54)
    if size != PHDR.size:
        raise ValueError('Unexpected program-header entry size')
    return offset, [list(PHDR.unpack_from(data, offset + size * i)) for i in range(count)]


def file_offset(headers, address: int, size: int = 1) -> int:
    for typ, flags, offset, vaddr, paddr, filesz, memsz, alignment in headers:
        if typ == PT_LOAD and vaddr <= address and address + size <= vaddr + filesz:
            return offset + address - vaddr
    raise ValueError(f'RVA {address:#x} has no file-backed LOAD mapping')


def find_setter(data, headers) -> int:
    hits = []
    first = SETTER_PATTERN[0][1]
    for typ, flags, offset, vaddr, paddr, filesz, memsz, alignment in headers:
        if typ != PT_LOAD or not flags & PF_X:
            continue
        words = struct.unpack_from(f'<{filesz // 4}I', data, offset)
        for k, word in enumerate(words):
            if word == first and k + len(SETTER_PATTERN) <= len(words) and all(
                    words[k + j] & mask == value for j, (mask, value) in enumerate(SETTER_PATTERN)):
                hits.append(vaddr + 4 * k)
    if len(hits) != 1:
        raise ValueError(f'Expected exactly one targetFrameRate setter, found {len(hits)} '
                         f'({", ".join(map(hex, hits))}). Already patched, or this game version '
                         'changed Unity\'s setter and needs re-analysis.')
    return hits[0]


def patch_libunity(raw: bytes, fps: int = 120) -> tuple[bytes, dict]:
    """Add an RX segment with a 4-instruction stub; replace setter's `mov w19,w0` with a branch to it."""
    if not MIN_REQUEST <= fps <= 240:
        raise ValueError(f'--fps must be between {MIN_REQUEST} and 240')
    old_phoff, old_headers = parse_phdrs(raw)
    setter = find_setter(raw, old_headers)
    patch_rva, return_rva = setter + 4, setter + 8
    loads = [p for p in old_headers if p[0] == PT_LOAD]
    new_offset = align_up(len(raw), PAGE_ALIGNMENT)
    new_vaddr = align_up(max(p[3] + p[6] for p in loads), PAGE_ALIGNMENT)
    new_headers = [p[:] for p in old_headers]
    table_size = (len(new_headers) + 1) * PHDR.size
    stub_relative = align_up(table_size, 0x400)
    stub_rva = new_vaddr + stub_relative

    # CMP W0,#MIN_REQUEST; MOVZ W19,#fps; CSEL W19,W19,W0,GE; B setter+8 (signed compare)
    stub = struct.pack('<III', 0x7100001F | MIN_REQUEST << 10, 0x52800013 | fps << 5, 0x1A80A273)
    stub += arm64_b(stub_rva + len(stub), return_rva)
    new_size = stub_relative + len(stub)
    for p in new_headers:
        if p[0] == PT_PHDR:
            p[:] = [PT_PHDR, 4, new_offset, new_vaddr, new_vaddr, table_size, table_size, 8]
    if sum(p[0] == PT_PHDR for p in new_headers) != 1:
        raise ValueError('Expected exactly one PT_PHDR')
    new_load = [PT_LOAD, 5, new_offset, new_vaddr, new_vaddr, new_size, new_size, PAGE_ALIGNMENT]
    last_load_index = max(i for i, p in enumerate(new_headers) if p[0] == PT_LOAD)
    new_headers.insert(last_load_index + 1, new_load)  # LOADs stay sorted by vaddr
    table = b''.join(PHDR.pack(*p) for p in new_headers)
    patched = bytearray(raw)
    patched.extend(b'\x00' * (new_offset + new_size - len(patched)))
    struct.pack_into('<Q', patched, 32, new_offset)
    struct.pack_into('<H', patched, 56, len(new_headers))
    patch_offset = file_offset(old_headers, patch_rva, 4)
    entry_branch = arm64_b(patch_rva, stub_rva)
    patched[patch_offset:patch_offset + 4] = entry_branch
    patched[new_offset:new_offset + len(table)] = table
    patched[new_offset + stub_relative:new_offset + new_size] = stub

    # Only e_phoff, e_phnum and the 4-byte branch may differ from the original bytes.
    allowed = set(range(32, 40)) | set(range(56, 58)) | set(range(patch_offset, patch_offset + 4))
    if not {i for i, (a, b) in enumerate(zip(raw, patched)) if a != b} <= allowed:
        raise AssertionError('Unexpected original-byte modification')
    if parse_phdrs(patched)[1] != new_headers:
        raise AssertionError('Program-header roundtrip mismatch')
    for p in new_headers:
        if p[0] == PT_LOAD and (p[5] > p[6] or p[2] + p[5] > len(patched) or p[2] % p[7] != p[3] % p[7]):
            raise AssertionError('Invalid ELF LOAD extent or alignment')
    info = {'setter_rva': hex(setter), 'stub_rva': hex(stub_rva), 'fps': fps,
            'source_sha256': hashlib.sha256(raw).hexdigest(),
            'patched_sha256': hashlib.sha256(patched).hexdigest()}
    return bytes(patched), info


def run(cmd):
    result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'{Path(str(cmd[0])).name} failed:\n{result.stdout}{result.stderr}')
    return result.stdout


def keytool_pass(ks_pass: str) -> list[str]:
    kind, _, value = ks_pass.partition(':')
    return {'pass': ['-storepass', value], 'env': ['-storepass:env', value], 'file': ['-storepass:file', value]}[kind]


def build(args):
    bt = args.build_tools
    apksigner = [args.java, '-jar', bt / 'lib' / 'apksigner.jar']
    zipalign = next(p for p in (bt / 'zipalign.exe', bt / 'zipalign') if p.exists())
    apks = sorted(args.apks.glob('*.apk'))
    if not any(a.name == 'base.apk' for a in apks):
        raise SystemExit(f'No base.apk in {args.apks}; pass the full split set (base.apk + split_*.apk).')
    for apk in apks:
        certs = run(apksigner + ['verify', '--print-certs', apk])
        if f'Signer #1 certificate SHA-256 digest: {OFFICIAL_SIGNER}' not in certs and not args.allow_any_signer:
            raise SystemExit(f'{apk.name} is not signed by the official ZZZ certificate. '
                             'Use unmodified official APKs (or --allow-any-signer if you know why).')
    with_lib = [a for a in apks if LIB in zipfile.ZipFile(a).namelist()]
    if len(with_lib) != 1:
        raise SystemExit(f'Expected exactly one APK containing {LIB}, found {len(with_lib)}')
    patched_lib, info = patch_libunity(zipfile.ZipFile(with_lib[0]).read(LIB), args.fps)
    print(json.dumps(info), flush=True)

    if not args.keystore.exists():
        keytool = Path(shutil.which('keytool') or Path(args.java).with_name('keytool'))
        run([keytool, '-genkeypair', '-keystore', args.keystore, '-storetype', 'PKCS12',
             *keytool_pass(args.ks_pass), '-alias', args.alias, '-keyalg', 'RSA', '-keysize', '3072',
             '-validity', '10000', '-dname', 'CN=ZZZ FPS Patcher'])
        print(f'Created new signing key {args.keystore}; back it up, future updates need it.', flush=True)

    args.out.mkdir(parents=True, exist_ok=True)
    work = args.out / '.work'
    work.mkdir(exist_ok=True)
    report = {'package': PACKAGE, 'patch': info, 'apks': []}
    for apk in apks:
        unsigned, aligned, target = work / f'{apk.stem}-unsigned.apk', work / f'{apk.stem}-aligned.apk', args.out / apk.name
        with zipfile.ZipFile(apk) as src, zipfile.ZipFile(unsigned, 'w') as dst:
            for item in src.infolist():
                name = item.filename
                if name.startswith('META-INF/') and (name == 'META-INF/MANIFEST.MF' or
                                                     name.upper().endswith(('.SF', '.RSA', '.DSA', '.EC'))):
                    continue  # old signature; re-signed below
                dst.writestr(copy.copy(item), patched_lib if name == LIB else src.read(name))
        run([zipalign, '-P', '16', '-f', '4', unsigned, aligned])
        run(apksigner + ['sign', '--ks', args.keystore, '--ks-key-alias', args.alias, '--ks-pass', args.ks_pass,
                         '--v1-signing-enabled', 'true', '--v2-signing-enabled', 'true',
                         '--v3-signing-enabled', 'true', '--v4-signing-enabled', 'false', '--out', target, aligned])
        run(apksigner + ['verify', target])
        run([zipalign, '-c', '-P', '16', '4', target])
        if apk in with_lib and hashlib.sha256(zipfile.ZipFile(target).read(LIB)).digest() != hashlib.sha256(patched_lib).digest():
            raise AssertionError('Signed APK does not contain the patched library')
        report['apks'].append({'file': target.name, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        print(f'signed {target.name}', flush=True)
    shutil.rmtree(work)
    (args.out / 'patch-manifest.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('\nInstall (first time: uninstall the official app first; later versions update in place):')
    print('adb install-multiple -r ' + ' '.join(f'"{args.out / a.name}"' for a in apks))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('build', help='patch + re-sign a full official split-APK set')
    b.add_argument('--apks', type=Path, required=True, help='folder with base.apk and split_*.apk')
    b.add_argument('--out', type=Path, required=True)
    b.add_argument('--build-tools', type=Path, required=True, help='Android SDK build-tools dir (35+)')
    b.add_argument('--keystore', type=Path, required=True, help='PKCS12 key; created if missing')
    b.add_argument('--ks-pass', required=True, help='apksigner format: pass:..., env:VAR or file:PATH')
    b.add_argument('--alias', default='fps-lab')
    b.add_argument('--java', default='java')
    b.add_argument('--fps', type=int, default=120)
    b.add_argument('--allow-any-signer', action='store_true')
    lib = sub.add_parser('lib', help='patch a bare libunity.so')
    lib.add_argument('source', type=Path)
    lib.add_argument('output', type=Path)
    lib.add_argument('--fps', type=int, default=120)
    args = parser.parse_args()
    if args.command == 'build':
        build(args)
    else:
        if args.source.resolve() == args.output.resolve():
            raise SystemExit('Refusing to overwrite the source library')
        patched, info = patch_libunity(args.source.read_bytes(), args.fps)
        args.output.write_bytes(patched)
        print(json.dumps(info, indent=2))


if __name__ == '__main__':
    main()
