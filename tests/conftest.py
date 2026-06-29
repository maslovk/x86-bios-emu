"""Shared fixtures for simple-bios tests."""

import sys
import os
import pytest

# Ensure parent dir is on path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from cpu import CPU
from video import Video, IO, Keyboard, Disk, Serial
from bios import BIOS


class Mem:
    """Minimal memory model for tests."""
    def __init__(self, size=0x100000):
        self.ram = bytearray(size)

    def read_byte(self, a):
        return self.ram[a & 0xFFFFF]

    def read_word(self, a):
        a &= 0xFFFFF
        return self.ram[a] | (self.ram[a + 1] << 8)

    def read_dword(self, a):
        a &= 0xFFFFF
        return (self.ram[a] | (self.ram[a + 1] << 8) |
                self.ram[a + 2] << 16 | (self.ram[a + 3] << 24))

    def write_byte(self, a, v):
        self.ram[a & 0xFFFFF] = v & 0xFF

    def write_word(self, a, v):
        a &= 0xFFFFF
        self.ram[a] = v & 0xFF
        self.ram[a + 1] = (v >> 8) & 0xFF

    def write_dword(self, a, v):
        a &= 0xFFFFF
        for i in range(4):
            self.ram[a + i] = (v >> (i * 8)) & 0xFF


@pytest.fixture
def memory():
    """Fresh 1MB memory."""
    return Mem()


@pytest.fixture
def kbd():
    """Keyboard with no keys."""
    return Keyboard()


@pytest.fixture
def disk():
    """Empty floppy disk."""
    return Disk()


@pytest.fixture
def serial():
    """Serial port."""
    return Serial()


@pytest.fixture
def video():
    """VGA text mode."""
    return Video()


@pytest.fixture
def io_ports(video, kbd, disk, serial):
    """I/O ports wired to video, keyboard, disk, serial."""
    return IO(video, kbd, disk, serial)


@pytest.fixture
def cpu(memory, io_ports):
    """Fresh CPU with default reset state."""
    return CPU(memory, io_ports)


@pytest.fixture
def bios_env(memory, video, kbd, disk):
    """Full BIOS environment."""
    return BIOS(memory, video, kbd, disk)
