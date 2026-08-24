"""
Simple BIOS Emulator - Hardware Devices
========================================
PIT (8254), PIC (8259A), CMOS RTC (MC146818), Keyboard Controller (8042).
"""

import time
import datetime


# ═══════════════════════════════════════════════════════════════════════════
# Keyboard Controller (i8042)
# ═══════════════════════════════════════════════════════════════════════════

_SCANCODE_MAP = {
    0x01: (0x1B, 0x1B),
    0x02: ('1', '!'), 0x03: ('2', '@'), 0x04: ('3', '#'),
    0x05: ('4', '$'), 0x06: ('5', '%'), 0x07: ('6', '^'),
    0x08: ('7', '&'), 0x09: ('8', '*'), 0x0A: ('9', '('),
    0x0B: ('0', ')'), 0x0C: ('-', '_'), 0x0D: ('=', '+'),
    0x0E: (0x08, 0x08), 0x0F: (0x09, 0x09),
    0x10: ('q', 'Q'), 0x11: ('w', 'W'), 0x12: ('e', 'E'),
    0x13: ('r', 'R'), 0x14: ('t', 'T'), 0x15: ('y', 'Y'),
    0x16: ('u', 'U'), 0x17: ('i', 'I'), 0x18: ('o', 'O'),
    0x19: ('p', 'P'), 0x1A: ('[', '{'), 0x1B: (']', '}'),
    0x1C: (0x0D, 0x0D),
    0x1E: ('a', 'A'), 0x1F: ('s', 'S'), 0x20: ('d', 'D'),
    0x21: ('f', 'F'), 0x22: ('g', 'G'), 0x23: ('h', 'H'),
    0x24: ('j', 'J'), 0x25: ('k', 'K'), 0x26: ('l', 'L'),
    0x27: (';', ':'), 0x28: ("'", '"'), 0x29: ('`', '~'),
    0x2B: ('\\', '|'),
    0x2C: ('z', 'Z'), 0x2D: ('x', 'X'), 0x2E: ('c', 'C'),
    0x2F: ('v', 'V'), 0x30: ('b', 'B'), 0x31: ('n', 'N'),
    0x32: ('m', 'M'), 0x33: (',', '<'), 0x34: ('.', '>'),
    0x35: ('/', '?'), 0x37: ('*', '*'), 0x39: (' ', ' '),
    0x47: ('7', '7'), 0x48: ('8', '8'), 0x49: ('9', '9'),
    0x4A: ('-', '-'), 0x4B: ('4', '4'), 0x4C: ('5', '5'),
    0x4D: ('6', '6'), 0x4E: ('+', '+'), 0x4F: ('1', '1'),
    0x50: ('2', '2'), 0x51: ('3', '3'), 0x52: ('0', '0'),
    0x53: ('.', '.'),
}

_E0_MAP = {
    0x1C: (0x0D, 0x0D), 0x35: ('/', '/'),
    0x47: (0, 0), 0x48: (0, 0), 0x49: (0, 0),
    0x4B: (0, 0), 0x4D: (0, 0), 0x4F: (0, 0),
    0x50: (0, 0), 0x51: (0, 0), 0x52: (0, 0),
    0x53: (0, 0),
}

_FUNCTION_KEYS = set(range(0x3B, 0x45)) | {0x57, 0x58}
_MOD_KEYS = {0x2A, 0x36, 0x1D, 0x38, 0x3A, 0x45, 0x46}
_KEYPAD_DIGIT_KEYS = set(range(0x47, 0x54)) - {0x4A, 0x4E}


class KeyboardController:
    """i8042 PS/2 Keyboard Controller.

    Ports:
        0x60  Data port (read scan code / ASCII, write command/data)
        0x64  Status/command port (read status, write command)

    Generates IRQ 1 when a character is available in the output buffer.
    Tracks shift/Ctrl/Alt/CapsLock/NumLock/ScrollLock state.
    """

    def __init__(self):
        self._out_buffer = []       # FIFO of translated ASCII chars
        self._scan_buffer = []      # matching scan code (or None for ASCII)
        self._raw_buffer = []       # host events exposed as raw port bytes
        self._out_full = False
        self._in_buffer = None
        self._in_full = False
        self._scan_fifo = []

        # Modifier state
        self.shift = False
        self.ctrl = False
        self.alt = False
        self.caps_lock = False
        self.num_lock = True
        self.scroll_lock = False

        self._ext_prefix = False
        self.irq_pending = False
        self._cmd_mode = False
        self._led_select = 0
        self._self_test_ok = 0x55

    def read_status(self):
        """Read status register (port 0x64)."""
        status = 0x00
        if len(self._out_buffer) > 0:
            status |= 0x01
        if self._in_full:
            status |= 0x02
        if self._cmd_mode:
            status |= 0x08
        status |= 0x20
        if self.irq_pending:
            status |= 0x80
        return status

    def write_command(self, val):
        """Write to command port (port 0x64)."""
        self._in_buffer = val
        self._in_full = True

        if val == 0xAA:
            self._queue_output(self._self_test_ok)
        elif val == 0xAD:
            self.irq_pending = False
        elif val == 0xAE:
            pass  # Enable keyboard interrupt
        elif val == 0xD0:
            self._queue_output(0x00)
        elif val == 0xD1:
            self._cmd_mode = True
            self._in_full = False
        elif val == 0x20:
            self._queue_output(0x9D)
        elif val == 0x60:
            self._cmd_mode = True
            self._in_full = False
        elif val == 0xAB:
            self._in_full = False
        else:
            self._in_full = False

    def read_data(self):
        """Read translated ASCII from the controller (test/API helper)."""
        _scan, ascii_value, _raw = self._read_event()
        return ascii_value

    def read_port_data(self):
        """Read the guest-visible byte from port 60h.

        Host-injected keys carry both a convenient ASCII value for the BIOS
        event path and the raw set-1 scan code that a DOS IRQ09 handler sees.
        """
        scan, ascii_value, raw = self._read_event()
        # Guest DOS IRQ handlers read the raw 8042 data port rather than
        # calling INT 16h. Enhanced keys have no ASCII byte, so expose their
        # set-1 scan code as real hardware does. Printable host injections
        # and translated scan codes retain the public ASCII behavior.
        if raw and scan is not None:
            return scan
        return ascii_value

    def read_key_event(self):
        """Return ``(scan_code, ascii)`` while consuming one output byte.

        Host-injected ASCII includes its best-effort physical scan code.
        Raw/extended injection preserves the supplied scan code so INT 16h
        can distinguish modifier chords, arrows, and function keys.
        """
        scan, value, _raw = self._read_event()
        return scan, value

    def _read_event(self):
        if self._cmd_mode:
            self._cmd_mode = False
        if self._out_buffer:
            val = self._out_buffer.pop(0)
            scan = self._scan_buffer.pop(0)
            raw = self._raw_buffer.pop(0)
        else:
            val, scan, raw = 0x00, None, False
        if not self._out_buffer:
            self._out_full = False
            self.irq_pending = False
        return scan, val, raw

    def _queue_output(self, value, scan_code=None, raw=False):
        self._out_buffer.append(value & 0xFF)
        self._scan_buffer.append(scan_code)
        self._raw_buffer.append(bool(raw))
        self._out_full = True

    def write_data(self, val):
        """Write to data port (port 0x60)."""
        if self._cmd_mode:
            self._cmd_mode = False
            if self._led_select:
                self.caps_lock = bool(val & 0x01)
                self.num_lock = bool(val & 0x02)
                self.scroll_lock = bool(val & 0x04)
                self._led_select = 0
            return

        if val == 0xED:
            self._led_select = 1
            self._cmd_mode = True
        elif val == 0xF3:
            self._cmd_mode = True
        elif val == 0xF0:
            pass
        elif val == 0xFE:
            if self._out_buffer:
                self._out_full = True
        elif val == 0xFF:
            self._queue_output(0xAA)

    def inject_scan_code(self, code):
        """Inject a raw scan code (make 0x00-0x80, break 0x80+)."""
        self._scan_fifo.append(code)
        self._process_fifo()

    def inject_key(self, ascii_char):
        """Inject an ASCII character directly into output buffer.

        Bypasses scan code translation — the exact ASCII value is buffered.
        """
        self._queue_output(ascii_char, self._ascii_to_scan(ascii_char), raw=True)
        self.irq_pending = True

    def inject_extended_key(self, scan_code):
        """Inject a non-ASCII enhanced key (e.g. an arrow key).

        Enhanced BIOS reads return AL=00h and the set-1 scan code in AH.
        """
        self._queue_output(0x00, scan_code & 0xFF, raw=True)
        self.irq_pending = True

    def _ascii_to_scan(self, ascii_val):
        """Find a scan code that produces the given ASCII character."""
        if ascii_val == 0x0D:
            return 0x1C
        if ascii_val == 0x08:
            return 0x0E
        if ascii_val == 0x09:
            return 0x0F
        if ascii_val == 0x20:
            return 0x39
        if ascii_val == 0x1B:
            return 0x01
        for sc, (lo, hi) in _SCANCODE_MAP.items():
            lo_c = lo if isinstance(lo, int) else ord(lo)
            hi_c = hi if isinstance(hi, int) else ord(hi)
            if lo_c == ascii_val or hi_c == ascii_val:
                return sc
        return None

    def _process_fifo(self):
        """Process one scan code from FIFO → output buffer + modifier state.

        Only processes one character-producing scan code per call.
        Modifier and prefix codes are processed inline.
        """
        while self._scan_fifo:
            code = self._scan_fifo.pop(0)

            if code == 0xE0:
                self._ext_prefix = True
                continue

            extended = self._ext_prefix
            self._ext_prefix = False

            if code & 0x80:
                self._handle_modifier_release(code & 0x7F, extended)
                continue

            if code in _MOD_KEYS:
                self._handle_modifier_make(code, extended)
                continue

            if not extended and code in _FUNCTION_KEYS:
                scan_code = code
                if code <= 0x44:
                    if self.alt:
                        scan_code += 0x2D
                    elif self.ctrl:
                        scan_code += 0x23
                    elif self.shift:
                        scan_code += 0x19
                elif self.alt:
                    scan_code = 0x8B + (code - 0x57)
                elif self.ctrl:
                    scan_code = 0x89 + (code - 0x57)
                elif self.shift:
                    scan_code = 0x87 + (code - 0x57)
                self._queue_output(0, scan_code, raw=True)
                self.irq_pending = True
                return

            if extended:
                entry = _E0_MAP.get(code)
            else:
                entry = _SCANCODE_MAP.get(code)

            if entry:
                lo, hi = entry
                lo_c = lo if isinstance(lo, int) else ord(lo)
                hi_c = hi if isinstance(hi, int) else ord(hi)
                ascii_val = self._apply_modifiers(lo_c, hi_c, code)
                self._queue_output(ascii_val, code, raw=True)
                self.irq_pending = True
                return  # Only process one char-producing code per call
            else:
                self._queue_output(0, code, raw=True)
                self.irq_pending = True
                return

    def _apply_modifiers(self, lower, upper, scan_code):
        """Apply PC BIOS Shift/Ctrl/Alt translation to a keypress."""
        if self.alt:
            return 0
        if self.ctrl:
            if 0x61 <= lower <= 0x7A:
                return lower - 0x60
            ctrl_chars = {
                0x1A: 0x1B,  # [ -> ESC
                0x1B: 0x1D,  # ] -> GS
                0x2B: 0x1C,  # backslash -> FS
                0x0C: 0x1F,  # minus/underscore key -> US
            }
            return ctrl_chars.get(scan_code, 0)
        if scan_code in _KEYPAD_DIGIT_KEYS:
            # Shift temporarily reverses Num Lock, matching the PC BIOS.
            return lower if self.num_lock != self.shift else 0
        if self.shift and scan_code == 0x0F:  # Shift+Tab is an extended key
            return 0
        is_alpha = (0x61 <= lower <= 0x7A) or (0x41 <= lower <= 0x5A)
        if is_alpha and self.caps_lock:
            return lower if self.shift else upper
        elif self.shift:
            return upper
        else:
            return lower

    def _handle_modifier_make(self, code, extended=False):
        if code in (0x2A, 0x36) and not extended:
            self.shift = True
        elif code == 0x1D:
            self.ctrl = True
        elif code == 0x38:
            self.alt = True
        elif code == 0x3A and not extended:
            self.caps_lock = not self.caps_lock
        elif code == 0x45 and not extended:
            self.num_lock = not self.num_lock
        elif code == 0x46 and not extended:
            self.scroll_lock = not self.scroll_lock

    def _handle_modifier_release(self, code, extended=False):
        if code in (0x2A, 0x36) and not extended:
            self.shift = False
        elif code == 0x1D:
            self.ctrl = False
        elif code == 0x38:
            self.alt = False

    def feed_string(self, s):
        """Feed a string of ASCII characters."""
        for ch in s:
            self.inject_key(ord(ch))
        self.inject_key(0x0D)

    def has_data(self):
        """Check if output buffer has data."""
        return len(self._out_buffer) > 0

    @property
    def shift_state(self):
        """Shift state byte for INT 16h AH=02h."""
        state = 0
        if self.shift:
            state |= 0x02
        if self.ctrl:
            state |= 0x04
        if self.alt:
            state |= 0x08
        if self.scroll_lock:
            state |= 0x10
        if self.num_lock:
            state |= 0x20
        if self.caps_lock:
            state |= 0x40
        return state


# ═══════════════════════════════════════════════════════════════════════════
# PIT (8254)
# ═══════════════════════════════════════════════════════════════════════════

class PIT:
    """8254 Programmable Interval Timer.

    Three 16-bit counters, 1.193180 MHz input clock.
    Counter 0 → IRQ 0 (system timer, ~18.2 Hz)
    Counter 1 → VGA DAC (not emulated)
    Counter 2 → Speaker (not emulated yet)

    Ports: 0x40 (Counter 0), 0x41 (Counter 1), 0x42 (Counter 2), 0x43 (Command)
    """

    INPUT_CLK = 1_193_180

    def __init__(self):
        self.counters = [0, 0, 0]
        self.reloads = [0, 0, 0]
        self.modes = [2, 3, 2]
        self.rw_modes = [0, 0, 0]
        self.irq_pending = [False, False, False]
        self._tick_accumulator = 0
        self._ticks = 0

    def write_command(self, val):
        counter = (val >> 6) & 3
        if counter == 3:
            return
        rw = (val >> 4) & 3
        mode = (val >> 1) & 7
        self.modes[counter] = mode
        self.rw_modes[counter] = rw

    def write_counter(self, counter, val):
        rw = self.rw_modes[counter]
        if rw == 0:
            self.reloads[counter] = (self.reloads[counter] & 0xFF00) | val
            self.counters[counter] = val
        elif rw == 1:
            self.reloads[counter] = (self.reloads[counter] & 0x00FF) | (val << 8)
            self.counters[counter] = self.reloads[counter]
        elif rw == 2:
            self._pending_val = val
        elif rw == 3:
            self.reloads[counter] = val & 0xFFFF
            self.counters[counter] = val & 0xFFFF

    def read_counter(self, counter):
        return self.counters[counter] & 0xFF

    def read_word_counter(self, counter):
        return self.counters[counter] & 0xFFFF

    def latch_counter(self, counter):
        pass

    def tick(self, dt):
        """Advance PIT. Returns list of fired IRQs."""
        self._tick_accumulator += dt * self.INPUT_CLK
        fired = []
        for i in range(3):
            while self._tick_accumulator >= 1.0:
                self._tick_accumulator -= 1.0
                self.counters[i] -= 1
                if self.counters[i] <= 0:
                    if i == 0:
                        self._ticks += 1
                    self.counters[i] = self.reloads[i] if self.reloads[i] else 0
                    self.irq_pending[i] = True
                    fired.append(i)
                    break
        return fired

    @property
    def tick_count(self):
        return self._ticks

    def reset_counter0(self):
        self.reloads[0] = 0
        self.counters[0] = 0
        self.modes[0] = 2
        self.rw_modes[0] = 1


# ═══════════════════════════════════════════════════════════════════════════
# PIC (8259A)
# ═══════════════════════════════════════════════════════════════════════════

class PIC:
    """8259A Programmable Interrupt Controller.

    Master: ports 0x20 (data/EOI), 0x21 (mask)
    Slave:  ports 0xA0 (data/EOI), 0xA1 (mask)
    IRQ 0-7 → Master → Vectors 0x08-0x0F
    IRQ 8-15 → Slave → Vectors 0x70-0x77 (cascaded via IRQ 2)
    """

    def __init__(self, master_base=0x08, slave_base=0x70, cascade_irq=2):
        self.master_base = master_base
        self.slave_base = slave_base
        self.cascade_irq = cascade_irq
        self.mask = 0x00
        self.slave_mask = 0x00
        self.irr = 0
        self.slave_irr = 0
        self.ims = 0
        self.slave_ims = 0
        self.pending = -1
        self._icw_state = 0
        self._need_icw4 = False
        self._auto_eoi = False

    def write_master(self, port, val):
        if port == 0x20:
            self._write_command(val)
        elif port == 0x21:
            self.mask = val

    def write_slave(self, port, val):
        if port == 0xA0:
            self._write_command_slave(val)
        elif port == 0xA1:
            self.slave_mask = val

    def _write_command(self, val):
        if val & 0x10:
            self._icw_state = 1
            self._need_icw4 = bool(val & 0x01)
            self._auto_eoi = False
        elif val < 0x08:
            pass
        else:
            self._send_eoi()

    def _write_command_slave(self, val):
        if val & 0x10:
            self._icw_state = 1
            self._need_icw4 = bool(val & 0x01)

    def _send_eoi(self):
        for i in range(8):
            if self.ims & (1 << i):
                self.ims &= ~(1 << i)
                break

    def send_eoi(self, irq):
        if irq < 8:
            self.ims &= ~(1 << irq)
            self.irr &= ~(1 << irq)
        else:
            i = irq - 8
            self.slave_ims &= ~(1 << i)
            self.slave_irr &= ~(1 << i)
            if self.cascade_irq >= 0:
                self.ims &= ~(1 << self.cascade_irq)
                self.irr &= ~(1 << self.cascade_irq)

    def raise_irq(self, irq):
        if irq < 8:
            self.irr |= (1 << irq)
        else:
            i = irq - 8
            self.slave_irr |= (1 << i)
            self.irr |= (1 << self.cascade_irq)

    def get_highest_irq(self):
        slave_pending = self.slave_irr & ~self.slave_mask
        if slave_pending:
            for i in range(8):
                if slave_pending & (1 << i):
                    if not (self.ims & (1 << self.cascade_irq)):
                        self.ims |= (1 << self.cascade_irq)
                        self.slave_ims |= (1 << i)
                        return i + 8
        masked = self.irr & ~self.mask
        if masked:
            for i in range(8):
                if masked & (1 << i):
                    if not (self.ims & (1 << i)):
                        self.ims |= (1 << i)
                        return i
        return -1

    def get_vector(self, irq):
        if irq < 8:
            return self.master_base + irq
        else:
            return self.slave_base + (irq - 8)

    def initialize(self):
        self.write_master(0x20, 0x11)
        self.write_master(0x20, self.master_base)
        self.write_master(0x20, 1 << self.cascade_irq)
        self.write_master(0x20, 0x01)
        self.write_master(0x21, self.mask)
        self.write_slave(0xA0, 0x11)
        self.write_slave(0xA0, self.slave_base)
        self.write_slave(0xA0, self.cascade_irq)
        self.write_slave(0xA0, 0x01)
        self.write_slave(0xA1, self.slave_mask)
        self.irr = 0
        self.slave_irr = 0
        self.ims = 0
        self.slave_ims = 0


# ═══════════════════════════════════════════════════════════════════════════
# CMOS RTC (MC146818)
# ═══════════════════════════════════════════════════════════════════════════

class CMOS:
    """MC146818 Real-Time Clock and NVRAM.

    Port 0x70: Address register
    Port 0x71: Data register
    Registers 0x00-0x07: Time/date (BCD)
    Register 0x0C: Century
    Registers 0x32+: BIOS data
    """

    def __init__(self):
        self._addr = 0
        self._data = bytearray(128)
        self._update_in_progress = False
        self._binary_mode = False
        self._sync_time()
        self._data[0x32] = 0x12
        self._data[0x33] = 0x56

    def _sync_time(self):
        now = datetime.datetime.now()
        s, m, h, day, month, year = (
            now.second, now.minute, now.hour,
            now.day, now.month, now.year
        )
        self._data[0x00] = self._to_bcd(s) if not self._binary_mode else s
        self._data[0x01] = self._to_bcd(m) if not self._binary_mode else m
        self._data[0x02] = self._to_bcd(h) if not self._binary_mode else h
        self._data[0x03] = self._to_bcd(day) if not self._binary_mode else day
        self._data[0x04] = self._to_bcd(month) if not self._binary_mode else month
        self._data[0x05] = self._to_bcd(year % 100) if not self._binary_mode else year % 100
        self._data[0x06] = self._to_bcd(now.isoweekday()) if not self._binary_mode else now.isoweekday()
        self._data[0x0C] = self._to_bcd(year // 100) if not self._binary_mode else year // 100

    def _to_bcd(self, val):
        return (val // 10) * 16 + (val % 10)

    def _from_bcd(self, val):
        return (val // 16) * 10 + (val % 16)

    def write_addr(self, val):
        self._addr = val & 0x7F

    def write_data(self, val):
        if self._addr < 128:
            self._data[self._addr] = val & 0xFF
            if self._addr == 0x0B:
                self._binary_mode = bool(val & 0x04)

    def read_data(self):
        if self._addr <= 0x06 or self._addr == 0x0C:
            self._sync_time()
        if self._addr == 0x0A:
            return (0x26 if not self._update_in_progress else 0xA6) | (0x04 if self._binary_mode else 0)
        if self._addr < 128:
            return self._data[self._addr]
        return 0

    def get_time(self):
        self._sync_time()
        conv = self._from_bcd if not self._binary_mode else lambda x: x
        century = conv(self._data[0x0C])
        year = century * 100 + conv(self._data[0x05])
        return (
            year, conv(self._data[0x04]), conv(self._data[0x03]),
            conv(self._data[0x02]), conv(self._data[0x01]), conv(self._data[0x00]),
        )

    def get_date_bcd(self):
        self._sync_time()
        return {
            'seconds': self._data[0x00], 'minutes': self._data[0x01],
            'hours': self._data[0x02], 'weekday': self._data[0x06],
            'day': self._data[0x03], 'month': self._data[0x04], 'year': self._data[0x05],
        }
