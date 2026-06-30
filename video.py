"""
Simple BIOS Emulator - Video and I/O
=====================================
VGA text-mode video (80x25) and I/O port emulation.
"""

import os
import sys


class Video:
    """VGA text mode 0xB8000, 80x25, 16 colors."""

    ATTR_NORMAL = 0x07
    ATTR_WHITE  = 0x0F
    ATTR_GREEN  = 0x09
    ATTR_CYAN   = 0x0A
    ATTR_RED    = 0x0C
    ATTR_YELLOW = 0x0E

    def __init__(self):
        self.width = 80
        self.height = 25
        self.buffer = [[(0, self.ATTR_NORMAL) for _ in range(self.width)]
                       for _ in range(self.height)]
        self.cur_x = 0
        self.cur_y = 0
        self.mode = 3
        self.mem = None
        self.text_base = 0xB8000

    def attach_memory(self, mem):
        self.mem = mem
        self._sync_to_memory()

    def _cell_addr(self, x, y):
        return self.text_base + ((y * self.width + x) * 2)

    def _sync_to_memory(self):
        if self.mem is None:
            return
        for y, row in enumerate(self.buffer):
            for x, (ch, attr) in enumerate(row):
                addr = self._cell_addr(x, y)
                self.mem.write_byte(addr, ch)
                self.mem.write_byte(addr + 1, attr)

    def _sync_from_memory(self):
        if self.mem is None:
            return
        for y in range(self.height):
            for x in range(self.width):
                addr = self._cell_addr(x, y)
                ch = self.mem.read_byte(addr)
                attr = self.mem.read_byte(addr + 1)
                self.buffer[y][x] = (ch, attr)

    def write(self, x, y, ch, attr=ATTR_NORMAL):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.buffer[y][x] = (ch, attr)
            if self.mem is not None:
                addr = self._cell_addr(x, y)
                self.mem.write_byte(addr, ch)
                self.mem.write_byte(addr + 1, attr)

    def putc(self, ch, attr=ATTR_NORMAL):
        if ch == 0x0A:
            self.cur_x = 0; self.cur_y += 1
        elif ch == 0x0D:
            self.cur_x = 0
        elif ch == 0x08:
            self.cur_x = max(0, self.cur_x - 1)
        elif 0x20 <= ch <= 0x7E:
            self.write(self.cur_x, self.cur_y, ch, attr)
            self.cur_x += 1
        elif ch == 0:
            return
        if self.cur_x >= self.width:
            self.cur_x = 0; self.cur_y += 1
        if self.cur_y >= self.height:
            self.scroll()

    def scroll(self):
        self.buffer = self.buffer[1:]
        self.buffer.append([(0x20, self.ATTR_NORMAL) for _ in range(self.width)])
        self.cur_y = self.height - 1

    def print_str(self, s, attr=ATTR_NORMAL, x=-1, y=-1):
        if x >= 0 and y >= 0:
            self.cur_x = x; self.cur_y = y
        for ch in s:
            self.putc(ord(ch), attr)

    def clear(self, attr=ATTR_NORMAL):
        self.buffer = [[(0x20, attr) for _ in range(self.width)]
                       for _ in range(self.height)]
        self.cur_x = 0; self.cur_y = 0
        self._sync_to_memory()

    # ANSI foreground escapes for the 16 CGA colours (low nibble of attr).
    _FG = {
        0: "30",  1: "34",  2: "32",  3: "36",
        4: "31",  5: "35",  6: "33",  7: "37",
        8: "90",  9: "94", 10: "92", 11: "96",
       12: "91", 13: "95", 14: "93", 15: "97",
    }

    def _render_row(self, row, use_color):
        """Render one VGA row (80 cells) as a string.

        Consecutive cells sharing the same foreground colour are batched
        into a single ANSI escape, so output stays readable when redirected
        and is fast to emit on a terminal."""
        out = []
        cur_fg = None
        for ch, attr in row:
            fg = attr & 0xF
            c = chr(ch) if 0x20 <= ch <= 0x7E else ' '
            if use_color and fg != cur_fg:
                out.append(f"\033[{self._FG.get(fg, 37)}m")
                cur_fg = fg
            out.append(c)
        if use_color:
            out.append("\033[0m")
        return ''.join(out)

    def display(self):
        self._sync_from_memory()
        use_color = sys.stdout.isatty()
        # Clear screen + home cursor. Only meaningful on a real terminal;
        # skip when redirected so piped output has no stray escape codes.
        if use_color:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        pad = 2                              # spaces of padding each side
        inner = self.width + pad * 2        # content width between the bars
        top = "╔" + "═" * inner + "╗"
        div = "╠" + "═" * inner + "╣"
        bot = "╚" + "═" * inner + "╝"
        title = "Simple BIOS Emulator — VGA Text Mode (80x25)"
        gap = inner - len(title)
        title_row = "║" + (" " * (gap // 2)) + title + (" " * (gap - gap // 2)) + "║"
        print(top)
        print(title_row)
        print(div)
        for row in self.buffer:
            body = self._render_row(row, use_color)
            print(f"║  {body}  ║")
        print(bot)


class Serial:
    """COM1 serial port (0x3F8-0x3FF). Outputs to stderr."""

    def __init__(self):
        self.data = 0
        self.llsr_thre = 0x20  # THR empty = ready
        self.line_ctrl = 0x03  # 8N1 default
        self.msr = 0xC0        | 0x20  # DCD+CTS active
        self.iir = 0x01        # No interrupt pending
        self.lsr = 0x60        # THRE+DATA_READY
        self.baud = 9600
        self.output = []

    def inb(self, offset):
        if offset == 0x00:     # RBR (receive buffer)
            return self.data
        if offset == 0x04:     # LSR (line status)
            return self.lsr
        if offset == 0x05:     # MSR (modem status)
            return self.msr
        if offset == 0x02:     # IIR (interrupt id)
            return self.iir
        return 0x00

    def outb(self, offset, val):
        if offset == 0x00:     # THR (transmit holding)
            if val >= 0x20:
                sys.stderr.write(f"[COM1] {chr(val)}")
            elif val == 0x0A:
                sys.stderr.write('\n')
            elif val == 0x0D:
                sys.stderr.write('\r')
            sys.stderr.flush()
            self.lsr |= 0x20  # THRE set
        elif offset == 0x04:   # LCR (line control)
            self.line_ctrl = val
        elif offset == 0x08:   # MCR (modem control)
            pass


class IO:
    """I/O port emulation (keyboard, PIT, PIC, CMOS, serial, etc.)."""

    def __init__(self, video, keyboard, disk=None, serial=None,
                 pit=None, pic=None, cmos=None, kbd_ctrl=None):
        self.video = video
        self.kbd = keyboard
        self.disk = disk
        self.serial = serial
        self.pit = pit
        self.pic = pic
        self.cmos = cmos
        self.kbd_ctrl = kbd_ctrl  # Keyboard controller (8042)
        self._pit_pending_irqs = []  # IRQs fired since last check

    def inb(self, port):
        if port == 0x60:  # Keyboard data port
            if self.kbd_ctrl:
                return self.kbd_ctrl.read_data()
            return self.kbd.read_key()
        if port == 0x61:  # PIT control / speaker
            return 0x00
        if port == 0x64:  # Keyboard controller status
            if self.kbd_ctrl:
                return self.kbd_ctrl.read_status()
            return 0x00 if self.kbd.key_pressed() else 0x01
        if port == 0x80:  # Diagnostic port
            return 0x00
        if port == 0x92:  # Soft config
            return 0x00

        # PIT counters (0x40-0x42)
        if self.pit and 0x40 <= port <= 0x42:
            return self.pit.read_counter(port - 0x40)

        # PIC master data (0x20) — read ISR/IRR
        if port == 0x20 and self.pic:
            return self.pic.ims | self.pic.irr
        # PIC slave data (0xA0)
        if port == 0xA0 and self.pic:
            return self.pic.slave_ims | self.pic.slave_irr

        # CMOS address (0x70) — read returns last address
        if port == 0x70 and self.cmos:
            return self.cmos._addr
        # CMOS data (0x71)
        if port == 0x71 and self.cmos:
            return self.cmos.read_data()

        if self.serial and 0x3F8 <= port <= 0x3FF:
            return self.serial.inb(port - 0x3F8)
        return 0x00

    def inw(self, port):
        lo = self.inb(port)
        hi = self.inb(port + 1)
        return lo | (hi << 8)

    def outb(self, port, val):
        if port == 0x60:  # Keyboard data port
            if self.kbd_ctrl:
                self.kbd_ctrl.write_data(val)
            return
        if port == 0x64:  # Keyboard controller command port
            if self.kbd_ctrl:
                self.kbd_ctrl.write_command(val)
            return
        if port == 0x80:  # Diagnostic port
            pass

        # PIT counters (0x40-0x42)
        if self.pit and 0x40 <= port <= 0x42:
            self.pit.write_counter(port - 0x40, val)
            return
        # PIT command (0x43)
        if self.pit and port == 0x43:
            self.pit.write_command(val)
            return

        # PIC master (0x20-0x21)
        if self.pic and 0x20 <= port <= 0x21:
            self.pic.write_master(port, val)
            return
        # PIC slave (0xA0-0xA1)
        if self.pic and 0xA0 <= port <= 0xA1:
            self.pic.write_slave(port, val)
            return

        # CMOS address (0x70)
        if self.cmos and port == 0x70:
            self.cmos.write_addr(val)
            return
        # CMOS data (0x71)
        if self.cmos and port == 0x71:
            self.cmos.write_data(val)
            return

        if self.serial and 0x3F8 <= port <= 0x3FF:
            self.serial.outb(port - 0x3F8, val)

    def outw(self, port, val):
        self.outb(port, val & 0xFF)
        self.outb(port + 1, (val >> 8) & 0xFF)

    def tick(self, dt=1/18.2):
        """Advance PIT by dt seconds. Returns list of fired IRQs."""
        if self.pit:
            fired = self.pit.tick(dt)
            routed = []
            for channel in fired:
                if channel != 0:
                    continue
                routed.append(channel)
                self._pit_pending_irqs.append(channel)
                if self.pic:
                    self.pic.raise_irq(channel)
            return routed
        return []

    def get_pending_irq(self):
        """Get highest priority pending IRQ, or -1 if none."""
        if self.pic:
            return self.pic.get_highest_irq()
        return -1

    def get_irq_vector(self, irq):
        """Get interrupt vector for given IRQ."""
        if self.pic:
            return self.pic.get_vector(irq)
        return irq + 8


class Keyboard:
    """Simple keyboard buffer."""

    def __init__(self):
        self.buffer = []

    def key_pressed(self):
        return len(self.buffer) > 0

    def read_key(self):
        return self.buffer.pop(0) if self.buffer else 0

    def feed_string(self, s):
        for ch in s:
            self.buffer.append(ord(ch))
        self.buffer.append(0x0D)


class Disk:
    """Simple disk image (array of 512-byte sectors)."""

    def __init__(self):
        self.sectors = [bytearray(512) for _ in range(2880)]  # 1.44MB floppy
        self.media_type = 0xF9  # Default: 1.44MB 3.5" floppy

    def read_sector(self, lba, buf):
        if not 0 <= lba < len(self.sectors):
            return False
        for i in range(512):
            buf[i] = self.sectors[lba][i]
        return True

    def write_sector(self, lba, buf):
        if not 0 <= lba < len(self.sectors):
            return False
        for i in range(512):
            self.sectors[lba][i] = buf[i]
        return True

    def write_boot_sector(self, code):
        """Write boot sector code (bytes) to LBA 0."""
        self.sectors[0][:len(code)] = code
        # Set boot signature
        self.sectors[0][510] = 0x55
        self.sectors[0][511] = 0xAA
