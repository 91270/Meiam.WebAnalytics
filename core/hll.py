#!/usr/bin/python3
# coding: utf-8
"""无第三方依赖的 HyperLogLog，用于低内存 UV/IP 近似去重。"""

from __future__ import annotations

import hashlib
import math
import zlib
from typing import Iterable, Optional


class HyperLogLog:
    def __init__(self, precision: int = 10, registers: Optional[Iterable[int]] = None):
        if precision < 4 or precision > 16:
            raise ValueError("precision must be between 4 and 16")
        self.precision = int(precision)
        self.size = 1 << self.precision
        self.registers = bytearray(registers or bytes(self.size))
        if len(self.registers) != self.size:
            raise ValueError("invalid register count")

    def add(self, value: str) -> None:
        digest = hashlib.blake2b(value.encode("utf-8", "ignore"), digest_size=8).digest()
        number = int.from_bytes(digest, "big")
        index = number >> (64 - self.precision)
        remaining_bits = 64 - self.precision
        remainder = number & ((1 << remaining_bits) - 1)
        rank = remaining_bits - remainder.bit_length() + 1
        if rank > self.registers[index]:
            self.registers[index] = min(255, rank)

    def merge(self, other: "HyperLogLog") -> None:
        if other.precision != self.precision:
            raise ValueError("cannot merge different precisions")
        for index, value in enumerate(other.registers):
            if value > self.registers[index]:
                self.registers[index] = value

    def estimate(self) -> int:
        size = float(self.size)
        if self.size == 16:
            alpha = 0.673
        elif self.size == 32:
            alpha = 0.697
        elif self.size == 64:
            alpha = 0.709
        else:
            alpha = 0.7213 / (1.0 + 1.079 / size)
        raw = alpha * size * size / sum(2.0 ** (-value) for value in self.registers)
        zeros = self.registers.count(0)
        if raw <= 2.5 * size and zeros:
            raw = size * math.log(size / float(zeros))
        return max(0, int(round(raw)))

    def dumps(self) -> bytes:
        return zlib.compress(bytes(self.registers), 6)

    @classmethod
    def loads(cls, payload: bytes, precision: int = 10) -> "HyperLogLog":
        return cls(precision, zlib.decompress(bytes(payload)))
