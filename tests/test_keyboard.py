"""
Simple BIOS Emulator - Keyboard Controller (8042) Tests
========================================================
Tests for scan code handling, modifier state, ASCII translation,
port I/O, IRQ generation, and shift/caps lock behavior.
"""

import pytest
from hardware import KeyboardController


class TestKeyboardControllerInit:
    """Keyboard controller initialization and defaults."""

    def test_default_state(self):
        kbd = KeyboardController()
        assert kbd.shift is False
        assert kbd.ctrl is False
        assert kbd.alt is False
        assert kbd.caps_lock is False
        assert kbd.num_lock is True
        assert kbd.scroll_lock is False
        assert kbd.irq_pending is False
        assert kbd.has_data() is False

    def test_shift_state_default(self):
        kbd = KeyboardController()
        # Num lock is on by default → bit 5 set
        assert kbd.shift_state == 0x20

    def test_no_data_on_init(self):
        kbd = KeyboardController()
        assert kbd.read_data() == 0


class TestScanCodeInjection:
    """Raw scan code injection and translation."""

    def test_simple_letter_a(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)  # 'a' make code
        assert kbd.has_data() is True
        assert kbd.irq_pending is True
        data = kbd.read_data()
        assert data == ord('a')
        assert kbd.has_data() is False

    def test_simple_letter_z(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2C)  # 'z' make code
        assert kbd.read_data() == ord('z')

    def test_digit_1(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x02)  # '1' make code
        assert kbd.read_data() == ord('1')

    def test_digit_0(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x0B)  # '0' make code
        assert kbd.read_data() == ord('0')

    def test_enter(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1C)  # Enter
        assert kbd.read_data() == 0x0D

    def test_backspace(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x0E)  # Backspace
        assert kbd.read_data() == 0x08

    def test_tab(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x0F)  # Tab
        assert kbd.read_data() == 0x09

    def test_space(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2B)  # Space
        assert kbd.read_data() == ord(' ')

    def test_escape(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x01)  # Escape
        assert kbd.read_data() == 0x1B

    def test_break_code_no_output(self):
        """Break codes (key release) should not produce output."""
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)  # 'a' make
        assert kbd.read_data() == ord('a')
        kbd.inject_scan_code(0x9E)  # 'a' break (0x1E | 0x80)
        assert kbd.has_data() is False

    def test_unknown_scan_code_passthrough(self):
        """Unknown scan codes pass through as-is."""
        kbd = KeyboardController()
        kbd.inject_scan_code(0x7F)  # F10 (mapped to space)
        data = kbd.read_data()
        assert data == ord(' ')  # F10 is mapped


class TestShiftModifier:
    """Shift key behavior with scan codes."""

    def test_shift_plus_letter(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2A)  # Left Shift make
        assert kbd.shift is True
        kbd.inject_scan_code(0x1E)  # 'a' make
        assert kbd.read_data() == ord('A')
        kbd.inject_scan_code(0xAA)  # Left Shift break
        assert kbd.shift is False

    def test_right_shift(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x36)  # Right Shift make
        assert kbd.shift is True
        kbd.inject_scan_code(0x1E)  # 'a'
        assert kbd.read_data() == ord('A')

    def test_shift_plus_digit(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2A)  # Shift make
        kbd.inject_scan_code(0x02)  # '1'
        assert kbd.read_data() == ord('!')

    def test_shift_plus_digit_2(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2A)  # Shift make
        kbd.inject_scan_code(0x03)  # '2'
        assert kbd.read_data() == ord('@')

    def test_shift_plus_dash(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2A)  # Shift make
        kbd.inject_scan_code(0x0C)  # '-'
        assert kbd.read_data() == ord('_')

    def test_shift_state_byte(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2A)  # Shift make
        state = kbd.shift_state
        assert state & 0x02  # Shift bit set
        assert state & 0x20  # Num lock bit set (default)


class TestCtrlModifier:
    """Ctrl key state tracking."""

    def test_left_ctrl(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1D)  # Left Ctrl make
        assert kbd.ctrl is True
        kbd.inject_scan_code(0x9D)  # Left Ctrl break
        assert kbd.ctrl is False

    def test_ctrl_state_byte(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1D)  # Ctrl make
        state = kbd.shift_state
        assert state & 0x04  # Ctrl bit set


class TestAltModifier:
    """Alt key state tracking (extended scan codes)."""

    def test_left_alt(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0xE0)  # Extended prefix
        kbd.inject_scan_code(0x5B)  # Left Alt make
        assert kbd.alt is True
        kbd.inject_scan_code(0xE0)  # Extended prefix
        kbd.inject_scan_code(0xDB)  # Left Alt break (0x5B | 0x80)
        assert kbd.alt is False

    def test_alt_state_byte(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0xE0)
        kbd.inject_scan_code(0x5B)
        state = kbd.shift_state
        assert state & 0x08  # Alt bit set


class TestCapsLock:
    """Caps Lock toggle behavior."""

    def test_caps_lock_toggle(self):
        kbd = KeyboardController()
        assert kbd.caps_lock is False
        kbd.inject_scan_code(0x3A)  # Caps Lock make
        assert kbd.caps_lock is True
        kbd.inject_scan_code(0x3A)  # Toggle off
        assert kbd.caps_lock is False

    def test_caps_lock_letter(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x3A)  # Caps Lock on
        kbd.inject_scan_code(0x1E)  # 'a'
        assert kbd.read_data() == ord('A')

    def test_caps_lock_plus_shift(self):
        """Shift + Caps Lock = lowercase (inverted)."""
        kbd = KeyboardController()
        kbd.inject_scan_code(0x3A)  # Caps Lock on
        kbd.inject_scan_code(0x2A)  # Shift on
        kbd.inject_scan_code(0x1E)  # 'a'
        assert kbd.read_data() == ord('a')  # Lowercase with shift+caps

    def test_caps_lock_state_byte(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x3A)  # Caps Lock on
        state = kbd.shift_state
        assert state & 0x40  # Caps Lock bit set


class TestNumLock:
    """Num Lock toggle."""

    def test_num_lock_toggle(self):
        kbd = KeyboardController()
        assert kbd.num_lock is True
        kbd.inject_scan_code(0x70)  # Num Lock make
        assert kbd.num_lock is False
        kbd.inject_scan_code(0x70)  # Toggle on
        assert kbd.num_lock is True


class TestScrollLock:
    """Scroll Lock toggle."""

    def test_scroll_lock_toggle(self):
        kbd = KeyboardController()
        assert kbd.scroll_lock is False
        kbd.inject_scan_code(0x45)  # Scroll Lock make
        assert kbd.scroll_lock is True
        kbd.inject_scan_code(0x45)  # Toggle off
        assert kbd.scroll_lock is False


class TestASCIIInjection:
    """Direct ASCII character injection."""

    def test_inject_a(self):
        kbd = KeyboardController()
        kbd.inject_key(ord('a'))
        assert kbd.read_data() == ord('a')

    def test_inject_enter(self):
        kbd = KeyboardController()
        kbd.inject_key(0x0D)
        assert kbd.read_data() == 0x0D

    def test_inject_space(self):
        kbd = KeyboardController()
        kbd.inject_key(ord(' '))
        assert kbd.read_data() == ord(' ')

    def test_inject_unmapped_char(self):
        """Unmapped ASCII chars pass through directly."""
        kbd = KeyboardController()
        kbd.inject_key(0x7F)  # DEL
        assert kbd.read_data() == 0x7F

    def test_feed_string(self):
        kbd = KeyboardController()
        kbd.feed_string("Hi")
        assert kbd.read_data() == ord('H')
        assert kbd.read_data() == ord('i')
        assert kbd.read_data() == 0x0D  # Auto Enter


class TestPortIO:
    """Port 0x60 (data) and 0x64 (status/command) I/O."""

    def test_status_no_data(self):
        kbd = KeyboardController()
        status = kbd.read_status()
        assert not (status & 0x01)  # OBF clear
        assert not (status & 0x02)  # IBF clear

    def test_status_with_data(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)
        status = kbd.read_status()
        assert status & 0x01  # OBF set

    def test_status_irq_pending(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)
        status = kbd.read_status()
        assert status & 0x80  # IRQ pending bit

    def test_status_system_flag(self):
        kbd = KeyboardController()
        status = kbd.read_status()
        assert status & 0x20  # System flag always set

    def test_read_data_clears_obf(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)
        assert kbd.read_status() & 0x01
        kbd.read_data()
        assert not (kbd.read_status() & 0x01)

    def test_self_test(self):
        kbd = KeyboardController()
        kbd.write_command(0xAA)  # Self-test
        assert kbd.read_data() == 0x55

    def test_disable_irq(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)
        assert kbd.irq_pending is True
        kbd.write_command(0xAD)  # Disable keyboard IRQ
        assert kbd.irq_pending is False

    def test_enable_irq(self):
        kbd = KeyboardController()
        kbd.write_command(0xAE)  # Enable keyboard IRQ
        # Should not crash

    def test_read_input_port(self):
        kbd = KeyboardController()
        kbd.write_command(0xD0)  # Read input port
        assert kbd.read_data() == 0x00

    def test_read_command_byte(self):
        kbd = KeyboardController()
        kbd.write_command(0x20)  # Read command byte
        assert kbd.read_data() == 0x9D

    def test_write_data_reset(self):
        kbd = KeyboardController()
        kbd.write_data(0xFF)  # Reset keyboard
        assert kbd.read_data() == 0xAA

    def test_led_command(self):
        kbd = KeyboardController()
        kbd.write_data(0xED)  # Set LEDs command
        kbd.write_data(0x04)  # Scroll Lock on
        assert kbd.scroll_lock is True

    def test_led_caps_lock(self):
        kbd = KeyboardController()
        kbd.write_data(0xED)
        kbd.write_data(0x01)  # Caps Lock on
        assert kbd.caps_lock is True

    def test_led_num_lock(self):
        kbd = KeyboardController()
        kbd.write_data(0xED)
        kbd.write_data(0x02)  # Num Lock on
        assert kbd.num_lock is True


class TestIRQGeneration:
    """IRQ 1 generation and clearing."""

    def test_irq_on_scan_code(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)
        assert kbd.irq_pending is True

    def test_irq_on_ascii_inject(self):
        kbd = KeyboardController()
        kbd.inject_key(ord('X'))
        assert kbd.irq_pending is True

    def test_irq_cleared_on_read(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)
        assert kbd.irq_pending is True
        kbd.read_data()
        assert kbd.irq_pending is False

    def test_irq_not_set_on_break(self):
        """Break codes should not set IRQ."""
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)  # Make
        kbd.read_data()  # Clear
        kbd.inject_scan_code(0x9E)  # Break
        assert kbd.irq_pending is False

    def test_irq_not_set_on_modifier(self):
        """Modifier make codes should not set IRQ."""
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2A)  # Shift make
        assert kbd.irq_pending is False


class TestExtendedScanCodes:
    """E0 prefix extended scan codes."""

    def test_e0_right_ctrl(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0xE0)
        kbd.inject_scan_code(0x1F)  # Right Ctrl
        # Should produce the mapped char
        assert kbd.has_data() is True

    def test_e0_enter(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0xE0)
        kbd.inject_scan_code(0x1C)  # Numpad Enter
        assert kbd.read_data() == 0x0D


class TestMultipleKeys:
    """Multiple key sequences."""

    def test_two_keys(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)  # 'a'
        kbd.inject_scan_code(0x1F)  # 's'
        assert kbd.read_data() == ord('a')
        assert kbd.read_data() == ord('s')

    def test_shift_word(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x2A)  # Shift
        kbd.inject_scan_code(0x1E)  # 'a' → 'A'
        kbd.inject_scan_code(0x1F)  # 's' → 'S'
        kbd.inject_scan_code(0x20)  # 'd' → 'D'
        kbd.inject_scan_code(0xAA)  # Shift release
        assert kbd.read_data() == ord('A')
        assert kbd.read_data() == ord('S')
        assert kbd.read_data() == ord('D')

    def test_word_with_break(self):
        kbd = KeyboardController()
        kbd.inject_scan_code(0x1E)  # 'a' make
        kbd.inject_scan_code(0x9E)  # 'a' break
        kbd.inject_scan_code(0x1F)  # 's' make
        kbd.inject_scan_code(0x9F)  # 's' break
        assert kbd.read_data() == ord('a')
        assert kbd.read_data() == ord('s')
        assert kbd.has_data() is False
