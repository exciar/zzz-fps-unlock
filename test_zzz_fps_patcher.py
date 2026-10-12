"""Self-check on a synthetic arm64 ELF (no game files needed): python test_zzz_fps_patcher.py"""

import struct

from zzz_fps_patcher import PHDR, PT_LOAD, PT_PHDR, find_setter, parse_phdrs, patch_libunity

# Real setter bytes from ZZZ 3.2.0 libunity.so (RVA 0x7aa1e4).
SETTER = bytes.fromhex('fe4fbfa9f303002a17000c94a80580521f0000720811931ae99300b028d10ab9fe4fc1a8711e0c14')


def fake_lib(offsets=(0x1000,)):
    size = 0x3000
    data = bytearray(size)
    data[:16] = bytes.fromhex('7f454c46020101000000000000000000')
    struct.pack_into('<HHI', data, 16, 3, 183, 1)
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HH', data, 54, PHDR.size, 2)
    PHDR.pack_into(data, 64, PT_PHDR, 4, 64, 64, 64, 2 * PHDR.size, 2 * PHDR.size, 8)
    PHDR.pack_into(data, 64 + PHDR.size, PT_LOAD, 5, 0, 0, 0, size, size, 0x4000)
    for off in offsets:
        data[off:off + len(SETTER)] = SETTER
    return bytes(data)


def word(data, rva):
    _, headers = parse_phdrs(data)
    for typ, flags, offset, vaddr, paddr, filesz, memsz, align in headers:
        if typ == PT_LOAD and vaddr <= rva < vaddr + filesz:
            return struct.unpack_from('<I', data, offset + rva - vaddr)[0]
    raise KeyError(hex(rva))


def branch_target(insn, at):
    assert insn >> 26 == 0b000101, hex(insn)
    imm = insn & 0x3FFFFFF
    return at + ((imm - (1 << 26) if imm >> 25 else imm) << 2)


def run_stub(data, setter, w0):
    """Decode and execute the injected CMP/MOVZ/CSEL stub for one request value."""
    stub = branch_target(word(data, setter + 4), setter + 4)
    cmp_, movz, csel, back = (word(data, stub + 4 * i) for i in range(4))
    assert cmp_ & 0xFFC003FF == 0x7100001F and movz & 0xFFE0001F == 0x52800013 and csel == 0x1A80A273
    assert branch_target(back, stub + 12) == setter + 8
    signed = w0 - (1 << 32) if w0 >> 31 else w0
    return (movz >> 5) & 0xFFFF if signed >= (cmp_ >> 10) & 0xFFF else w0


def test_patch():
    lib = fake_lib()
    assert find_setter(lib, parse_phdrs(lib)[1]) == 0x1000
    for fps in (120, 90):
        patched, info = patch_libunity(lib, fps)
        assert info['setter_rva'] == '0x1000'
        for request, expected in [(0, 0), (30, 30), (59, 59), (60, fps), (61, fps), (120, fps),
                                  (144, fps), (0xFFFFFFFF, 0xFFFFFFFF), (0x7FFFFFFF, fps)]:
            assert run_stub(patched, 0x1000, request) == expected, (fps, request)
        # Original instructions around the patched one are untouched.
        assert word(patched, 0x1000) == word(lib, 0x1000) and word(patched, 0x1008) == word(lib, 0x1008)


def test_refuses():
    for bad in (fake_lib(()), fake_lib((0x1000, 0x2000)), patch_libunity(fake_lib())[0]):
        try:
            patch_libunity(bad)
        except ValueError:
            continue
        raise AssertionError('patched an input without exactly one unpatched setter')


if __name__ == '__main__':
    test_patch()
    test_refuses()
    print('ok')
