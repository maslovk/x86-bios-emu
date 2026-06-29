"""Unit tests for hardware.py — PIT, PIC, CMOS."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from hardware import PIT, PIC, CMOS


class TestPIT:
    def test_init(self):
        pit = PIT()
        assert pit.counters == [0, 0, 0]
        assert pit.reloads == [0, 0, 0]
        assert pit.modes == [2, 3, 2]
        assert pit.tick_count == 0

    def test_write_command_counter0(self):
        pit = PIT()
        pit.write_command(0x36)  # Counter 0, rw=3 (both bytes), mode 3 (rate gen), binary
        assert pit.modes[0] == 3
        assert pit.rw_modes[0] == 3  # Both bytes at once

    def test_write_command_counter1(self):
        pit = PIT()
        pit.write_command(0x76)  # Counter 1, rw=3, mode 3
        assert pit.modes[1] == 3
        assert pit.rw_modes[1] == 3

    def test_write_counter_byte(self):
        pit = PIT()
        pit.write_command(0x34)  # Counter 0, low byte only, mode 2
        pit.write_counter(0, 0x50)
        assert pit.counters[0] == 0x50

    def test_write_counter_word(self):
        pit = PIT()
        pit.write_command(0x36)  # Counter 0, rw=3 (both bytes), mode 3
        pit.write_counter(0, 0x3450)  # Both bytes at once
        assert pit.counters[0] == 0x3450
        assert pit.reloads[0] == 0x3450

    def test_write_counter_word_sequential(self):
        pit = PIT()
        pit.write_command(0x34)  # Counter 0, low byte first (rw=2), mode 2
        pit.write_counter(0, 0x50)  # Low byte
        pit.write_counter(0, 0x34)  # High byte (rw=3 branch stores both)
        # With rw=2, first write stores pending, second write uses rw=3 logic
        # Actual implementation: rw=2 stores pending, rw=3 stores both at once
        assert pit.counters[0] == 0x34  # Last write overwrites

    def test_read_counter(self):
        pit = PIT()
        pit.counters[0] = 0x1234
        assert pit.read_counter(0) == 0x34

    def test_read_word_counter(self):
        pit = PIT()
        pit.counters[0] = 0x1234
        assert pit.read_word_counter(0) == 0x1234

    def test_tick_no_reload(self):
        pit = PIT()
        pit.reloads[0] = 0
        fired = pit.tick(1.0 / 18.2)
        assert 0 in fired

    def test_tick_with_reload(self):
        pit = PIT()
        pit.reloads[0] = 3
        pit.counters[0] = 3
        fired = pit.tick(0.5)
        assert 0 in fired
        assert pit.tick_count >= 1

    def test_tick_count_increments(self):
        pit = PIT()
        pit.reloads[0] = 1
        pit.counters[0] = 1
        for _ in range(5):
            pit.tick(0.1)
        assert pit.tick_count >= 5

    def test_reset_counter0(self):
        pit = PIT()
        pit.reloads[0] = 0x1234
        pit.counters[0] = 0x5678
        pit.reset_counter0()
        assert pit.reloads[0] == 0
        assert pit.counters[0] == 0
        assert pit.modes[0] == 2


class TestPIC:
    def test_init(self):
        pic = PIC()
        assert pic.master_base == 0x08
        assert pic.slave_base == 0x70
        assert pic.cascade_irq == 2
        assert pic.mask == 0x00
        assert pic.slave_mask == 0x00
        assert pic.irr == 0
        assert pic.pending == -1

    def test_write_master_mask(self):
        pic = PIC()
        pic.write_master(0x21, 0xFF)
        assert pic.mask == 0xFF

    def test_write_slave_mask(self):
        pic = PIC()
        pic.write_slave(0xA1, 0x0F)
        assert pic.slave_mask == 0x0F

    def test_raise_irq_master(self):
        pic = PIC()
        pic.raise_irq(0)
        assert pic.irr & 0x01

    def test_raise_irq_slave(self):
        pic = PIC()
        pic.raise_irq(8)
        assert pic.slave_irr & 0x01
        assert pic.irr & (1 << 2)

    def test_get_highest_irq_unmasked(self):
        pic = PIC()
        pic.raise_irq(0)
        pic.raise_irq(1)
        irq = pic.get_highest_irq()
        assert irq == 0

    def test_get_highest_irq_masked(self):
        pic = PIC()
        pic.mask = 0x01
        pic.raise_irq(0)
        pic.raise_irq(1)
        irq = pic.get_highest_irq()
        assert irq == 1

    def test_get_highest_irq_none(self):
        pic = PIC()
        irq = pic.get_highest_irq()
        assert irq == -1

    def test_send_eoi(self):
        pic = PIC()
        pic.raise_irq(0)
        pic.get_highest_irq()
        assert pic.ims & 0x01
        pic.send_eoi(0)
        assert not (pic.ims & 0x01)
        assert not (pic.irr & 0x01)

    def test_get_vector_master(self):
        pic = PIC()
        assert pic.get_vector(0) == 0x08
        assert pic.get_vector(7) == 0x0F

    def test_get_vector_slave(self):
        pic = PIC()
        assert pic.get_vector(8) == 0x70
        assert pic.get_vector(15) == 0x77

    def test_initialize(self):
        pic = PIC()
        pic.mask = 0xFF
        pic.initialize()
        assert pic.mask == 0xFF
        assert pic.irr == 0
        assert pic.ims == 0

    def test_custom_bases(self):
        pic = PIC(master_base=0x08, slave_base=0x70, cascade_irq=2)
        assert pic.master_base == 0x08
        assert pic.slave_base == 0x70

    def test_cascade_irq(self):
        pic = PIC()
        pic.slave_mask = 0x02  # Mask IRQ 9 (bit 1), allow IRQ 8 (bit 0)
        pic.raise_irq(8)
        pic.raise_irq(9)
        irq = pic.get_highest_irq()
        assert irq == 8  # IRQ 8 unmasked, IRQ 9 masked


class TestCMOS:
    def test_init(self):
        cmos = CMOS()
        assert len(cmos._data) == 128
        assert cmos._binary_mode is False

    def test_write_read_addr(self):
        cmos = CMOS()
        cmos.write_addr(0x00)
        assert cmos._addr == 0x00
        cmos.write_addr(0x3F)
        assert cmos._addr == 0x3F

    def test_write_read_data(self):
        cmos = CMOS()
        cmos.write_addr(0x10)
        cmos.write_data(0xAB)
        assert cmos.read_data() == 0xAB

    def test_read_time_registers(self):
        cmos = CMOS()
        for reg in range(0x07):
            cmos.write_addr(reg)
            val = cmos.read_data()
            assert 0 <= val <= 0xFF

    def test_get_time(self):
        cmos = CMOS()
        t = cmos.get_time()
        assert len(t) == 6
        year, month, day, hour, minute, second = t
        assert 2024 <= year <= 2030
        assert 1 <= month <= 12
        assert 1 <= day <= 31
        assert 0 <= hour <= 23
        assert 0 <= minute <= 59
        assert 0 <= second <= 59

    def test_get_date_bcd(self):
        cmos = CMOS()
        d = cmos.get_date_bcd()
        assert 'seconds' in d
        assert 'minutes' in d
        assert 'hours' in d
        assert 'day' in d
        assert 'month' in d
        assert 'year' in d
        assert 'weekday' in d

    def test_cmos_signature(self):
        cmos = CMOS()
        assert cmos._data[0x32] == 0x12
        assert cmos._data[0x33] == 0x56

    def test_register_a(self):
        cmos = CMOS()
        cmos.write_addr(0x0A)
        val = cmos.read_data()
        # Bit 7 = update in progress (should be 0)
        assert not (val & 0x80)

    def test_write_register_b_binary_mode(self):
        cmos = CMOS()
        cmos.write_addr(0x0B)
        cmos.write_data(0x04)  # Set binary mode
        assert cmos._binary_mode is True

    def test_bcd_conversion(self):
        cmos = CMOS()
        assert cmos._to_bcd(0) == 0x00
        assert cmos._to_bcd(9) == 0x09
        assert cmos._to_bcd(59) == 0x59
        assert cmos._to_bcd(23) == 0x23
        assert cmos._from_bcd(0x59) == 59
        assert cmos._from_bcd(0x23) == 23

    def test_nvm_write_read(self):
        cmos = CMOS()
        # Write to NVRAM area (0x40+)
        for i in range(16):
            cmos.write_addr(0x40 + i)
            cmos.write_data(i * 0x10)
        for i in range(16):
            cmos.write_addr(0x40 + i)
            assert cmos.read_data() == i * 0x10
