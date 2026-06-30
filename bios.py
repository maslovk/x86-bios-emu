"""
Simple BIOS Emulator - BIOS ROM
================================
Minimal BIOS ROM at 0xF0000-0xFFFFF with POST and key interrupt handlers.
"""

from video import Video
from hardware import PIT, PIC, CMOS
import sys


class BIOS:
    """Minimal BIOS ROM implementation."""

    def __init__(self, memory, video, keyboard, disk, pit=None, pic=None, cmos=None, kbd_ctrl=None):
        self.mem = memory
        self.video = video
        self.kbd = keyboard
        self.disk = disk
        self.pit = pit
        self.pic = pic
        self.cmos = cmos
        self.kbd_ctrl = kbd_ctrl
        self.handlers = {}
        self.ivt_stubs = {}

    def initialize(self):
        self._clear_ivt()
        self._setup_bda()
        self._register_handlers()
        self._install_ivt_stubs()
        self._setup_diskette_tables()
        self._setup_bootstrap()
        self._run_post()

    # ── IVT ─────────────────────────────────────────────────────

    def _clear_ivt(self):
        for i in range(1024):
            self.mem.write_byte(i, 0)

    def _register_handlers(self):
        self.handlers[0x00] = self._div_zero
        self.handlers[0x01] = self._debug
        self.handlers[0x02] = self._nmi
        self.handlers[0x03] = self._breakpoint
        self.handlers[0x04] = self._overflow
        self.handlers[0x08] = self._irq0_timer   # PIT IRQ 0
        self.handlers[0x09] = self._irq1_keyboard  # Keyboard IRQ 1
        self.handlers[0x0A] = self._irq_cascade    # Cascade (unused)
        self.handlers[0x0B] = self._irq_reserved
        self.handlers[0x0C] = self._irq_reserved
        self.handlers[0x0D] = self._irq_reserved
        self.handlers[0x0E] = self._irq_reserved
        self.handlers[0x0F] = self._irq_reserved
        self.handlers[0x10] = self._int10h
        self.handlers[0x11] = self._int11h
        self.handlers[0x12] = self._int12h
        self.handlers[0x13] = self._int13h
        self.handlers[0x14] = self._int14h
        self.handlers[0x15] = self._int15h
        self.handlers[0x16] = self._int16h
        self.handlers[0x17] = self._int17h
        self.handlers[0x19] = self._int19h
        self.handlers[0x1A] = self._int1ah
        self.handlers[0x1C] = self._int1ch        # Timer tick callback (usually empty)
        self.handlers[0x20] = self._int20h
        self.handlers[0x29] = self._int29h
        self.handlers[0x2A] = self._int2ah
        self.handlers[0x2B] = self._int2bh
        self.handlers[0x33] = self._int33h
        self.handlers[0x4F] = self._int4fh

    def _install_ivt_stubs(self):
        """Install IVT entries that point at tiny BIOS ROM stubs.

        The emulator still dispatches `INT n` to Python handlers directly, but
        DOS and boot loaders also inspect and chain IVT entries as data. Each
        stub is `INT n; IRET`, so a far caller that chains with the standard
        `PUSHF; CALL FAR [vec]` pattern gets a correct 6-byte IRET return
        (popping the pushed flags together with the call's CS:IP).  Using IRET
        here matches real BIOS interrupt handlers, which always return with
        IRET rather than RETF.
        """
        stub_seg = 0xF000
        stub_off = 0x0100
        for n in sorted(self.handlers):
            if n == 0x1E:
                continue
            base = (stub_seg << 4) + stub_off
            self.mem.write_byte(base + 0, 0xCD)   # INT imm8
            self.mem.write_byte(base + 1, n & 0xFF)
            self.mem.write_byte(base + 2, 0xCF)   # IRET (see comment above)
            self.mem.write_word(n * 4, stub_off)
            self.mem.write_word(n * 4 + 2, stub_seg)
            self.ivt_stubs[n] = (stub_seg, stub_off)
            stub_off += 4

    def handle_interrupt(self, cpu, n):
        handler = self.handlers.get(n)
        if handler:
            handler(cpu)
            return
        ip = self.mem.read_word(n * 4)
        cs = self.mem.read_word(n * 4 + 2)
        if ip != 0 or cs != 0:
            cpu.cs = cs
            cpu.ip = ip
            cpu.int_no_return = True

    # ── BIOS Data Area (0x00400) ────────────────────────────────

    def _setup_bda(self):
        bda = 0x00400
        # Populate the standard text-mode BDA fields DOS probes during boot.
        self.mem.write_word(bda + 0x04, 0x0000)  # Cursor pos page 0
        self.mem.write_word(bda + 0x06, 80)      # Columns
        self.mem.write_byte(bda + 0x08, 3)       # Video mode
        self.mem.write_word(bda + 0x0A, 0x0000)  # Cursor shape
        self.mem.write_word(bda + 0x0E, 0xB800)  # Video memory segment
        # Memory size at 0x10 and 0x13
        self.mem.write_word(bda + 0x10, 640)     # Conventional memory (KB)
        self.mem.write_byte(bda + 0x13, 640)     # Conventional memory (KB)
        # Cursor position at 0x50, 0x51
        self.mem.write_byte(bda + 0x50, 0)       # Cursor row
        self.mem.write_byte(bda + 0x51, 0)       # Cursor col
        # Warm boot flag
        self.mem.write_word(bda + 0x72, 0)       # Warm boot flag (0=cold)
        # Boot drive and error
        self.mem.write_byte(bda + 0x74, 0)       # Boot drive
        self.mem.write_byte(bda + 0x75, 0)       # Boot error
        # Sector size
        self.mem.write_word(bda + 0x7D, 512)     # Sector size

    def _setup_diskette_tables(self):
        # INT 1Eh points at the BIOS diskette parameter table. DOS boot sectors
        # copy and patch this table before issuing floppy reads.
        table_seg = 0xF000
        table_off = 0xEFC7
        table = [
            0xAF,  # step rate/unload time
            0x02,  # head load time / DMA mode
            0x25,  # motor-off delay
            0x02,  # bytes/sector code (512 bytes)
            0x09,  # sectors per track
            0x2A,  # gap length for read/write
            0xFF,  # data length (unused for 512-byte sectors)
            0x50,  # format gap length
            0xF6,  # format fill byte
            0x0F,  # head settle time
            0x08,  # motor start time
        ]
        base = (table_seg << 4) + table_off
        for i, value in enumerate(table):
            self.mem.write_byte(base + i, value)
        self.mem.write_word(0x1E * 4, table_off)
        self.mem.write_word(0x1E * 4 + 2, table_seg)

    # ── Bootstrap (0xFFFF0) ────────────────────────────────────

    def _setup_bootstrap(self):
        # JMP F000:E05B (jump to POST entry point)
        self.mem.write_byte(0xFFFF0, 0xEA)
        self.mem.write_word(0xFFFF1, 0xE05B)
        self.mem.write_word(0xFFFF3, 0xF000)
        for i in range(6, 16):
            self.mem.write_byte(0xFFFF0 + i, 0x90)

    # ── POST ───────────────────────────────────────────────────

    def _run_post(self):
        self.video.clear()
        self.video.print_str("Simple BIOS v1.0", Video.ATTR_GREEN, 5, 2)
        self.video.print_str("Copyright (C) 2025", Video.ATTR_NORMAL, 5, 3)
        self.video.print_str("CPU: x86 Real Mode Emulator", Video.ATTR_CYAN, 5, 5)
        self.video.print_str("Memory: 640K conventional", Video.ATTR_CYAN, 5, 6)
        self.video.print_str("Video: VGA 80x25 Color Text", Video.ATTR_CYAN, 5, 7)
        self.video.print_str("Keyboard: PS/2 Emulated", Video.ATTR_CYAN, 5, 8)
        self.video.print_str("Disk: 1.44MB Floppy", Video.ATTR_CYAN, 5, 9)
        self.video.print_str("POST: All tests passed.", Video.ATTR_GREEN, 5, 11)
        self.video.print_str("Booting from floppy...", Video.ATTR_YELLOW, 5, 13)

    # ── Exception Handlers ─────────────────────────────────────

    def _div_zero(self, cpu):
        self.video.print_str("\n*** DIVIDE BY ZERO ***", Video.ATTR_RED, 0, 22)
        cpu.halted = True

    def _debug(self, cpu):
        pass  # Single-step (handled by TF)

    def _nmi(self, cpu):
        self.video.print_str("\n*** NMI ***", Video.ATTR_RED, 0, 22)
        cpu.halted = True

    def _breakpoint(self, cpu):
        self.video.print_str("\n*** BREAKPOINT ***", Video.ATTR_RED, 0, 22)

    def _overflow(self, cpu):
        self.video.print_str("\n*** OVERFLOW ***", Video.ATTR_RED, 0, 22)

    # ── IRQ Handlers ─────────────────────────────────────────────

    def _irq0_timer(self, cpu):
        """IRQ 0: PIT timer interrupt (~18.2 Hz).
        Increments BDA timer tick counter and calls INT 1Ch handler."""
        # Increment BDA timer ticks at 0x046C
        if hasattr(self.mem, 'read_dword'):
            ticks = self.mem.read_dword(0x046C)
            ticks = (ticks + 1) & 0xFFFFFFFF
            self.mem.write_dword(0x046C, ticks)
        else:
            lo = self.mem.read_word(0x046C)
            hi = self.mem.read_word(0x046E)
            ticks = (lo | (hi << 16)) + 1
            ticks &= 0xFFFFFFFF
            self.mem.write_word(0x046C, ticks & 0xFFFF)
            self.mem.write_word(0x046E, (ticks >> 16) & 0xFFFF)

        # Call INT 1Ch (timer tick callback — usually empty in DOS)
        ip = self.mem.read_word(0x1C * 4)
        cs = self.mem.read_word(0x1C * 4 + 2)
        # Send EOI to PIC
        if self.pic:
            self.pic.send_eoi(0)
        # If DOS hooked INT 1Ch, transfer control directly to that handler and
        # let its IRET consume the original IRQ frame. Skip the built-in BIOS
        # stub here; it is only for far calls, not for hardware IRQ chaining.
        if (ip, cs) != (0, 0) and (cs, ip) != self.ivt_stubs.get(0x1C):
            cpu.cs = cs
            cpu.ip = ip
            cpu.int_no_return = True

    def _irq1_keyboard(self, cpu):
        """IRQ 1: Keyboard interrupt.

        Reads scan code from keyboard controller (port 0x60),
        translates to ASCII, stores in keyboard buffer for INT 16h.
        """
        # Read scan code / ASCII from keyboard controller
        if self.kbd_ctrl:
            sc = self.kbd_ctrl.read_data()
        else:
            sc = self.kbd.read_key()
        if sc:
            self.kbd.buffer.append(sc)

        # Send EOI to PIC
        if self.pic:
            self.pic.send_eoi(1)

    def _irq_cascade(self, cpu):
        """IRQ 2: Cascade to slave PIC."""
        if self.pic:
            self.pic.send_eoi(2)

    def _irq_reserved(self, cpu):
        """Reserved IRQ — send EOI and ignore."""
        pass

    # ── INT 15h: Miscellaneous Services ─────────────────────────

    # ── INT 14h: Serial Services ────────────────────────────────

    def _int14h(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF
        status = 0x60  # transmitter holding + shift registers empty
        modem = 0x00

        if ah == 0x00:  # Initialize port
            cpu.ah = status
        elif ah == 0x01:  # Transmit character
            cpu.ah = status
        elif ah == 0x02:  # Receive character
            cpu.al = 0x00
            cpu.ah = status
        elif ah == 0x03:  # Get port status
            cpu.ax = (status << 8) | modem
        else:
            cpu.ah = status

    # ── INT 15h: Miscellaneous Services ─────────────────────────

    def _int15h(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF
        if ah == 0x86:  # Wait for specified time
            cpu.ax = 0x0000
            cpu.flags &= ~0x01
        elif ah == 0x87:  # Move block
            # BL=0: ES:DI <- DS:SI, CX=word count
            # BL=1: DS:SI <- ES:DI, CX=word count
            bl = cpu.bx & 0xFF
            if bl == 0:
                src = (cpu.ds << 4) + cpu.si
                dst = (cpu.es << 4) + cpu.di
                for i in range(cpu.cx * 2):
                    self.mem.write_byte(dst + i, self.mem.read_byte(src + i))
                cpu.si = (cpu.si + cpu.cx * 2) & 0xFFFF
                cpu.di = (cpu.di + cpu.cx * 2) & 0xFFFF
                cpu.ax = 0x0000  # success
                cpu.flags &= ~0x01
            elif bl == 1:
                src = (cpu.es << 4) + cpu.di
                dst = (cpu.ds << 4) + cpu.si
                for i in range(cpu.cx * 2):
                    self.mem.write_byte(dst + i, self.mem.read_byte(src + i))
                cpu.si = (cpu.si + cpu.cx * 2) & 0xFFFF
                cpu.di = (cpu.di + cpu.cx * 2) & 0xFFFF
                cpu.ax = 0x0000  # success
                cpu.flags &= ~0x01
            else:
                cpu.ax = 0x8701  # function not supported
                cpu.flags |= 0x01
        elif ah == 0x88:  # Get extended memory size
            # Returns AX = KB above 1MB, CF=0 on success
            # We emulate 4MB total (640K conventional + 3840K extended)
            cpu.ax = 3840  # KB above 1MB (roughly)
            cpu.flags &= ~0x01
        elif ah == 0xCA:  # CRC-32 calculation
            # Simple software CRC-32 fallback
            # DX = 0: initialize CRC
            # DX = 1: process data at ES:BX, CX = byte count
            # Returns: DX:AX = CRC-32 result
            if cpu.dx == 0:
                cpu.ax = 0xFFFF
                cpu.dx = 0xFFFF
            else:
                # Process bytes and compute CRC-32
                es = cpu.es
                bx = cpu.bx
                cx = cpu.cx
                crc = (cpu.dx << 16) | cpu.ax
                for i in range(cx if cx else 0):
                    byte = self.mem.read_byte((es << 4) + bx + i)
                    crc ^= byte
                    for _ in range(8):
                        if crc & 1:
                            crc = (crc >> 1) ^ 0xEDB88320
                        else:
                            crc >>= 1
                crc &= 0xFFFFFFFF
                cpu.ax = crc & 0xFFFF
                cpu.dx = (crc >> 16) & 0xFFFF
            cpu.flags &= ~0x01
        else:
            cpu.ah = 0x86  # function not supported
            cpu.flags |= 0x01

    # ── INT 1Ch: Timer Tick Callback ─────────────────────────────

    def _int1ch(self, cpu):
        """INT 1Ch: Timer tick callback. Usually chained by DOS."""
        pass  # Empty by default; DOS/TSR chains here

    # ── INT 10h: Video ─────────────────────────────────────────

    def _int10h(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF
        al = cpu.ax & 0xFF
        bh = (cpu.bx >> 8) & 0xFF
        bl = cpu.bx & 0xFF
        cx = cpu.cx
        dx = cpu.dx

        if ah == 0x00:  # Set video mode
            self.video.mode = al
            self.video.clear()
            if al == 0x03:
                self.mem.write_byte(0x00408, 3)
                self.mem.write_word(0x00406, 80)
        elif ah == 0x02:  # Set cursor position
            row = dx & 0xFF
            col = (dx >> 8) & 0xFF
            self.video.cur_x = col
            self.video.cur_y = row
            self.mem.write_byte(0x00404, row)
            self.mem.write_byte(0x00405, col)
        elif ah == 0x03:  # Get cursor position
            cpu.ax = (self.video.cur_y << 8) | self.video.cur_x
            cpu.cx = 0x0607
        elif ah == 0x06:  # Scroll up
            rows = al if al else 25
            attr = bh
            for y in range(25):
                for x in range(80):
                    if y + rows < 25:
                        self.video.buffer[y][x] = self.video.buffer[y + rows][x]
                    else:
                        self.video.buffer[y][x] = (0x20, attr)
        elif ah == 0x08:  # Read char/attr at cursor
            x, y = self.video.cur_x, self.video.cur_y
            if 0 <= y < 25 and 0 <= x < 80:
                ch, attr = self.video.buffer[y][x]
                cpu.ax = (attr << 8) | ch
        elif ah == 0x09:  # Write char/attr at cursor
            for _ in range(cx if cx else 1):
                self.video.putc(al, bh)
        elif ah == 0x0C:  # Write char at row/col
            row = (dx >> 8) & 0xFF
            col = dx & 0xFF
            self.video.write(col, row, al, bh)
        elif ah == 0x0E:  # Teletype output
            self.video.putc(al, bh)
        elif ah == 0x0F:  # Get text mode
            cpu.ax = (self.video.width << 8) | self.video.mode
            cpu.bx = 0
            cpu.cx = self.video.height - 1
        elif ah == 0x13:  # Write string
            attr = bl if (bh & 0x80) else 0x07
            count = cx
            row = (dx >> 8) & 0xFF
            col = dx & 0xFF
            if row < 25 and col < 80:
                self.video.cur_x = col
                self.video.cur_y = row
            for i in range(count if count else 16):
                ch = self.mem.read_byte((cpu.es << 4) + cpu.bp + i)
                if ch == 0:
                    break
                if bh & 0x01:
                    self.video.putc(ch, attr)
                else:
                    self.video.write(self.video.cur_x, self.video.cur_y, ch, attr)
                    self.video.cur_x = (self.video.cur_x + 1) % 80

    # ── INT 11h: Equipment List ────────────────────────────────

    def _int11h(self, cpu):
        # Equipment list word:
        # Bits 15-14: unused
        # Bits 13-12: BASIC ROM (00 = none)
        # Bits 11-10: number of floppies - 1 (01 = 1 drive)
        # Bit 9: 1 = loaded from ROM
        # Bits 8-5: unused
        # Bit 4: 1 = color display (CGA/EGA/VGA)
        # Bits 3-1: unused
        # Bit 0: 1 = math coprocessor
        # Our config: 1 floppy, color VGA, no math, no BASIC
        # = (01 << 10) | (1 << 4) = 0x0400 | 0x0010 = 0x0410
        cpu.ax = 0x0410
        cpu.flags = (cpu.flags & ~0x40)

    # ── INT 12h: Memory Size ───────────────────────────────────

    def _int12h(self, cpu):
        cpu.ax = 640

    # ── INT 13h: Disk Services ─────────────────────────────────

    def _int13h(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF

        if ah == 0x00:  # Reset disk
            cpu.ax = 0
            cpu.flags &= ~0x01
        elif ah == 0x02:  # Read sectors (CHS)
            sectors = cpu.al
            sector = cpu.cl & 0x3F
            head = (cpu.dx >> 8) & 0xFF
            cyl = cpu.ch | ((cpu.cl & 0xC0) << 2)
            media = self.disk.media_type if hasattr(self.disk, 'media_type') else 0xF9
            spt = {0xF9: 18, 0xF8: 15, 0xF0: 15, 0xF1: 9, 0xFD: 9, 0xF2: 18}.get(media, 18)
            nheads = 2
            raw_lba = (cyl * nheads + head) * spt + (sector - 1)
            print(f"[BIOS] INT 13h AH=02 Raw: sector={sector}, head={head}, cyl={cyl}, count={sectors}, media=0x{media:02X}, spt={spt} -> Raw LBA={raw_lba} (IP={cpu.ip:04X}, CS={cpu.cs:04X})", file=sys.stderr)

            max_cyl = 79 # Default
            max_head = 1
            if hasattr(self.disk, 'media_type'):
                media_type = self.disk.media_type
                if media_type == 0xFD: # 360KB
                    max_cyl, max_head = 39, 1
                elif media_type == 0xF1: # 720KB
                    max_cyl, max_head = 39, 1
                elif media_type == 0xF9: # 1.44MB
                    max_cyl, max_head = 79, 1

            if (
                sectors == 0
                or sector == 0
                or sector > spt
                or head > max_head
                or cyl > max_cyl
            ):
                cpu.ax = 0x0400
                cpu.flags |= 0x01
                return

            lba = (cyl * nheads + head) * spt + (sector - 1)
            print(f"[BIOS] INT 13h AH=02 CHS OK: sector={sector}, head={head}, cyl={cyl} -> LBA={lba}", file=sys.stderr)
            es = cpu.es
            bx = cpu.bx
            ok = True
            for s in range(sectors):
                buf = bytearray(512)
                if not self.disk.read_sector(lba + s, buf):
                    ok = False
                    break
                # Write each sector immediately to ES:BX + s*512, wrapping offset modulo 64K
                for i in range(512):
                    self.mem.write_byte((es << 4) + ((bx + s * 512 + i) & 0xFFFF), buf[i])
            if not ok:
                cpu.ax = 0x0004
                cpu.flags |= 0x01
                return

            # Log the first 16 bytes of the buffer being read into
            try:
                buf_addr = (cpu.es << 4) + cpu.bx
                data = bytearray(self.mem.read_byte(buf_addr + i) for i in range(16))
                print(f"[BIOS] INT 13h AH=02 Read {sectors} sectors from LBA={lba} into {hex(buf_addr)}: {data.hex(' ')}", file=sys.stderr)
            except Exception as e:
                print(f"[BIOS] INT 13h AH=02 Buffer log error: {e}", file=sys.stderr)

            cpu.ah = 0x00
            cpu.al = sectors
            cpu.flags &= ~0x01
        elif ah == 0x03:  # Write sectors
            cpu.ah = 0x00
            cpu.flags &= ~0x01
        elif ah == 0x08:  # Get disk params
            media = self.disk.media_type if hasattr(self.disk, 'media_type') else 0xF9
            if media == 0xF9:  # 1.44MB
                max_cyl, max_head, spt = 79, 1, 18
            elif media == 0xF8:  # 1.2MB
                max_cyl, max_head, spt = 79, 1, 15
            elif media == 0xFD:  # 360KB
                max_cyl, max_head, spt = 39, 1, 9
            elif media == 0xF1:  # 720KB
                max_cyl, max_head, spt = 79, 1, 9
            else:
                max_cyl, max_head, spt = 79, 1, 18

            cpu.ah = 0x00
            cpu.al = 0x00
            cpu.bl = media
            cpu.ch = max_cyl & 0xFF
            cpu.cl = (spt & 0x3F) | ((max_cyl >> 2) & 0xC0)
            cpu.dh = max_head
            cpu.dl = 0x01
            cpu.di = self.mem.read_word(0x1E * 4)
            cpu.es = self.mem.read_word(0x1E * 4 + 2)
            cpu.flags &= ~0x01
        elif ah == 0x04:  # Re-calibrate drive
            cpu.ax = 0x0000
            cpu.flags &= ~0x01
        elif ah == 0x05:  # Check media changed
            cpu.al = 0x00  # Media not changed
            cpu.flags &= ~0x01
        elif ah == 0x06:  # Check drive status (Compaq)
            cpu.al = 0x00  # No error
            cpu.flags &= ~0x01
        elif ah == 0x07:  # Set disk parameters (ignore)
            cpu.flags &= ~0x01
        elif ah == 0x0D:  # Get disk type (IBM)
            media = self.disk.media_type if hasattr(self.disk, 'media_type') else 0xF9
            disk_type = {0xF0: 2, 0xF1: 3, 0xF8: 0, 0xF9: 3, 0xFD: 0, 0xF2: 3}.get(media, 3)
            cpu.al = disk_type
            cpu.flags &= ~0x01
        elif ah == 0x0E:  # Verify sectors (no-op)
            cpu.ax = 0x0001
            cpu.flags &= ~0x01
        elif ah == 0x0F:  # Format track (no-op)
            cpu.ax = 0x0000
            cpu.flags &= ~0x01
        elif ah == 0x41:  # Extended INT 13h check
            if cpu.bx == 0x55AA:
                cpu.ax = 0x0001
                cpu.bx = 0x0007  # Supported features bitmask
                cpu.flags &= ~0x01
            else:
                cpu.flags |= 0x01
        elif ah == 0x42:  # Extended read (LBA, DAP)
            # DL = drive, DS:SI = pointer to Disk Access Packet (16 bytes)
            # DAP: [0]size(4), [4]reserved(2), [6]count(2), [8]buf_seg(2), [A]buf_off(2), [C]lba(4)
            si = cpu.si
            ds = cpu.ds
            dap_base = (ds << 4) + si
            dap_size = self.mem.read_word(dap_base)
            if dap_size < 16:
                dap_size = 16
            count = self.mem.read_word(dap_base + 6)
            buf_seg = self.mem.read_word(dap_base + 8)
            buf_off = self.mem.read_word(dap_base + 10)
            lba = self.mem.read_dword(dap_base + 12)

            ok = True
            for s in range(count):
                buf = bytearray(512)
                if not self.disk.read_sector(lba + s, buf):
                    ok = False
                    break
                # Write each sector immediately to buf_seg:buf_off + s*512, wrapping offset modulo 64K
                for i in range(512):
                    self.mem.write_byte((buf_seg << 4) + ((buf_off + s * 512 + i) & 0xFFFF), buf[i])

            if ok:
                cpu.ax = 0x0001  # Success, sectors transferred
                cpu.flags &= ~0x01
            else:
                cpu.ax = 0x0004  # CRC/error
                cpu.flags |= 0x01
        elif ah == 0x43:  # Check drive type (extended disk services)
            # Returns drive geometry in DAP-like structure
            # DL = drive, DS:SI = pointer to parameter block
            # For simplicity, just return success with media type info
            media = self.disk.media_type if hasattr(self.disk, 'media_type') else 0xF9
            spt = {0xF9: 18, 0xF8: 15, 0xF0: 15, 0xF1: 9, 0xFD: 9, 0xF2: 9}.get(media, 18)
            cpu.ax = 0x0001  # Success
            cpu.flags &= ~0x01
        else:
            cpu.ax = 0
            cpu.flags &= ~0x01

    # ── INT 16h: Keyboard ──────────────────────────────────────

    def _int16h(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF
        # Set-1 scan codes for the printable ASCII range 0x20–0x7E, used to
        # back-fill AH (the scan-code byte required by INT 16h) when the
        # keyboard buffer only carries an ASCII value (the path used by the
        # interactive main loop's kbd_ctrl.inject_key).  Mapping scan codes
        # back to ASCII (the old approach) is wrong for this path because the
        # buffer already holds ASCII -- e.g. typing '1' (0x31 = ord('1')) would
        # have been misread as scan code 0x31 (the 'N' key) and returned 'n'.
        _ASCII_TO_SCAN = {
            0x1B: 0x01, 0x09: 0x0F, 0x0D: 0x1C, 0x08: 0x0E, 0x20: 0x39,
            0x27: 0x35, 0x2C: 0x33, 0x2D: 0x0C, 0x2E: 0x34, 0x2F: 0x35,
            0x30: 0x0B, 0x31: 0x02, 0x32: 0x03, 0x33: 0x04, 0x34: 0x05,
            0x35: 0x06, 0x36: 0x07, 0x37: 0x08, 0x38: 0x09, 0x39: 0x0A,
            0x3B: 0x27, 0x3D: 0x0D, 0x5B: 0x1A, 0x5C: 0x2B, 0x5D: 0x1B,
            0x60: 0x29, 0x61: 0x1E, 0x62: 0x30, 0x63: 0x2E, 0x64: 0x20,
            0x65: 0x12, 0x66: 0x21, 0x67: 0x22, 0x68: 0x23, 0x69: 0x17,
            0x6A: 0x24, 0x6B: 0x25, 0x6C: 0x26, 0x6D: 0x32, 0x6E: 0x31,
            0x6F: 0x18, 0x70: 0x19, 0x71: 0x10, 0x72: 0x13, 0x73: 0x1F,
            0x74: 0x14, 0x75: 0x16, 0x76: 0x2F, 0x77: 0x11, 0x78: 0x2D,
            0x79: 0x15, 0x7A: 0x2C,
        }
        if ah == 0x00:  # Wait for key (blocking)
            # Drain keyboard controller output into kbd buffer first. Keys
            # arrive here as ASCII bytes (kbd_ctrl.inject_key bypasses scan
            # translation), so the buffer holds ASCII, NOT scan codes.
            if self.kbd_ctrl and self.kbd_ctrl.has_data():
                while self.kbd_ctrl.has_data():
                    ch = self.kbd_ctrl.read_data()
                    if ch:
                        self.kbd.buffer.append(ch)
            if self.kbd.key_pressed():
                asc = self.kbd.read_key()
                if isinstance(asc, str):
                    asc = ord(asc)
                asc &= 0xFF
                sc = _ASCII_TO_SCAN.get(asc, 0)   # best-effort AH scan code
                cpu.ax = (sc << 8) | asc
                cpu.flags &= ~0x40
            else:
                # No key available — return scan code 0, ZF=0
                # DOS boot loops on this; auto-feed should prevent infinite loop
                cpu.ax = 0
                cpu.flags &= ~0x40
        elif ah == 0x01:  # Check key (peek; do NOT consume)
            # Drain the keyboard controller output buffer into the BIOS key
            # buffer first.  Without this, keys injected via kbd_ctrl (the
            # path used by the interactive main loop) are invisible to AH=01,
            # because the IRQ-1/INT-09h drain path is not guaranteed to have
            # fired.  DOS's idle loop polls AH=01, so this would deadlock.
            if self.kbd_ctrl and self.kbd_ctrl.has_data():
                while self.kbd_ctrl.has_data():
                    ch = self.kbd_ctrl.read_data()
                    if ch:
                        self.kbd.buffer.append(ch)
            if self.kbd.key_pressed():
                # AH=01 peeks (returns the key in AX but leaves it in the
                # buffer for AH=00 to consume).  Buffer holds ASCII; put it
                # in AL and best-effort scan code in AH.
                key = self.kbd.buffer[0] & 0xFF
                sc = _ASCII_TO_SCAN.get(key, 0)
                cpu.ax = (sc << 8) | key
                cpu.flags &= ~0x40         # ZF=0: key available
            else:
                cpu.ax = 0
                cpu.flags |= 0x40         # ZF=1: no key
        elif ah == 0x02:  # Shift state
            if self.kbd_ctrl:
                cpu.ax = self.kbd_ctrl.shift_state
            else:
                cpu.ax = 0

    # ── INT 17h: Printer Services ───────────────────────────────

    def _int17h(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF
        status = 0x90  # selected + not busy

        if ah == 0x00:  # Print character
            cpu.ah = status
        elif ah == 0x01:  # Initialize printer
            cpu.ah = status
        elif ah == 0x02:  # Get printer status
            cpu.ah = status
        else:
            cpu.ah = status

    # ── INT 19h: Boot Loader ───────────────────────────────────

    def _int19h(self, cpu):
        buf = bytearray(512)
        if not self.disk.read_sector(0, buf):
            self.video.print_str(" Boot failure!", Video.ATTR_RED, 20, 13)
            cpu.halted = True
            return
        for i in range(512):
            self.mem.write_byte(0x7C00 + i, buf[i])
        for i in range(16):
            print(f"BootSector[{i}] = {self.mem.read_byte(0x7C00 + i):02X}")
        sig = self.mem.read_word(0x7DFE)
        if sig != 0xAA55:
            self.video.print_str(" No bootable OS!", Video.ATTR_RED, 20, 13)
            cpu.halted = True
            return
        cpu.cs = 0x0000
        cpu.ip = 0x7C00
        cpu.ss = 0x0000
        cpu.sp = 0x7C00
        cpu.ds = 0x0000
        cpu.es = 0x0000
        cpu.int_no_return = True  # Don't pop return address — boot takes over
        self.video.print_str(" OK", Video.ATTR_GREEN, 36, 13)

    # ── INT 20h: Terminate ─────────────────────────────────────

    def _int20h(self, cpu):
        self.video.print_str("\nWarm reboot.", Video.ATTR_YELLOW, 0, 23)
        cpu.halted = True

    # ── INT 29h: Direct Console Output ─────────────────────────

    def _int29h(self, cpu):
        self.video.putc(cpu.al)

    # ── INT 1Ah: System Time ───────────────────────────────────

    def _int1ah(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF
        if ah == 0x00:  # Get system ticks
            # INT 1Ah AH=00 returns CX:DX = 32-bit tick count where
            # CX = high word and DX = low word (per IBM PC BIOS spec).
            # Returning them swapped causes DOS's elapsed-time checks
            # (SBB CX, saved_high) to see a non-zero high word and falsely
            # conclude a timeout, aborting device opens.
            ticks = self.mem.read_dword(0x046C) if hasattr(self.mem, 'read_dword') else (
                self.mem.read_word(0x046C) | (self.mem.read_word(0x046E) << 16)
            )
            cpu.cx = (ticks >> 16) & 0xFFFF   # high word
            cpu.dx = ticks & 0xFFFF            # low word
        elif ah == 0x02:  # Get RTC time
            if self.cmos:
                t = self.cmos.get_date_bcd()
                cpu.cx = (t['hours'] << 8) | t['minutes']
                cpu.dx = (t['seconds'] << 8) | 0x80  # Bit 7 = RTC available
                cpu.flags &= ~0x01  # CF=0 (success)
            else:
                cpu.dx = 0x0000
                cpu.flags |= 0x01  # CF=1 (failure)
        elif ah == 0x04:  # Get RTC date
            if self.cmos:
                t = self.cmos.get_date_bcd()
                cpu.cx = (t['weekday'] << 8) | t['day']
                cpu.dx = (t['month'] << 8) | t['year']
                cpu.flags &= ~0x01
            else:
                cpu.flags |= 0x01

    # ── INT 2Ah: (compat) Get System Time ──────────────────────

    def _int2ah(self, cpu):
        # Only AH=00h is "Get System Time" — and it is the only function
        # we should answer.  DOS uses INT 2Ah for many internal signals
        # (AH=80h–87h critical-section, AH=06h print-server, etc.) that
        # must NOT clobber any registers.  Returning here without touching
        # registers lets those internal calls pass through harmlessly.
        if (cpu.ax >> 8) & 0xFF:
            return
        import time
        t = int(time.time())
        cpu.cx = (t // 1000) & 0xFFFF  # Hundredths
        cpu.dx = 0  # Days since 1980

    # ── INT 2Bh: Get System Date ───────────────────────────────

    def _int2bh(self, cpu):
        import datetime
        now = datetime.datetime.now()
        cpu.ax = now.year
        cpu.cx = now.month
        cpu.dx = now.day

    # ── INT 33h: Mouse ─────────────────────────────────────────

    def _int33h(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF
        if ah == 0x00:
            cpu.ax = 0x0001  # Mouse present
        elif ah == 0x03:
            cpu.ax = 0x0001
            cpu.cx = 0
            cpu.dx = 0
            cpu.bx = 0
        else:
            cpu.ax = 0x0001

    # ── INT 4Fh: VBE ──────────────────────────────────────────

    def _int4fh(self, cpu):
        ah = (cpu.ax >> 8) & 0xFF
        if ah == 0x00 and cpu.bx == 0x4F00:
            # Return VBE info block
            es = cpu.es
            di = cpu.di
            addr = (es << 4) + di
            # "VESA" signature
            for i, c in enumerate(b'VESA'):
                self.mem.write_byte(addr + i, c)
            self.mem.write_word(addr + 4, 0x0200)  # VBE version 2.0
            self.mem.write_word(addr + 0x1E, 0x0041)  # OEM vendor len
            self.mem.write_word(addr + 0x34, 0x004F)  # AX value for success
            cpu.ax = 0x004F
        elif ah == 0x01 and cpu.bx == 0x4F01:
            # Get mode info
            cpu.ax = 0x004F
        else:
            cpu.ax = 0x0000
