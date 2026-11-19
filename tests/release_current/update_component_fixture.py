# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Minimal AMD64 PE header fixture; not executable and never launched."""
def pe_bytes():
    value = bytearray(1024)
    value[:2] = b'MZ'
    value[0x3c:0x40] = (64).to_bytes(4, 'little')
    value[64:68] = b'PE\0\0'
    value[68:70] = (0x8664).to_bytes(2, 'little')
    value[84:86] = (112).to_bytes(2, 'little')
    value[88:90] = b'\x0b\x02'
    return bytes(value)
