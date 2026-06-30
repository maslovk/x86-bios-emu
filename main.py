"""
Simple BIOS Emulator - Main
============================
Ties together CPU, Memory, Video, BIOS, and Disk.
Includes a sample boot sector that prints "Hello from boot sector!"
"""

import sys
import time
import os
import argparse

from cpu import CPU
from video import Video, IO, Keyboard, Disk, Serial
from bios import BIOS
from hardware import PIT, PIC, CMOS, KeyboardController
from fat12 import FAT12, FAT12Error
import video as video_mod


# ─── Sample Boot Sector (512 bytes) ────────────────────────────────────────
#
# This is a minimal x86 real-mode boot sector written in "assembly" as bytes.
# It prints "Hello from boot sector!" using INT 10h and then halts.
#
# Assembly equivalent:
#
#   [org 0x7C00]
#   cli                    ; Disable interrupts
#   xor ax, ax             ; AX = 0
#   mov ss, ax             ; SS = 0
#   mov sp, 0x7C00         ; Stack below boot sector
#   mov ds, ax             ; DS = 0
#   mov es, ax             ; ES = 0
#
#   ; Set video mode 3 (80x25 color)
#   mov ax, 0x0003
#   int 0x10
#
#   ; Print "Hello from boot sector!"
#   mov ax, 0x1301         ; AH=13, AL=1 (write, update cursor)
#   mov bx, 0x0007         ; Page 0, attribute 7 (light gray)
#   mov cx, 25             ; String length
#   mov dx, 0x0000         ; Row 0, Col 0
#   mov bp, msg            ; ES:BP -> message (ES=0, so absolute addr)
#   int 0x10
#
#   ; Print "Press any key to continue..."
#   mov ax, 0x1301
#   mov bx, 0x000E         ; Yellow
#   mov cx, 30
#   mov dx, 0x0100         ; Row 1, Col 0
#   mov bp, msg2
#   int 0x10
#
#   ; Wait for key
#   xor ax, ax
#   int 0x16
#
#   ; Print "Key pressed: " + hex value
#   mov ax, 0x1301
#   mov bx, 0x000A         ; Cyan
#   mov cx, 14
#   mov dx, 0x0200
#   mov bp, msg3
#   int 0x10
#
#   ; Halt
#   hlt
#   jmp $
#
# msg  db "Hello from boot sector!", 0
# msg2 db "Press any key to continue...", 0
# msg3 db "Key pressed: ", 0
#
#   times 510-($-$$) db 0
#   dw 0xAA55

def build_boot_sector():
    """Build a sample boot sector in raw bytes."""
    code = bytearray()

    def write_byte(b):
        code.append(b & 0xFF)

    def write_word(w):
        code.append(w & 0xFF)
        code.append((w >> 8) & 0xFF)

    # --- CODE SECTION ---

    # cli
    write_byte(0xFA)

    # xor ax, ax
    write_byte(0x31); write_byte(0xC0)

    # mov ss, ax
    write_byte(0x8E); write_byte(0xD0)

    # mov sp, 0x7C00
    write_byte(0xBC); write_word(0x7C00)

    # mov ds, ax (ds = 0)
    write_byte(0x8E); write_byte(0xD8)

    # mov ax, 0x07C0
    write_byte(0xB8); write_word(0x07C0)
    # mov es, ax (es = 0x07C0 for string addresses in boot sector)
    write_byte(0x8E); write_byte(0xC0)

    # Set video mode 3
    write_byte(0xB8); write_word(0x0003)  # mov ax, 0x0003
    write_byte(0xCD); write_byte(0x10)     # int 0x10

    # Print msg1: "Hello from boot sector!"
    msg1 = b"Hello from boot sector!"
    write_byte(0xB8); write_word(0x1301)  # mov ax, 0x1301
    write_byte(0xBB); write_word(0x0007)  # mov bx, 0x0007 (white)
    write_byte(0xB9); write_word(len(msg1))  # mov cx, len
    write_byte(0xBA); write_word(0x0000)  # mov dx, 0x0000 (row 0, col 0)
    # jmp over strings (will patch address later)
    jmp1_pos = len(code)
    write_byte(0xEB); write_byte(0x00)  # jmp short (placeholder)

    # --- STRINGS SECTION ---
    msg1_offset = len(code)  # offset within boot sector for BP (ES:BP = 0x7C00+offset)
    code.extend(msg1)
    code.append(0)

    msg2 = b"Press any key..."
    msg2_offset = len(code)
    code.extend(msg2)
    code.append(0)

    msg3 = b"Key: "
    msg3_offset = len(code)
    code.extend(msg3)
    code.append(0)

    msg4 = b" OK!"
    msg4_offset = len(code)
    code.extend(msg4)
    code.append(0)

    key_buf_offset = len(code)
    code.append(0)  # buffer for key char

    # --- CODE CONTINUES ---
    jmp1_target = len(code)  # absolute offset within boot sector
    # Patch jmp1: compute relative offset from byte after JMP
    jmp1_rel = jmp1_target - (jmp1_pos + 2)
    code[jmp1_pos + 1] = jmp1_rel & 0xFF

    # Print msg2 at row 1
    write_byte(0xB8); write_word(0x1301)  # mov ax, 0x1301
    write_byte(0xBB); write_word(0x000E)  # mov bx, 0x000E (yellow)
    write_byte(0xB9); write_word(len(msg2))  # mov cx, len
    write_byte(0xBA); write_word(0x0100)  # mov dx, 0x0100 (row 1)
    write_byte(0xBD); write_word(msg2_offset)  # mov bp, msg2_offset
    write_byte(0xCD); write_byte(0x10)     # int 0x10

    # Wait for key: xor ax, ax; int 0x16
    write_byte(0x31); write_byte(0xC0)  # xor ax, ax
    write_byte(0xCD); write_byte(0x16)     # int 0x16

    # Print msg3 at row 2
    write_byte(0xB8); write_word(0x1301)  # mov ax, 0x1301
    write_byte(0xBB); write_word(0x000A)  # mov bx, 0x000A (cyan)
    write_byte(0xB9); write_word(len(msg3))  # mov cx, len
    write_byte(0xBA); write_word(0x0200)  # mov dx, 0x0200 (row 2)
    write_byte(0xBD); write_word(msg3_offset)  # mov bp, msg3_offset
    write_byte(0xCD); write_byte(0x10)     # int 0x10

    # Print key char at row 2, col 5
    write_byte(0xB8); write_word(0x1301)  # mov ax, 0x1301
    write_byte(0xBB); write_word(0x000F)  # mov bx, 0x000F (white)
    write_byte(0xB9); write_word(1)       # mov cx, 1
    write_byte(0xBA); write_word(0x0205)  # mov dx, 0x0205 (row 2, col 5)
    # Store AL (key) to buffer
    write_byte(0xA2); write_word(key_buf_offset)  # mov [buf], al
    write_byte(0xBD); write_word(key_buf_offset)  # mov bp, buf_offset
    write_byte(0xCD); write_byte(0x10)     # int 0x10

    # Print msg4 at row 3
    write_byte(0xB8); write_word(0x1301)  # mov ax, 0x1301
    write_byte(0xBB); write_word(0x0009)  # mov bx, 0x0009 (green)
    write_byte(0xB9); write_word(len(msg4))  # mov cx, len
    write_byte(0xBA); write_word(0x0300)  # mov dx, 0x0300 (row 3)
    write_byte(0xBD); write_word(msg4_offset)  # mov bp, msg4_offset
    write_byte(0xCD); write_byte(0x10)     # int 0x10

    # Print msg1 at row 0 (was skipped by jmp, now print it)
    write_byte(0xB8); write_word(0x1301)  # mov ax, 0x1301
    write_byte(0xBB); write_word(0x0007)  # mov bx, 0x0007
    write_byte(0xB9); write_word(len(msg1))  # mov cx, len
    write_byte(0xBA); write_word(0x0000)  # mov dx, 0x0000
    write_byte(0xBD); write_word(msg1_offset)  # mov bp, msg1_offset
    write_byte(0xCD); write_byte(0x10)     # int 0x10

    # hlt; jmp $
    write_byte(0xF4)  # HLT
    write_byte(0xEB); write_byte(0xFE)  # JMP $ (infinite loop)

    # Pad to 510 bytes
    while len(code) < 510:
        code.append(0)

    # Boot signature
    code.append(0x55)
    code.append(0xAA)

    assert len(code) == 512
    return bytes(code)


# ─── Emulator ──────────────────────────────────────────────────────────────

class Emulator:
    """Main emulator loop."""

    def __init__(self, boot_file=None, step_mode=False, interactive=False,
                 enable_hardware=True, floppy_image=None, gtk=False,
                 gtk_font_size=18):
        self.memory = type('Memory', (), {})()
        # Use the Memory class from cpu module
        from cpu import CPU as _CPU
        # We need a proper Memory class
        self.mem = self._create_memory()
        self.video = Video()
        self.video.attach_memory(self.mem)
        self.kbd = Keyboard()
        self.disk = Disk()
        self.serial = Serial()

        # Hardware devices
        self.pit = PIT() if enable_hardware else None
        self.pic = PIC() if enable_hardware else None
        self.cmos = CMOS() if enable_hardware else None
        self.kbd_ctrl = KeyboardController() if enable_hardware else None

        self.io = IO(self.video, self.kbd, self.disk, self.serial,
                     pit=self.pit, pic=self.pic, cmos=self.cmos,
                     kbd_ctrl=self.kbd_ctrl)
        self.cpu = _CPU(self.mem, self.io)
        self.cpu.step_mode = step_mode
        self.bios = BIOS(self.mem, self.video, self.kbd, self.disk,
                         pit=self.pit, pic=self.pic, cmos=self.cmos,
                         kbd_ctrl=self.kbd_ctrl)
        self.boot_file = boot_file
        self.interactive = interactive or gtk   # --gtk implies interactive
        self.enable_hardware = enable_hardware

        # GTK display (optional).  When enabled, it takes over rendering and
        # keyboard input: the emulator loop pumps Gtk events between
        # instruction batches, and key-press callbacks inject ASCII bytes
        # directly into the keyboard controller (no cbreak/scan-code dance).
        self.gtk = gtk
        self.gtk_display = None
        if gtk:
            from gtdisplay import GtkDisplay
            def _on_key(byte):
                if self.kbd_ctrl:
                    self.kbd_ctrl.inject_key(byte)
                else:
                    self.kbd.buffer.append(byte)
            self.gtk_display = GtkDisplay(
                self.video, on_key=_on_key, font_size=gtk_font_size)

        # FAT12 filesystem
        self.floppy_image = floppy_image
        self.fat = None
        if floppy_image:
            self._load_floppy(floppy_image)

        # Write BIOS ROM string
        bios_str = b"SIMPLE BIOS"
        for i, b in enumerate(bios_str):
            self.mem.write_byte(0xF0000 + i, b)

    def _create_memory(self):
        """Create memory object compatible with CPU."""
        class Mem:
            def __init__(self):
                self.ram = bytearray(0x100000)
            def read_byte(self, a):
                return self.ram[a & 0xFFFFF]
            def read_word(self, a):
                a &= 0xFFFFF
                return self.ram[a] | (self.ram[a + 1] << 8)
            def read_dword(self, a):
                a &= 0xFFFFF
                return (self.ram[a] | (self.ram[a + 1] << 8) |
                        (self.ram[a + 2] << 16) | (self.ram[a + 3] << 24))
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
        return Mem()

    def _setup_ivt_irq_handlers(self):
        """Set up IVT entries for IRQ handlers."""
        # INT 1Ch (timer tick callback) → empty by default
        int1c_addr = 0x1C * 4
        self.mem.write_word(int1c_addr, 0x0000)
        self.mem.write_word(int1c_addr + 2, 0x0000)

    def _load_floppy(self, path: str):
        """Load a floppy image file and mount FAT12."""
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except FileNotFoundError:
            print(f"[ERROR] Floppy image not found: {path}", file=sys.stderr)
            sys.exit(1)

        # Detect image size and media type
        actual_sectors = len(data) // 512
        media_byte = data[0x15] if len(data) > 0x15 else 0xF9
        media_names = {0xF8: '360KB (5.25")', 0xF0: '1.2MB (5.25")',
                       0xF9: '1.44MB (3.5")', 0xF1: '720KB (3.5")', 0xF2: '2.88MB (3.5")'}
        media_name = media_names.get(media_byte, f'unknown (0x{media_byte:02X})')
        print(f"  Floppy: {len(data)//1024}KB, {actual_sectors} sectors, media=0x{media_byte:02X} ({media_name})",
              file=sys.stderr)

        # Pad to 1.44 MB (2880 sectors)
        if len(data) < 1474560:
            data = data + b'\x00' * (1474560 - len(data))

        # Write to disk sectors
        for i in range(2880):
            buf = bytearray(512)
            buf[:min(512, len(data) - i * 512)] = data[i * 512:(i + 1) * 512]
            self.disk.write_sector(i, buf)

        # Store media type for BIOS to use in INT 13h AH=08
        self.disk.media_type = media_byte

        # Mount FAT12
        try:
            self.fat = FAT12(self.disk)
            self.fat.mount()
            info = self.fat.info()
            print(f"  FAT12: {info['capacity_kb']}KB, {info['cluster_size']}B/cluster, "
                  f"{info['total_clusters']} clusters", file=sys.stderr)
        except FAT12Error as e:
            print(f"[WARN] FAT12 mount failed: {e}", file=sys.stderr)
            self.fat = None

    def _check_and_dispatch_irq(self):
        """Check for pending IRQs and dispatch highest priority one.
        Returns True if an IRQ was dispatched."""
        if not self.cpu.if_flag:
            return False
        if self.cpu._irq_shadow:
            return False
        irq = self.io.get_pending_irq()
        if irq < 0:
            return False
        vector = self.io.get_irq_vector(irq)
        # Any delivered interrupt resumes a CPU halted by HLT.
        self.cpu.halted = False
        # Push FLAGS, CS, IP and jump to handler
        saved_flags = self.cpu.flags
        self.cpu._push(saved_flags)
        self.cpu.tf = False
        self.cpu.if_flag = False
        self.cpu._push(self.cpu.cs)
        self.cpu._push(self.cpu.ip)
        self._dispatch_hardware_interrupt(vector)
        # Pop IP, CS, FLAGS (return to interrupted code)
        if not self.cpu.int_no_return:
            self._finish_interrupt_return(saved_flags)
        return True

    def _dispatch_hardware_interrupt(self, vector):
        """Dispatch a hardware IRQ.

        If DOS has replaced the IVT entry, transfer control to that handler and
        let its IRET consume the IRQ frame. Otherwise, use the built-in BIOS
        handler for the original BIOS stub-backed vectors.
        """
        ip = self.mem.read_word(vector * 4)
        cs = self.mem.read_word(vector * 4 + 2)
        bios_stub = self.bios.ivt_stubs.get(vector)

        self.cpu.int_no_return = False
        if bios_stub and (cs, ip) != bios_stub and (ip, cs) != (0, 0):
            self.cpu.cs = cs
            self.cpu.ip = ip
            self.cpu.int_no_return = True
            return

        self.bios.handle_interrupt(self.cpu, vector)

    def _finish_interrupt_return(self, saved_flags):
        """Restore CS:IP and merge handler result flags with saved control flags."""
        self.cpu.ip = self.cpu._pop()
        self.cpu.cs = self.cpu._pop()
        self.cpu._pop()  # Discard the stack copy; we already captured FLAGS.
        self.cpu.flags = self._merge_interrupt_flags(saved_flags, self.cpu.flags)

    def _merge_interrupt_flags(self, saved_flags, live_flags):
        """Preserve BIOS result flags while restoring IF/TF/DF from the interrupted code."""
        result_mask = 0x08D5  # CF, PF, AF, ZF, SF, OF
        return (saved_flags & ~result_mask) | (live_flags & result_mask)

    def _install_bios_interrupt_hook(self):
        """Route CPU software interrupts to BIOS handlers directly."""
        bios_ref = self.bios

        def hooked_interrupt(n):
            # Push flags, CS, IP (standard INT behavior)
            saved_flags = self.cpu.flags
            self.cpu._push(saved_flags)
            self.cpu.tf = False
            self.cpu.if_flag = False
            self.cpu._push(self.cpu.cs)
            self.cpu._push(self.cpu.ip)
            # Reset no-return flag
            self.cpu.int_no_return = False
            # Call BIOS handler (modifies registers; sets int_no_return for boot)
            bios_ref.handle_interrupt(self.cpu, n)
            # For normal interrupts: restore CS:IP and IRET-style control flags.
            if not self.cpu.int_no_return:
                self._finish_interrupt_return(saved_flags)

        self.cpu._do_interrupt = hooked_interrupt

    def run(self):
        """Initialize and run the emulator."""
        # Load or build boot sector
        if self.boot_file:
            print(f"[Loading boot sector from {self.boot_file}]", file=sys.stderr)
            with open(self.boot_file, 'rb') as f:
                boot_code = f.read()
            if len(boot_code) > 512:
                boot_code = boot_code[:512]
            elif len(boot_code) < 512:
                boot_code = boot_code + bytearray(512 - len(boot_code))
            # Check boot signature
            sig = boot_code[510] | (boot_code[511] << 8)
            if sig != 0xAA55:
                print(f"[WARNING: No boot signature (0x{sig:04X}), expected 0xAA55]", file=sys.stderr)
            self.disk.write_boot_sector(bytes(boot_code))
        elif self.floppy_image is None:
            boot_code = build_boot_sector()
            self.disk.write_boot_sector(boot_code)
        else:
            print("[Booting from floppy image boot sector...]", file=sys.stderr)

        # Initialize BIOS
        self.bios.initialize()

        # Initialize hardware
        if self.pic:
            self.pic.initialize()

        # Set up IVT for IRQ handlers
        self._setup_ivt_irq_handlers()

        # Display initial state
        self.video.display()
        print("\n[BIOS initialized. Booting...]\n", file=sys.stderr)
        if self.enable_hardware:
            print("  PIT: 8254 (1.193180 MHz)", file=sys.stderr)
            print("  PIC: 8259A (master+slave)", file=sys.stderr)
            print("  CMOS: MC146818 RTC", file=sys.stderr)
        time.sleep(0.5)

        # Load boot sector directly (skip INT 19h stack push)
        buf = bytearray(512)
        self.disk.read_sector(0, buf)
        for i in range(512):
            self.mem.write_byte(0x7C00 + i, buf[i])
        self.cpu.cs = 0x0000
        self.cpu.ip = 0x7C00
        self.cpu.ds = 0x0000
        self.cpu.es = 0x0000
        self.video.print_str(" OK", video_mod.Video.ATTR_GREEN, 36, 13)

        # Replace CPU interrupt handling entirely
        # (skip IVT lookup, call BIOS handlers directly)
        self._install_bios_interrupt_hook()

        # Run the CPU
        print("[Booting...]", file=sys.stderr)
        # Auto-feed a space key (for INT 16h wait) — skip in interactive mode
        if not self.interactive:
            if self.kbd_ctrl:
                self.kbd_ctrl.feed_string(" ")
            else:
                self.kbd.feed_string(" ")

        if self.interactive and not self.gtk:
            print("[Interactive mode: type keys, Ctrl+C to stop]", file=sys.stderr)
            import select
            import termios
            import tty
            import os as _os
            # Put the terminal into cbreak mode so each keystroke is
            # delivered immediately (no line buffering) and Enter produces
            # CR (0x0D) -- the value COMMAND.COM's DATE/TIME prompt expects
            # -- instead of LF (0x0A) which cooked mode yields. cbreak keeps
            # ISIG on, so Ctrl+C still raises KeyboardInterrupt to exit.
            # Only configure the terminal when stdin IS a real TTY; if input
            # is piped (e.g. `printf ... | main.py -i`) we just read bytes.
            self._term_fd = sys.stdin.fileno()
            self._term_old = None
            if sys.stdin.isatty():
                self._term_old = termios.tcgetattr(self._term_fd)
                tty.setcbreak(self._term_fd)
                sys.stdout.write("\033[24;1H")   # cursor to bottom-left
                sys.stdout.flush()
        elif self.gtk:
            print("[GTK mode: click the window and type; Ctrl+C or close "
                  "the window to stop]", file=sys.stderr)

        step = 0
        last_display = 0
        last_ip = None
        stuck_count = 0
        pit_ticks_since_last = 0
        # ~500 instructions per PIT tick (rough approximation for timing)
        pit_insn_interval = 500

        try:
            while True:
                if not self.cpu.halted:
                    if not self.cpu.execute():
                        break
                    step += 1

                if step > 10000000:
                    print(f"[Reached step limit of 10,000,000]", file=sys.stderr)
                    break

                # PIT tick: advance timer every N instructions
                if self.pit:
                    if self.cpu.halted:
                        self.io.tick(1.0 / 18.2)  # ~18.2 Hz
                    else:
                        pit_ticks_since_last += 1
                        if pit_ticks_since_last >= pit_insn_interval:
                            pit_ticks_since_last = 0
                            self.io.tick(1.0 / 18.2)  # ~18.2 Hz

                # Check for pending IRQs and dispatch
                if self.pic:
                    self._check_and_dispatch_irq()

                # Interactive: read one keystroke.  Two paths:
                #   - GTK mode: pump the Gtk main loop (handles redraw,
                #     key-press, and window-close events).  Key presses
                #     are injected into kbd_ctrl via the on_key callback set
                #     up in __init__, so we only need to pump here.
                #   - terminal mode: cbreak stdin read.
                if self.gtk:
                    if self.gtk_display.pump():
                        print("[GTK window closed]", file=sys.stderr)
                        break
                elif self.interactive:
                    try:
                        if select.select([sys.stdin], [], [], 0)[0]:
                            b = _os.read(0, 1)
                            if b:
                                key = b[0]
                                if self.kbd_ctrl:
                                    self.kbd_ctrl.inject_key(key)
                                else:
                                    self.kbd.buffer.append(key)
                    except (OSError, ValueError):
                        pass

                # Keyboard controller: inject scan codes → raise IRQ 1
                if self.kbd_ctrl and self.kbd_ctrl.has_data() and not self.kbd_ctrl.irq_pending:
                    self.kbd_ctrl.irq_pending = True
                    if self.pic:
                        self.pic.raise_irq(1)

                # Detect infinite loops
                cur_ip = (self.cpu.cs << 4) + self.cpu.ip
                if cur_ip == last_ip:
                    stuck_count += 1
                    if stuck_count > 100000:
                        print(f"[STUCK at CS:IP={self.cpu.cs:04X}:{self.cpu.ip:04X} "
                              f"after {step:,} instructions]", file=sys.stderr)
                        break
                else:
                    stuck_count = 0
                last_ip = cur_ip

                # Display video every 5000 instructions (terminal path only).
                # In GTK mode the per-batch pump() above already queued a
                # redraw and processed the expose event, so the terminal box
                # render would be wasted work (and would clobber the GUI's
                # stdout with ANSI escapes).
                if not self.gtk and step - last_display > 5000:
                    self.video.display()
                    last_display = step

                if step % 100000 == 0 and not self.gtk:
                    print(f"[Step {step:,}] CS:IP={self.cpu.cs:04X}:{self.cpu.ip:04X} AX={self.cpu.ax:04X} BX={self.cpu.bx:04X}", file=sys.stderr)

                # Check for halt
                if self.cpu.halted and not self.pic:
                    break
        except KeyboardInterrupt:
            print("\n[Interrupted by user]", file=sys.stderr)
        finally:
            # Restore terminal settings even if the loop broke or crashed
            # (terminal interactive path only; GTK mode never touched them).
            if self.interactive and not self.gtk and \
                    getattr(self, '_term_old', None) is not None:
                try:
                    termios.tcsetattr(self._term_fd, termios.TCSADRAIN, self._term_old)
                except (OSError, ValueError, NameError):
                    pass
            # Tear down the GTK window if it was opened.
            if self.gtk and self.gtk_display is not None:
                self.gtk_display.close()

        # Final display (terminal path only; GTK window already closed).
        if not self.gtk:
            self.video.display()
        status = self.cpu.status()
        print(f"\n[CPU HALTED] CS:IP={status['cs']:04X}:{status['ip']:04X} "
              f"Instructions: {step:,}", file=sys.stderr)
        print(f"  AX={status['ax']:04X} BX={status['bx']:04X} "
              f"CX={status['cx']:04X} DX={status['dx']:04X}", file=sys.stderr)
        print(f"  SP={status['sp']:04X} BP={status['bp']:04X} "
              f"SI={status['si']:04X} DI={status['di']:04X}", file=sys.stderr)
        print(f"  DS={status['ds']:04X} ES={status['es']:04X} "
              f"SS={status['ss']:04X} FL={status['flags']:04X}",
              file=sys.stderr)

        # Memory dump around current IP
        linear_ip = (status['cs'] << 4) + status['ip']
        print(f"\nMemory dump around IP {linear_ip:08X}:", file=sys.stderr)
        for i in range(-64, 64):
            addr = (linear_ip + i) & 0xFFFFF
            val = self.mem.read_byte(addr)
            print(f"{addr:08X}: {val:02X}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Simple BIOS Emulator")
    parser.add_argument('--boot', '-b', metavar='FILE',
                        help='Load boot sector from binary file (512 bytes)')
    parser.add_argument('--step', '-s', action='store_true',
                        help='Step mode: print mnemonic + registers each instruction')
    parser.add_argument('--interactive', '-i', action='store_true',
                        help='Interactive mode: read keys from stdin')
    parser.add_argument('--serial', action='store_true', default=True,
                        help='Enable COM1 serial output (default: on)')
    parser.add_argument('--no-serial', action='store_true',
                        help='Disable COM1 serial output')
    parser.add_argument('--floppy', '-f', metavar='IMG',
                        help='Load floppy image (FAT12, 1.44MB)')
    parser.add_argument('--gtk', '-g', action='store_true',
                        help='Use a GTK window for display + keyboard input '
                             '(replaces the terminal box; sidesteps cbreak/')
    parser.add_argument('--gtk-font-size', type=int, default=18,
                        metavar='PT',
                        help='Pango font point size for --gtk (default: 18)')
    args = parser.parse_args()

    print("=" * 60, file=sys.stderr)
    print("  Simple BIOS Emulator", file=sys.stderr)
    print("  x86 Real Mode | VGA Text | PIT/PIC/CMOS", file=sys.stderr)
    if args.boot:
        print(f"  Boot file: {args.boot}", file=sys.stderr)
    if args.step:
        print(f"  Step mode: ON", file=sys.stderr)
    if args.gtk:
        print(f"  Display: GTK window", file=sys.stderr)
    elif args.interactive:
        print(f"  Interactive: ON", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print()

    emu = Emulator(boot_file=args.boot, step_mode=args.step,
                   interactive=args.interactive, floppy_image=args.floppy,
                   gtk=args.gtk, gtk_font_size=args.gtk_font_size)
    emu.run()


if __name__ == "__main__":
    main()
