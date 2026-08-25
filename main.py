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

from cpu_backend import BACKENDS, create_cpu, normalize_backend
from video import Video, IO, Keyboard, Disk, Serial
from bios import BIOS
from hardware import PIT, PIC, CMOS, KeyboardController
from fat12 import FAT12, FAT12Error
from hostbridge import (audit_host_directory_deletions,
                        build_host_directory_disk, delete_missing_host_files,
                        snapshot_host_directory, sync_host_directory_disk)
from terminal_keyboard import ASCII, TerminalKeyDecoder
import video as video_mod


BUNDLED_DOS_IMAGE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'DOS3_3_525', 'DISK01.IMG')

_SNAP_GTK_VARIABLES = (
    'GTK_PATH', 'GTK_EXE_PREFIX', 'GTK_MODULES', 'GTK_IM_MODULE_FILE',
    'GIO_MODULE_DIR', 'SNAP_LIBRARY_PATH',
)


def schedule_pit_ticks(now, deadline, interval):
    """Return bounded elapsed PIT ticks and the next wall-clock deadline."""
    if now < deadline:
        return 0, deadline
    ticks = min(4, 1 + int((now - deadline) / interval))
    return ticks, now + interval


# A native CPU backend can execute a polling loop many thousands of times
# before the first 18.2 Hz timer interrupt is due.  Judge a repeated CS:IP by
# elapsed time rather than by loop iterations so timer-driven DOS programs do
# not get mistaken for a hung emulator.
STUCK_LOOP_SECONDS = 0.25


def sanitize_snap_gtk_environment(environ=None, executable=None):
    """Remove incompatible Snap host GTK paths from a native Python process.

    Snap-packaged editors export their own GTK/GIO module paths to integrated
    terminals.  A system ``/usr/bin/python3`` then mixes those Core runtime
    libraries with the host Gtk installation and can terminate in the dynamic
    linker before PyGObject can report an exception.  A Python executable
    genuinely running inside the same Snap is left untouched.

    Returns the names removed, primarily for startup reporting and tests.
    """
    environ = os.environ if environ is None else environ
    executable = sys.executable if executable is None else executable
    snap_root = environ.get('SNAP')
    if not snap_root:
        return ()

    snap_root = os.path.realpath(snap_root)
    executable = os.path.realpath(executable)
    if executable == snap_root or executable.startswith(snap_root + os.sep):
        return ()

    removed = []
    for name in _SNAP_GTK_VARIABLES:
        if name in environ:
            environ.pop(name)
            removed.append(name)

    # Some Snap launchers also export LD_LIBRARY_PATH.  Preserve a normal
    # user path, but discard it when it explicitly references the Snap root.
    library_path = environ.get('LD_LIBRARY_PATH', '')
    if library_path and (snap_root in library_path or '/snap/' in library_path):
        environ.pop('LD_LIBRARY_PATH')
        removed.append('LD_LIBRARY_PATH')
    return tuple(removed)


def create_hard_disk_image(path, cylinders=306):
    """Create a blank legacy C/4/17 hard-disk image without overwriting."""
    if not isinstance(cylinders, int) or not 1 <= cylinders <= 1024:
        raise ValueError('hard-disk cylinders must be an integer from 1 to 1024')
    sectors = cylinders * 4 * 17
    size = sectors * 512
    with open(path, 'xb') as image:
        image.truncate(size)
    return sectors, size


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

# Legacy PC floppy formats are fully described by their sector count:
# 8 spt for the earliest 160/320 KB 5.25" disks, 9 spt for 360 KB 5.25"
# and 720 KB 3.5", 15 spt for 1.2 MB 5.25", 18 spt for 1.44 MB 3.5".
# The media descriptor byte alone cannot pick between them (0xF9 covers
# both 720 KB and 1.44 MB depending on vendor convention), so exact
# geometry is pinned from the image size whenever it matches a known
# format.  INT 13h CHS translation then agrees with the boot sector's
# own BPB-derived arithmetic.
_FLOPPY_SIZE_GEOMETRY = {
    320: (40, 1, 8),    # 160 KB 5.25"
    640: (40, 2, 8),    # 320 KB 5.25"
    720: (40, 2, 9),    # 360 KB 5.25"
    1440: (80, 2, 9),   # 720 KB 3.5"
    2400: (80, 2, 15),  # 1.2 MB 5.25"
    2880: (80, 2, 18),  # 1.44 MB 3.5"
}


def _pin_floppy_geometry(disk, actual_sectors):
    """Pin CHS geometry on ``disk`` when the sector count is unambiguous."""
    geo = _FLOPPY_SIZE_GEOMETRY.get(actual_sectors)
    if geo is None:
        return False
    disk.cylinders, disk.heads, disk.sectors_per_track = geo
    return True


class Emulator:
    """Main emulator loop."""

    def __init__(self, boot_file=None, step_mode=False, interactive=False,
                 enable_hardware=True, floppy_image=None, floppy_b=None,
                 hard_disk=None, boot_drive=0x00, gtk=False, gtk_font_size=18,
                 persist=False, serial_output=True, host_dir=None,
                 host_dir_write=False, host_dir_delete=False,
                 host_dir_dos_text=False, max_instructions=10_000_000,
                 cpu_backend='python', pit_speed=1.0):
        self.memory = type('Memory', (), {})()
        self.cpu_backend = normalize_backend(cpu_backend)
        # We need a proper Memory class
        self.mem = self._create_memory()
        self.video = Video()
        self.video.attach_memory(self.mem)
        self.kbd = Keyboard()
        self.disk = Disk()
        self.serial = Serial(echo=serial_output)

        # Hardware devices
        self.pit = PIT() if enable_hardware else None
        self.pic = PIC() if enable_hardware else None
        self.cmos = CMOS() if enable_hardware else None
        self.kbd_ctrl = KeyboardController() if enable_hardware else None

        self.io = IO(self.video, self.kbd, self.disk, self.serial,
                     pit=self.pit, pic=self.pic, cmos=self.cmos,
                     kbd_ctrl=self.kbd_ctrl)
        self.step_mode = step_mode
        if not 0.25 <= pit_speed <= 8.0:
            raise ValueError('pit_speed must be between 0.25 and 8')
        self.pit_speed = float(pit_speed)
        self.cpu = self._new_cpu()
        if max_instructions < 1:
            raise ValueError('max-instructions must be positive')
        self.max_instructions = max_instructions
        self.bios = BIOS(self.mem, self.video, self.kbd, self.disk,
                         pit=self.pit, pic=self.pic, cmos=self.cmos,
                         kbd_ctrl=self.kbd_ctrl, serial=self.serial)
        self.boot_file = boot_file
        self.interactive = interactive or gtk   # --gtk implies interactive
        self.enable_hardware = enable_hardware
        # Second floppy drive (B:); populated by _load_floppy_b() below.
        self.disk_b = None
        self.hard_disk = None
        self.boot_drive = boot_drive

        # GTK display (optional).  When enabled, it takes over rendering and
        # keyboard input: the emulator loop pumps Gtk events between
        # instruction batches, and key callbacks inject physical set-1 scan
        # codes into the keyboard controller. Direct ASCII injection remains
        # available only for host clipboard paste.
        self.gtk = gtk
        self.gtk_display = None
        if gtk:
            removed = sanitize_snap_gtk_environment()
            if removed:
                print("[GTK] Ignoring incompatible Snap host library settings",
                      file=sys.stderr)
            from gtdisplay import GtkDisplay
            def _on_key(byte):
                if self.kbd_ctrl:
                    self.kbd_ctrl.inject_key(byte)
                else:
                    self.kbd.buffer.append(byte)
            def _on_extended_key(scan_code):
                if self.kbd_ctrl:
                    self.kbd_ctrl.inject_extended_key(scan_code)
                else:
                    # The reference keyboard buffer accepts the same tuple
                    # consumed by BIOS INT 16h as the hardware controller.
                    self.kbd.buffer.append((scan_code & 0xFF, 0))
            def _on_scan_code(scan_code):
                if self.kbd_ctrl:
                    self.kbd_ctrl.inject_scan_code(scan_code)
            self.gtk_display = GtkDisplay(
                self.video, on_key=_on_key, on_extended_key=_on_extended_key,
                on_scan_code=_on_scan_code,
                on_reset=self.reset_guest,
                on_refresh=self.refresh_host_dir,
                on_eject=self.eject_host_dir,
                close_warning=self._close_warning,
                font_size=gtk_font_size)

        # FAT12 filesystem
        self.floppy_image = floppy_image
        self.floppy_b_image = floppy_b
        self.host_dir = host_dir
        self.host_dir_write = host_dir_write
        self.host_dir_dos_text = host_dir_dos_text
        self.host_dir_snapshot = {}
        self.host_dir_delete = host_dir_delete
        self.fat = None
        # Original (pre-padding) sector count of the loaded image, so --persist
        # writes back exactly the image's on-disk size instead of the 1.44MB
        # in-memory padding.
        self._image_sectors = None
        self.persist = persist
        self.stop_reason = 'completed'
        if floppy_image:
            self._load_floppy(floppy_image)
        if floppy_b:
            self._load_floppy_b(floppy_b)
        elif host_dir:
            self._load_host_dir(host_dir)
        if host_dir:
            self.host_dir_snapshot = snapshot_host_directory(host_dir)
        self.hard_disk_image = hard_disk
        self._hard_disk_sectors = None
        if hard_disk:
            self._load_hard_disk(hard_disk)
        self.bios.boot_drive = self.boot_drive
        if self.gtk_display is not None:
            self.gtk_display.set_media_status(self._media_status())
            self.gtk_display.set_session_status(
                'Ready • writes ' + ('enabled' if self.persist else 'discarded'))

        # Write BIOS ROM string
        bios_str = b"SIMPLE BIOS"
        for i, b in enumerate(bios_str):
            self.mem.write_byte(0xF0000 + i, b)

    def reset_guest(self):
        """Perform a soft hardware reset from the GTK control bar."""
        self.mem.ram[:0xA0000] = b'\x00' * 0xA0000
        self.video.clear()
        self.kbd.buffer.clear()
        if self.kbd_ctrl:
            self.kbd_ctrl._out_buffer.clear()
            self.kbd_ctrl._scan_buffer.clear()
            self.kbd_ctrl._port_buffer.clear()
            self.kbd_ctrl._raw_buffer.clear()
            self.kbd_ctrl._physical_buffer.clear()
            self.kbd_ctrl._bios_key_buffer.clear()
            self.kbd_ctrl._state_buffer.clear()
            self.kbd_ctrl._scan_fifo.clear()
            self.kbd_ctrl._irq_port_event = None
            self.kbd_ctrl._output_ready = False
            self.kbd_ctrl._next_output_time = 0.0
            self.kbd_ctrl._out_full = False
            self.kbd_ctrl.irq_pending = False
        self.cpu = self._new_cpu()
        boot_disk = self.hard_disk if self.boot_drive == 0x80 else self.disk
        buf = bytearray(512)
        boot_disk.read_sector(0, buf)
        for i, value in enumerate(buf):
            self.mem.write_byte(0x7C00 + i, value)
        self.cpu.cs = 0x0000
        self.cpu.ip = 0x7C00
        self.cpu.dl = self.boot_drive
        self.bios.initialize()
        if self.pic:
            self.pic.initialize()
        self._setup_ivt_irq_handlers()
        self._install_bios_interrupt_hook()
        if self.gtk_display is not None:
            self.gtk_display.set_session_status(
                'Running • writes ' + ('enabled' if self.persist else 'discarded'))
        if self.gtk_display is not None:
            self.gtk_display.show_cursor()

    def _new_cpu(self):
        """Construct the selected CPU backend for initial boot or reset."""
        cpu = create_cpu(self.cpu_backend, self.mem, self.io)
        cpu.step_mode = self.step_mode
        return cpu

    def _media_status(self):
        """Return compact GUI media labels, marking guest-dirty devices."""
        def label(letter, path, disk):
            if path and disk is None:
                return f'{letter}: ejected'
            name = os.path.basename(path) if path else 'none'
            return f'{letter}: {name}' + (' *' if disk and disk.dirty else '')
        return '  '.join((
            label('A', self.floppy_image, self.disk),
            label('B', getattr(self, 'floppy_b_image', None), self.disk_b),
            label('C', self.hard_disk_image, self.hard_disk)))

    def _close_warning(self):
        """Return a GTK close warning only when non-persistent writes exist."""
        if self.persist and not self.host_dir_delete:
            return None
        dirty = self._dirty_media()
        if not dirty:
            return None
        if self.host_dir_delete and self.host_dir_write:
            return ('Guest writes to ' + ', '.join(dirty) +
                    ' will be persisted, and files removed by DOS may be '
                    'deleted from the host folder. Close anyway?')
        return ('Guest writes to ' + ', '.join(dirty) +
                ' will be discarded. Close anyway?')

    def _dirty_media(self):
        dirty = []
        if getattr(self.disk, 'dirty', False):
            dirty.append('A:')
        if self.disk_b and self.disk_b.dirty:
            dirty.append('B:')
        if self.hard_disk and self.hard_disk.dirty:
            dirty.append('C:')
        return dirty

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
        """Keep the BIOS-installed IRQ callback vectors intact.

        ``BIOS.initialize()`` installs an IRET-compatible ROM stub for INT
        1Ch.  Guest programs commonly hook the timer callback and chain to
        the vector they found during installation, so replacing that stub
        with ``0000:0000`` is not equivalent to an empty callback: the guest
        eventually executes the IVT as code.  There is no additional IRQ IVT
        setup required here; hardware IRQ dispatch already consults the IVT.

        The method remains as a compatibility hook for the harness and the
        diagnostic scripts that call it after BIOS initialization.
        """

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
        self._image_sectors = actual_sectors
        media_byte = data[0x15] if len(data) > 0x15 else 0xF9
        # DOS 1.x system disks predate the conventional BPB.  Their byte
        # at 15h is often a vendor/reserved value (the Compaq 1.10 image
        # uses BBh), while the first FAT byte still carries the real media
        # descriptor.  Prefer that descriptor for the small legacy formats
        # so INT 13h geometry/media queries made by IOSYS work correctly.
        if (media_byte not in (0xF0, 0xF1, 0xF2, 0xF8, 0xF9)
                and actual_sectors in (320, 640) and len(data) >= 513):
            fat_media = data[512]
            if fat_media in (0xFE, 0xFF):
                media_byte = fat_media
        media_names = {0xFD: '360KB (5.25")', 0xFE: '160KB (5.25")', 0xFF: '320KB (5.25")',
                       0xF8: '360KB (5.25")', 0xF0: '1.2MB (5.25")',
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
        # Pin the physical geometry by image size.  The media descriptor
        # alone is ambiguous: 0xF9 means both 1.44 MB (18 spt) and 720 KB
        # 3.5" (9 spt), and DOS 1.x boot sectors carry no BPB at all.  The
        # image size uniquely identifies the legacy cylinder/head layout.
        _pin_floppy_geometry(self.disk, actual_sectors)

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

    def _load_floppy_b(self, path: str):
        """Load the second-drive (B:) floppy image into a fresh Disk.

        The BIOS already references ``self.disk_b``; we create it here and
        point the BIOS at it so INT 13h DL=01 dispatches to drive B.
        """
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except FileNotFoundError:
            print(f"[ERROR] Floppy B image not found: {path}", file=sys.stderr)
            sys.exit(1)
        self.disk_b = Disk()
        self.bios.disk_b = self.disk_b
        media_byte = data[0x15] if len(data) > 0x15 else 0xF9
        actual_sectors = max(len(data) // 512, 1)
        padded = data + b'\x00' * (max(0, 2880 - len(data) // 512) * 512)
        for i in range(min(2880, len(padded) // 512)):
            buf = bytearray(512)
            buf[:512] = padded[i * 512:(i + 1) * 512]
            self.disk_b.write_sector(i, buf)
        self.disk_b.media_type = media_byte
        _pin_floppy_geometry(self.disk_b, actual_sectors)
        print(f"  Floppy B: {len(data)//1024}KB, {actual_sectors} sectors, "
              f"media=0x{media_byte:02X}", file=sys.stderr)

    def _load_host_dir(self, path: str):
        """Attach a read-only host directory as DOS drive B:."""
        try:
            self.disk_b = build_host_directory_disk(
                path, dos_text=self.host_dir_dos_text)
        except (OSError, ValueError) as exc:
            raise ValueError(f"--host-dir: {exc}") from exc
        self.bios.disk_b = self.disk_b
        self.disk_b.read_only = not self.host_dir_write
        self.floppy_b_image = f"host:{os.path.abspath(path)}"
        mode = 'write-back enabled' if self.host_dir_write else 'read-only FAT12'
        print(f"  Host folder B: {os.path.abspath(path)} ({mode})",
              file=sys.stderr)

    def refresh_host_dir(self):
        """Rebuild the read-only host-folder disk currently attached as B:."""
        if not self.host_dir:
            return False
        try:
            self.disk_b = build_host_directory_disk(
                self.host_dir, dos_text=self.host_dir_dos_text)
        except (OSError, ValueError) as exc:
            print(f"[host bridge] refresh failed: {exc}", file=sys.stderr)
            if self.gtk_display is not None:
                self.gtk_display.set_session_status('Refresh failed')
            return False
        self.bios.disk_b = self.disk_b
        self.disk_b.read_only = not self.host_dir_write
        self.host_dir_snapshot = snapshot_host_directory(self.host_dir)
        if self.gtk_display is not None:
            self.gtk_display.set_media_status(self._media_status())
            self.gtk_display.set_session_status(
                'Running • writes ' + ('enabled' if self.persist else 'discarded'))
        print(f"[host bridge] refreshed B: from {self.host_dir}", file=sys.stderr)
        return True

    def eject_host_dir(self):
        """Detach the host-folder disk from BIOS drive B:."""
        if not self.host_dir or self.disk_b is None:
            return False
        self.disk_b = None
        self.bios.disk_b = None
        if self.gtk_display is not None:
            self.gtk_display.set_media_status(self._media_status())
            self.gtk_display.set_session_status('B: ejected')
        print('[host bridge] ejected B:', file=sys.stderr)
        return True

    def _persist_host_dir(self):
        if not self.host_dir_write or not self.host_dir or not self.disk_b:
            return
        if not self.disk_b.dirty:
            return
        try:
            changed, conflicts = sync_host_directory_disk(
                self.disk_b, self.host_dir, self.host_dir_snapshot)
            preserved = audit_host_directory_deletions(self.disk_b, self.host_dir)
            self.disk_b.dirty = False
            print(f'[persist] host-folder B: {len(changed)} file(s) updated '
                  f'in {self.host_dir}', file=sys.stderr)
            for path in changed:
                print(f'  [persist] {path}', file=sys.stderr)
            if conflicts:
                print(f'[persist] skipped {len(conflicts)} host conflict(s)',
                      file=sys.stderr)
                for path in conflicts:
                    print(f'  [persist] conflict: {path}', file=sys.stderr)
            if self.host_dir_delete and preserved:
                removed = delete_missing_host_files(self.disk_b, self.host_dir)
                print(f'[persist] deleted {len(removed)} host file(s) removed '
                      'by the guest', file=sys.stderr)
                preserved = []
            if preserved:
                print(f'[persist] preserved {len(preserved)} host file(s) absent '
                      'from the guest image (deletion disabled)', file=sys.stderr)
                for path in preserved:
                    print(f'  [persist] preserved {path}', file=sys.stderr)
        except (OSError, ValueError, FAT12Error) as exc:
            print(f'[persist] host-folder write failed: {exc}', file=sys.stderr)

    def _load_hard_disk(self, path: str):
        """Load a raw legacy-CHS hard-disk image as BIOS drive 80h."""
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except FileNotFoundError:
            print(f"[ERROR] Hard disk image not found: {path}", file=sys.stderr)
            sys.exit(1)
        if len(data) == 0 or len(data) % 512:
            raise ValueError("hard-disk image size must be a non-zero multiple of 512")

        sectors = len(data) // 512
        heads, spt = 4, 17
        cylinders = sectors // (heads * spt)
        if not 1 <= cylinders <= 1024 or cylinders * heads * spt != sectors:
            raise ValueError(
                "hard-disk image must use an exact C/4/17 legacy CHS geometry")
        self.hard_disk = Disk(
            sectors, cylinders=cylinders, heads=heads,
            sectors_per_track=spt, hard_disk=True)
        self.bios.hard_disk = self.hard_disk
        for i in range(sectors):
            self.hard_disk.sectors[i][:] = data[i * 512:(i + 1) * 512]
        self.hard_disk.dirty = False
        self._hard_disk_sectors = sectors
        print(f"  Hard disk: {len(data)//1024}KB, {cylinders}/{heads}/{spt} CHS",
              file=sys.stderr)

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
        if irq == 1 and self.kbd_ctrl:
            self.kbd_ctrl.begin_irq()
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

    def _schedule_keyboard_irq(self):
        """Raise IRQ 1 for queued controller data without claiming another IRQ."""
        if (not self.kbd_ctrl or not self.pic
                or self.pic.is_irq_pending(1)):
            return False
        # A real 8042 presents only one keyboard byte in its output buffer.
        # Advance the serial stream after the preceding IRQ/EOI, not when the
        # host enqueues the whole E0 make/break sequence.
        self.kbd_ctrl.service_input()
        if not self.kbd_ctrl.has_output_data():
            return False
        self.kbd_ctrl.irq_pending = True
        self.pic.raise_irq(1)
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

    def _merge_bios_flags_into_outer_frame(self):
        """Propagate handler result flags into a chained BIOS stub's IRET frame.

        The BIOS ROM stubs are ``INT n; IRET``.  A guest that chains with the
        standard ``PUSHF; CALL FAR [vec]`` pattern leaves its original FLAGS on
        the stack just above the call's CS:IP (SS:SP+4 once the inner Python
        INT hook has unwound its own frame).  The inner hook merges handler
        result flags into the inner return, but the stub's *outer* IRET would
        pop that stale PUSHF word and discard ZF/CF.  Rewrite it here so the
        stub's IRET restores the correct flags.
        """
        addr = self.cpu._phys(self.cpu.ss, (self.cpu.sp + 4) & 0xFFFF)
        saved = self.cpu._readw(addr)
        merged = self._merge_interrupt_flags(saved, self.cpu.flags)
        self.cpu._writew(addr, merged)

    def _install_bios_interrupt_hook(self):
        """Route CPU software interrupts to BIOS handlers directly."""
        bios_ref = self.bios

        def hooked_interrupt(n):
            # Push flags, CS, IP (standard INT behavior)
            entry_cs, entry_ip = self.cpu.cs, self.cpu.ip
            stub = bios_ref.ivt_stubs.get(n)
            at_bios_stub = (stub is not None
                            and (entry_cs, entry_ip) == (stub[0], stub[1] + 2))
            retry_state = self.cpu._retry_interrupt_state
            continuing_retry = (retry_state is not None
                                and retry_state[:3] ==
                                (n, self.cpu.cs, self.cpu.ip))
            saved_flags = retry_state[3] if continuing_retry else self.cpu.flags
            self.cpu._push(self.cpu.flags)
            self.cpu.tf = False
            self.cpu.if_flag = False
            self.cpu._push(self.cpu.cs)
            self.cpu._push(self.cpu.ip)
            # Reset no-return flag
            self.cpu.int_no_return = False
            self.cpu.retry_software_interrupt = False
            # Call BIOS handler (modifies registers; sets int_no_return for boot)
            bios_ref.handle_interrupt(self.cpu, n)
            # For normal interrupts: restore CS:IP and IRET-style control flags.
            if not self.cpu.int_no_return:
                self._finish_interrupt_return(saved_flags)
                if self.cpu.retry_software_interrupt:
                    # INT imm8 is two bytes.  Repeating it lets the outer loop
                    # deliver hardware IRQs and pump host input while keeping
                    # AH=00h/10h truly blocking from the guest's perspective.
                    self.cpu._retry_interrupt_state = (
                        n, self.cpu.cs, self.cpu.ip, saved_flags)
                    self.cpu.ip = (self.cpu.ip - 2) & 0xFFFF
                    # The IBM BIOS enables maskable interrupts while waiting
                    # for keyboard input, then IRET restores the caller's IF.
                    # Keep IRQ1 deliverable between host-side retry slices.
                    self.cpu.if_flag = True
                else:
                    # Successful completion: this stub will IRET shortly, so
                    # fold the handler result flags into its outer PUSHF word.
                    if at_bios_stub:
                        self._merge_bios_flags_into_outer_frame()
                    if continuing_retry:
                        self.cpu._retry_interrupt_state = None
                    elif retry_state is not None:
                        # A hardware IRQ handler may invoke an unrelated
                        # software interrupt while a keyboard wait is pending.
                        # Preserve the exact outer retry instead of letting
                        # that nested INT consume its marker.
                        self.cpu.retry_software_interrupt = True
            elif retry_state is not None:
                self.cpu.retry_software_interrupt = True

        self.cpu._do_interrupt = hooked_interrupt

    def run(self):
        """Initialize and run the emulator."""
        write_mode = 'enabled' if self.persist else 'discarded'
        print(f"[Session] starting • writes {write_mode}", file=sys.stderr)
        if self.gtk_display is not None:
            self.gtk_display.set_session_status(f'Booting • writes {write_mode}')
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
        elif self.floppy_image is None and self.boot_drive == 0x00:
            boot_code = build_boot_sector()
            self.disk.write_boot_sector(boot_code)
        elif self.boot_drive == 0x00:
            print("[Booting from floppy image boot sector...]", file=sys.stderr)
        elif self.boot_drive == 0x80:
            if self.hard_disk is None:
                raise ValueError("hard-disk boot requested without --hard-disk")
            print("[Booting from hard-disk MBR...]", file=sys.stderr)
        else:
            raise ValueError(f"unsupported BIOS boot drive 0x{self.boot_drive:02X}")

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
        boot_disk = (self.hard_disk if self.boot_drive == 0x80 else self.disk)
        boot_disk.read_sector(0, buf)
        for i in range(512):
            self.mem.write_byte(0x7C00 + i, buf[i])
        self.cpu.cs = 0x0000
        self.cpu.ip = 0x7C00
        self.cpu.ds = 0x0000
        self.cpu.es = 0x0000
        self.cpu.dl = self.boot_drive
        self.video.print_str(" OK", video_mod.Video.ATTR_GREEN, 36, 13)
        self.bios.set_text_cursor(14, 0)

        # Replace CPU interrupt handling entirely
        # (skip IVT lookup, call BIOS handlers directly)
        self._install_bios_interrupt_hook()

        # Run the CPU
        print("[Booting...]", file=sys.stderr)
        if self.gtk_display is not None:
            self.gtk_display.set_session_status(f'Running • writes {write_mode}')
        # Auto-feed a space key (for INT 16h wait) — skip in interactive mode
        if not self.interactive:
            if self.kbd_ctrl:
                self.kbd_ctrl.feed_string(" ")
            else:
                self.kbd.feed_string(" ")

        terminal_keyboard = None
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
            terminal_keyboard = TerminalKeyDecoder()
            if sys.stdin.isatty():
                self._term_old = termios.tcgetattr(self._term_fd)
                tty.setcbreak(self._term_fd)
                sys.stdout.write("\033[24;1H")   # cursor to bottom-left
                sys.stdout.flush()
        elif self.gtk:
            print("[GTK mode: click the window and type; Ctrl+Shift+C or "
                  "close the window to stop]", file=sys.stderr)

        step = 0
        last_display = 0
        last_ip = None
        stuck_count = 0
        stuck_since = time.monotonic()
        # Keep the PIT's simulated hardware elapsed time independent from
        # how frequently it is scheduled.  Passing the shorter scheduling
        # interval to ``io.tick`` would cancel --pit-speed exactly.
        pit_tick_duration = 1.0 / 18.2065  # IBM PC PIT channel 0 / 65536
        pit_interval = pit_tick_duration / self.pit_speed
        pit_next_tick = time.monotonic() + pit_interval
        gtk_last_frame = 0.0
        gtk_poll_counter = 0
        # Native graphics batches can complete far faster than text-mode DOS
        # work. Check the GTK clock more often in graphics mode so the 30 FPS
        # render cap is reachable instead of being limited by a 100-batch
        # polling cadence.
        gtk_graphics_poll_batches = 8
        gtk_text_poll_batches = 100

        try:
            while True:
                if not self.cpu.halted:
                    native = getattr(self.cpu, 'execute_many', None)
                    if native is not None and not self.step_mode:
                        preferred = getattr(self.cpu, 'preferred_batch_size', None)
                        batch_size = (preferred() if preferred is not None
                                      else getattr(self.cpu, 'native_batch_size', 4096))
                        executed = native(batch_size)
                    else:
                        executed = 1 if self.cpu.execute() else 0
                    if not executed:
                        self.stop_reason = 'CPU halted'
                        break
                    step += executed

                if step > self.max_instructions and not self.interactive:
                    self.stop_reason = 'instruction limit reached'
                    print(f"[Reached step limit of {self.max_instructions:,}]",
                          file=sys.stderr)
                    break

                # PIT tick: advance timer against wall-clock time
                if self.pit:
                    now = time.monotonic()
                    elapsed_ticks, pit_next_tick = schedule_pit_ticks(
                        now, pit_next_tick, pit_interval)
                    if elapsed_ticks:
                        for _ in range(elapsed_ticks):
                            self.io.tick(pit_tick_duration)

                # Check for pending IRQs and dispatch
                if self.pic:
                    self._check_and_dispatch_irq()

                # Interactive: read one keystroke.  Two paths:
                #   - GTK mode: pump the Gtk main loop (handles redraw,
                #     key-press, and window-close events).  Key presses
                #     are injected into kbd_ctrl via the on_key callback set
                #     up in __init__, so we only need to pump here.
                #   - terminal mode: decode xterm escape sequences and inject
                #     their corresponding BIOS key events.
                if self.gtk:
                    # Drawing the complete 80x25 Pango grid after every guest
                    # instruction starves DOS during boot.  Check the clock in
                    # small batches and cap GTK work at about 30 frames/sec;
                    # this remains responsive while leaving nearly all CPU
                    # time to emulation.
                    gtk_poll_counter += 1
                    poll_batches = (gtk_graphics_poll_batches
                                    if self.video.graphics_mode
                                    else gtk_text_poll_batches)
                    if gtk_poll_counter >= poll_batches:
                        gtk_poll_counter = 0
                        now = time.monotonic()
                        if now - gtk_last_frame >= 1 / 30:
                            gtk_last_frame = now
                            if self.gtk_display.pump():
                                self.stop_reason = 'GTK window closed'
                                print("[GTK window closed]", file=sys.stderr)
                                break
                            self.gtk_display.set_media_status(self._media_status())
                elif self.interactive:
                    try:
                        now = time.monotonic()
                        key_events = []
                        if select.select([sys.stdin], [], [], 0)[0]:
                            data = _os.read(self._term_fd, 64)
                            key_events.extend(
                                terminal_keyboard.feed(data, now))
                        key_events.extend(terminal_keyboard.flush(now))
                        for kind, value in key_events:
                            if kind == ASCII:
                                if self.kbd_ctrl:
                                    self.kbd_ctrl.inject_key(value)
                                else:
                                    self.kbd.buffer.append(value)
                            elif self.kbd_ctrl:
                                self.kbd_ctrl.inject_extended_key(value)
                            else:
                                self.kbd.buffer.append((value, 0))
                    except (OSError, ValueError):
                        pass

                # Keyboard controller: inject scan codes → raise IRQ 1
                self._schedule_keyboard_irq()

                # Detect infinite loops
                cur_ip = (self.cpu.cs << 4) + self.cpu.ip
                if self.cpu._retry_interrupt_state is not None:
                    # A blocking BIOS call intentionally remains on its INT
                    # instruction while the main loop pumps external input.
                    stuck_count = 0
                    stuck_since = time.monotonic()
                elif cur_ip == last_ip:
                    stuck_count += 1
                    if time.monotonic() - stuck_since > STUCK_LOOP_SECONDS:
                        self.stop_reason = 'stuck instruction loop'
                        print(f"[STUCK at CS:IP={self.cpu.cs:04X}:{self.cpu.ip:04X} "
                              f"after {step:,} instructions]", file=sys.stderr)
                        break
                else:
                    stuck_count = 0
                    stuck_since = time.monotonic()
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
            self.stop_reason = 'interrupted by user'
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
            dirty_before_persist = self._dirty_media()
            if dirty_before_persist and not self.persist:
                print('[persist] discarded guest writes on ' +
                      ', '.join(dirty_before_persist) +
                      ' (--persist was not supplied)', file=sys.stderr)
            self._persist_floppy()
            self._persist_hard_disk()
            self._persist_host_dir()

            # Final display (terminal path only; GTK window already closed).
            if not self.gtk:
                self.video.display()
            status = self.cpu.status()
            summary = (f"[Session] stopped • {self.stop_reason} • writes {write_mode} • "
                       f"{step:,} instructions")
            print(f"\n{summary}", file=sys.stderr)
            if self.gtk_display is not None:
                self.gtk_display.set_session_status(
                    f'Stopped • {self.stop_reason}')
            print(f"[Emulator stopped] CS:IP={status['cs']:04X}:{status['ip']:04X}",
                  file=sys.stderr)
            # Register state and a 128-byte memory dump are debugging output,
            # useful in --step mode but noisy after a normal GUI close.
            if self.cpu.step_mode:
                print(f"  AX={status['ax']:04X} BX={status['bx']:04X} "
                      f"CX={status['cx']:04X} DX={status['dx']:04X}", file=sys.stderr)
                print(f"  SP={status['sp']:04X} BP={status['bp']:04X} "
                      f"SI={status['si']:04X} DI={status['di']:04X}", file=sys.stderr)
                print(f"  DS={status['ds']:04X} ES={status['es']:04X} "
                      f"SS={status['ss']:04X} FL={status['flags']:04X}",
                      file=sys.stderr)

                linear_ip = (status['cs'] << 4) + status['ip']
                print(f"\nMemory dump around IP {linear_ip:08X}:", file=sys.stderr)
                for i in range(-64, 64):
                    addr = (linear_ip + i) & 0xFFFFF
                    val = self.mem.read_byte(addr)
                    print(f"{addr:08X}: {val:02X}", file=sys.stderr)

    def _persist_floppy(self):
        """Write dirty disk sectors back to the loaded image (only if --persist).

        Writes exactly ``_image_sectors`` sectors so a 360KB image stays
        360KB on disk rather than ballooning to the 1.44MB in-memory padding.
        """
        if not self.persist or not self.floppy_image:
            return
        if not getattr(self.disk, 'dirty', False):
            return
        n = self._image_sectors or len(self.disk.sectors)
        try:
            with open(self.floppy_image, 'r+b') as f:
                f.seek(0)
                for i in range(n):
                    f.write(self.disk.sectors[i])
            print(f"[persist] wrote {n} sectors back to {self.floppy_image}",
                  file=sys.stderr)
        except OSError as e:
            print(f"[persist] failed: {e}", file=sys.stderr)

    def _persist_hard_disk(self):
        """Write a dirty hard disk back when ``--persist`` is enabled."""
        if not self.persist or not self.hard_disk_image or self.hard_disk is None:
            return
        if not self.hard_disk.dirty:
            return
        try:
            with open(self.hard_disk_image, 'r+b') as f:
                f.seek(0)
                for sector in self.hard_disk.sectors:
                    f.write(sector)
            self.hard_disk.dirty = False
            print(f"[persist] wrote {len(self.hard_disk.sectors)} hard-disk sectors "
                  f"back to {self.hard_disk_image}", file=sys.stderr)
        except OSError as e:
            print(f"[persist] hard-disk write failed: {e}", file=sys.stderr)


def build_argument_parser():
    """Build the user-facing CLI parser independently for fast testing."""
    parser = argparse.ArgumentParser(
        prog='python3 main.py',
        description='Run an x86 real-mode PC with BIOS, VGA, and DOS disks.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''examples:
  python3 main.py --dos                 boot bundled DOS in this terminal
  python3 main.py --dos --gtk           boot bundled DOS in a GTK window
  python3 main.py -f disk.img -i        boot a floppy with terminal input
  python3 main.py --hard-disk hd.img --boot-hard-disk --gtk
  python3 main.py --create-hard-disk hd.img --hard-disk-cylinders 306
  python3 main.py --dos --host-dir ./dos-files --gtk

Disk writes are discarded unless --persist is supplied.  The --dos shortcut
always protects the bundled image and therefore cannot be used with --persist.''')

    boot = parser.add_argument_group('boot media')
    boot.add_argument('--boot', '-b', metavar='FILE',
                      help='load a custom 512-byte boot sector')
    floppy = boot.add_mutually_exclusive_group()
    floppy.add_argument('--dos', action='store_true',
                        help='boot the bundled MS-DOS 3.3 disk (changes discarded)')
    floppy.add_argument('--floppy', '-f', metavar='IMG',
                        help='boot a FAT12 floppy image')
    boot.add_argument('--floppy-b', metavar='IMG',
                      help='attach a second floppy image as drive B:')
    boot.add_argument('--hard-disk', metavar='IMG',
                      help='attach an exact C/4/17 raw image as BIOS drive 80h')
    boot.add_argument('--boot-hard-disk', action='store_true',
                      help='boot the attached hard-disk MBR instead of drive A:')

    storage = parser.add_argument_group('disk image tools')
    storage.add_argument('--create-hard-disk', metavar='IMG',
                         help='create a blank legacy C/4/17 image and exit')
    storage.add_argument('--hard-disk-cylinders', type=int, default=306,
                         metavar='N',
                         help='cylinders for --create-hard-disk, 1..1024 '
                              '(default: 306, about 10 MB)')
    storage.add_argument('--host-dir', metavar='DIR',
                         help='expose a host folder read-only as DOS drive B:')
    storage.add_argument('--host-dir-dos-text', action='store_true',
                         help='normalize known host text files to DOS CR/LF')
    storage.add_argument('--host-dir-write', action='store_true',
                         help='allow host-folder write-back (requires --persist)')
    storage.add_argument('--host-dir-delete', action='store_true',
                         help='delete host files removed by DOS (requires write-back)')

    display = parser.add_argument_group('display and input')
    display.add_argument('--interactive', '-i', action='store_true',
                         help='read keyboard input from the terminal')
    display.add_argument('--gtk', '-g', action='store_true',
                         help='use a GTK window for display and keyboard input')
    display.add_argument('--gtk-font-size', type=int, default=18, metavar='PT',
                         help='GTK font size from 6 to 72 points (default: 18)')

    runtime = parser.add_argument_group('runtime')
    runtime.add_argument('--cpu-backend', choices=BACKENDS, default='python',
                         help='CPU implementation: python (reference/default) '
                              'or c (optional native backend)')
    runtime.add_argument('--step', '-s', action='store_true',
                         help='print each instruction and register state')
    runtime.add_argument('--max-instructions', type=int, default=10_000_000,
                         metavar='N',
                         help='noninteractive instruction limit (default: 10000000)')
    runtime.add_argument('--pit-speed', type=float, default=1.0, metavar='N',
                         help='PIT/timer speed multiplier, 0.25..8 (default: 1)')
    serial = runtime.add_mutually_exclusive_group()
    serial.add_argument('--serial', dest='serial_output', action='store_true',
                        default=True,
                        help='enable COM1 serial output (default)')
    serial.add_argument('--no-serial', dest='serial_output', action='store_false',
                        help='disable COM1 serial output')
    runtime.add_argument('--persist', action='store_true',
                         help='write modified sectors back on clean exit')
    return parser


def parse_args(argv=None):
    """Parse and normalize CLI arguments, reporting actionable errors."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.boot_hard_disk and not args.hard_disk:
        parser.error('--boot-hard-disk requires --hard-disk IMG')
    if not 6 <= args.gtk_font_size <= 72:
        parser.error('--gtk-font-size must be between 6 and 72')
    if args.dos and args.persist:
        parser.error('--dos protects the bundled image; use --floppy with a copy '
                     'if you want --persist')
    if args.create_hard_disk:
        conflicting = []
        if args.boot or args.dos or args.floppy or args.floppy_b or args.host_dir:
            conflicting.append('boot media')
        if args.hard_disk or args.boot_hard_disk:
            conflicting.append('--hard-disk/--boot-hard-disk')
        if args.persist or args.gtk or args.interactive or args.step:
            conflicting.append('runtime/display options')
        if conflicting:
            parser.error('--create-hard-disk is a create-only command; remove '
                         + ', '.join(conflicting))
        if not 1 <= args.hard_disk_cylinders <= 1024:
            parser.error('--hard-disk-cylinders must be between 1 and 1024')
        if os.path.exists(args.create_hard_disk):
            parser.error(f'--create-hard-disk: refusing to overwrite existing '
                         f'file: {args.create_hard_disk}')

    if args.host_dir:
        if args.floppy_b:
            parser.error('--host-dir cannot be combined with --floppy-b')
        if args.persist:
            parser.error('--host-dir is read-only and cannot be used with --persist')
        if not os.path.isdir(args.host_dir):
            parser.error(f'--host-dir: directory not found: {args.host_dir}')
    if args.host_dir_write:
        if not args.host_dir:
            parser.error('--host-dir-write requires --host-dir DIR')
        if not args.persist:
            parser.error('--host-dir-write requires --persist')
    if args.host_dir_dos_text and not args.host_dir:
        parser.error('--host-dir-dos-text requires --host-dir DIR')
    if args.host_dir_delete:
        if not args.host_dir_write:
            parser.error('--host-dir-delete requires --host-dir-write')
    if args.max_instructions < 1:
        parser.error('--max-instructions must be positive')
    if not 0.25 <= args.pit_speed <= 8.0:
        parser.error('--pit-speed must be between 0.25 and 8')

    if args.dos:
        args.floppy = BUNDLED_DOS_IMAGE
        # A one-flag DOS launch should accept input immediately.  GTK already
        # implies interactive behavior inside Emulator, but normalizing this
        # here also makes the selected behavior explicit in startup output.
        args.interactive = True

    for option, path in (('--boot', args.boot), ('--floppy', args.floppy),
                         ('--floppy-b', args.floppy_b),
                         ('--hard-disk', args.hard_disk)):
        if path and not os.path.isfile(path):
            parser.error(f'{option}: file not found: {path}')
    return parser, args


def main(argv=None):
    parser, args = parse_args(argv)

    if args.create_hard_disk:
        try:
            sectors, size = create_hard_disk_image(
                args.create_hard_disk, args.hard_disk_cylinders)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print(f'Created {args.create_hard_disk}: '
              f'{args.hard_disk_cylinders}/4/17 CHS, '
              f'{sectors:,} sectors, {size:,} bytes')
        print('Next: attach it with --hard-disk, run FDISK, exit, then '
              'relaunch and run FORMAT C: /S.')
        return

    print("=" * 60, file=sys.stderr)
    print("  Simple BIOS Emulator", file=sys.stderr)
    print("  x86 Real Mode | VGA Text | PIT/PIC/CMOS", file=sys.stderr)
    if args.boot:
        print(f"  Boot file: {args.boot}", file=sys.stderr)
    if args.step:
        print(f"  Step mode: ON", file=sys.stderr)
    print(f"  CPU backend: {args.cpu_backend}", file=sys.stderr)
    if args.gtk:
        print(f"  Display: GTK window", file=sys.stderr)
    elif args.interactive:
        print(f"  Interactive: ON", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print()

    try:
        emu = Emulator(boot_file=args.boot, step_mode=args.step,
                       interactive=args.interactive, floppy_image=args.floppy,
                       floppy_b=args.floppy_b, hard_disk=args.hard_disk,
                       boot_drive=0x80 if args.boot_hard_disk else 0x00,
                       gtk=args.gtk, gtk_font_size=args.gtk_font_size,
                       persist=args.persist, serial_output=args.serial_output,
                       host_dir=args.host_dir,
                       host_dir_write=args.host_dir_write,
                       host_dir_delete=args.host_dir_delete,
                       host_dir_dos_text=args.host_dir_dos_text,
                       max_instructions=args.max_instructions,
                       cpu_backend=args.cpu_backend, pit_speed=args.pit_speed)
        emu.run()
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
