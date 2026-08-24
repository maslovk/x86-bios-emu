# Simple BIOS Emulator

A Python-based x86 real-mode CPU emulator with a minimal BIOS implementation, VGA text mode video, keyboard, and floppy disk emulation.

## Architecture

```
x86-bios-emu/
├── cpu.py             # x86 real-mode CPU core + step debugger (2000+ lines)
├── bios.py            # BIOS ROM (IVT, POST, INT 10h–2Bh)
├── video.py           # VGA 80x25 text + I/O ports + COM1 serial + keyboard + disk
├── hardware.py        # PIT (8254), PIC (8259A), CMOS RTC (MC146818), Keyboard (i8042)
├── fat12.py           # FAT12/FAT16 reader + writer (BPB, FAT, dir, chains, blank-image factory)
├── cpu_backend.py     # Explicit Python/native CPU backend boundary
├── c_cpu_native.py    # Optional Unicorn-backed native 16-bit CPU adapter
├── main.py            # Emulator harness + sample boot sector + IRQ dispatch + floppy loader
├── gtdisplay.py       # Optional GTK window display (real keyboard capture, CGA colours)
├── trace_boot.py      # Boot tracer with INT 13h/INT 10h call logging
├── trace_dos.py       # DOS-boot INT 21h/13h/2Fh call + return-value tracer
├── debug_dos.py       # DOS 3.3 boot debugger (INT 13h trace + BDA dump)
├── snapshot_capture.py# Capture full CPU+1MB memory state at a trigger point for diff tracing
├── diff_trace.py      # Differential single-step tracer: my CPU vs Unicorn (QEMU)
├── probe_*.py         # IVT/device-chain/snapshot probes (one-shot diagnostics)
├── check_*.py         # GTK render/keyboard smoke tests + pty interactive test
├── DOS3_3_525/         # MS-DOS 3.3 floppy images (DISK01.IMG, DISK02.IMG)
└── tests/             # pytest suite (1389 fast + 84 slow: CPU/BIOS/disk/FAT, DOS tools)
```

## Components

### CPU Core (`cpu.py`)
- Full x86 real-mode instruction decoder
- 16-bit registers: AX, BX, CX, DX, SP, BP, SI, DI
- Segment registers: CS, DS, ES, SS
- Flags: CF, PF, AF, ZF, SF, TF, IF, DF, OF
- Instruction support:
  - Data transfer: MOV (all forms), PUSH (reg/imm/memory), POP (reg/memory), XCHG, LES/LDS, MOV r/m,imm (C6/C7)
  - Arithmetic: ADD, SUB, INC, DEC, NEG, MUL, IMUL, DIV, IDIV, AAM, AAD, SALC, DAA, DAS, AAA, AAS (BCD, SDM-verified vs Unicorn)
  - Logic: AND, OR, XOR, NOT, TEST, SHL, SHR, SAR, ROL, ROR
  - Control flow: JMP (near/far), JE/JZ, JNE/JNZ, JB/JAE, JL/JGE, JBE/JA, JO/JNO, JPE/JPO, CALL, RET, RETF, LOOP, JCXZ
  - String: MOVS[BW], CMPS[BW], STOS[BW], LODS[BW], SCAS[BW] (DF-aware, REP/REPNE)
  - Stack: PUSHA/POPA, ENTER/LEAVE
  - Flags: PUSHF/POPF, STC/CLD/STD/CMC, SETcc (all 16 conditions), LAHF/SAHF
  - System: INT, IRET, CLI, STI, HLT, WAIT (no-op, no 8087), XLAT (honours segment-override prefix)
  - Trap flag single-step: TF set -> INT 1 after the instruction (with the one-instruction POPF/IRET delay), enabling DEBUG -T
  - LAHF/SAHF store/load flags low byte to/from AH (not AL)
  - Segment overrides: ES:/CS:/SS:/DS: prefixes (applied to next memory instruction, including XLAT)
  - BP-based addressing defaults to SS segment (per x86 spec); all offsets masked to 16 bits
  - Shift/rotate flag semantics: scalar shifts (SHL/SHR/SAR/SAL) set SF/ZF/PF from the result and clear AF; rotates (ROL/ROR/RCL/RCR) only touch CF/OF, per Intel SDM
  - REP/REPE/REPNE string ops with CX=0 are no-ops (CMPSB/CW/SCASB/W checked before first iteration)

### BIOS ROM (`bios.py`)
- Interrupt Vector Table (IVT) initialization
- POST (Power-On Self-Test) with system info display
- Interrupt handlers:
  - **INT 08h**: IRQ 0 timer handler (BDA tick increment + INT 1Ch callback)
  - **INT 09h**: IRQ 1 keyboard handler (scan code → ASCII, EOI)
  - **INT 0Ah**: IRQ 2 cascade handler
  - **INT 10h**: Video services (AH=00h set mode, AH=13h write string)
  - **INT 11h**: Equipment list (bit 0 = floppy drives installed, bits 6-7 =
    floppy count - 1, bit 10 = one serial port; returns 0x0411 with one floppy,
    0x0451 with two — mirrored into BDA `0040:0010`. DOS 4.0's SYSINIT depends
    on bit 0)
  - **INT 12h**: Memory size (returns 640K)
  - **INT 13h**: Disk services (AH=00h reset, AH=02h read CHS, AH=08h params, AH=42h LBA extended)
  - **INT 15h**: Misc services (AH=87h move block, AH=88h ext memory, AH=CA CRC-32)
  - **INT 16h**: Keyboard (AH=00h wait key, AH=01h status, AH=02h shift state)
  - **INT 19h**: Boot loader (loads sector 0 → 0x7C00, jumps)
  - **INT 1Ah**: System time (AH=00h ticks, AH=02h RTC time, AH=04h RTC date)
  - **INT 1Ch**: Timer tick callback (chained by OS/TSR)
  - **INT 20h**: Terminate program
  - **INT 29h**: Char output (direct to video)
  - **INT 2Bh**: Country info

### Video (`video.py`)
- 80x25 color text mode (16 colors)
- Cursor positioning
- Scroll support
- I/O port emulation (keyboard 0x60/0x64, PIT 0x61, CMOS 0x804)

### Serial Port (`video.py`)
- COM1 emulation (ports 0x3F8-0x3FF)
- THR (transmit) outputs to stderr with `[COM1]` prefix
- RBR, LSR, MSR, IIR registers
- 8N1 line control, 9600 baud default

### Keyboard (`video.py`)
- PS/2 keyboard emulation
- Scan code to ASCII translation
- Key buffer for INT 16h

### Disk (`video.py`)
- Floppy disk emulation (auto-detects size: 360KB/720KB/1.2MB/1.44MB)
- Media type detection from BPB (offset 0x15)
- INT 13h sector read (CHS and LBA extended via AH=42h)
- INT 13h AH=08 returns geometry matching media type
- CHS-to-LBA conversion uses correct sectors-per-track per format
- Boot sector loaded from sector 0

### PIT — 8254 Programmable Interval Timer (`hardware.py`)
- Three 16-bit counters with 1.193180 MHz input clock
- Counter 0 → IRQ 0 (system timer, ~18.2 Hz with default reload 0x0000)
- Counter 1 → VGA DAC (not emulated)
- Counter 2 → Speaker (not emulated)
- Ports: 0x40-0x42 (counter data), 0x43 (command)
- Supports rate generator (mode 3), square wave (mode 2)
- BCD and binary count modes
- Tick count exposed via INT 1Ah

### PIC — 8259A Programmable Interrupt Controller (`hardware.py`)
- Master PIC: ports 0x20 (command/EOI), 0x21 (mask)
- Slave PIC: ports 0xA0 (command/EOI), 0xA1 (mask)
- IRQ 0-7 → Master → Vectors 0x08-0x0F
- IRQ 8-15 → Slave → Vectors 0x70-0x77 (cascaded via IRQ 2)
- Full ICW1-4 initialization sequence
- Specific and non-specific EOI
- Interrupt masking per IRQ line
- Priority-based IRQ dispatch

### CMOS RTC — MC146818 (`hardware.py`)
- Port 0x70: Address register
- Port 0x71: Data register
- 128 bytes of NVRAM
- Registers 0x00-0x07: Time/date (BCD format)
- Register 0x0C: Century
- Register 0x0A: RTC control (update-in-progress flag)
- Register 0x0B: RTC control 2 (24h/12h, BCD/binary)
- Registers 0x32-0x33: BIOS signature (0x12, 0x56)
- Auto-syncs with system time on read

### Keyboard Controller — i8042 (`hardware.py`)
- Port 0x60: Data port (read/write scan codes and ASCII)
- Port 0x64: Status/command port (OBF, IBF, IRQ pending flags)
- Scan code set 1 (AT) with E0 extended prefix support
- Left/right Shift/Ctrl/Alt and CapsLock/NumLock/ScrollLock state tracking
- Scan code → ASCII translation with modifier application
- IRQ 1 generation for make, break, modifier, and E0-prefix bytes
- BIOS keyboard flags mirrored at `0040:0017`, `0040:0018`, and `0040:0096`
- FIFO output buffer (multiple keys queued)
- LED control via 0xED command
- Self-test (0xAA), input port read (0xD0), command byte (0x20)

### FAT12 Filesystem (`fat12.py`)
- Full FAT12 parser for 1.44 MB / 360 KB floppy images (BPB + DOS 1.x media fallback)
- BPB parsing: sector size, cluster size, FAT count, root entries; cluster-chain following
- Read: find by name, read cluster chains, load to memory, read subdirectory (read_dir)
- **Write (host side)**: write_file (first-fit cluster alloc, both FAT copies mirrored, root
  dir entry), delete_file, set_fat_entry, free_cluster_count; make_blank_image() factory
- Extended INT 13h (AH=42h): LBA sector reads via Disk Access Packet (DAP)
- CLI: `--floppy image.img` loads and mounts FAT12 automatically; `--persist` writes
  guest-modified sectors back to the image on clean exit

### Building a private DOS 1.x image

`build_dos125_image.py` creates a non-destructive 160 KB DOS 1.x FAT12 image
from a boot-sector image and rebuilt system files:

```bash
python3 build_dos125_image.py /tmp/dos125.img \
  --io DOS_sources/v1.25/source/IO.SYS \
  --msdos DOS_sources/v1.25/source/MSDOS.SYS \
  --command DOS_sources/v1.25/source/COMMAND.COM
```

The output must not already exist; source images and source files are never
modified.  DOS 1.x images without a conventional BPB are supported: when the
reserved media byte is nonstandard, the emulator derives the floppy media
descriptor from the first FAT byte (`FEh`/`FFh`) so IOSYS receives the correct
geometry through INT 13h.

The checked-out `DOS_sources/v1.25/source/IO.ASM` is configured for the
320 KB double-sided 5.25-inch target (`SMALL=1`, `SMALLDS=1`); regenerate
`IO.HEX` with the historical SCP assembler after changing this configuration.
When using the host-folder bridge, SCP 2.44 selects source and HEX output
drives from the filename extension.  With both files on bridged B:, run:

```dos
B:
ASM IO.BB
```

### Historical OEM DOS images

Historical source trees and disk images used for local experiments are kept
outside the tracked project content.  They are useful compatibility fixtures,
but are not bundled or redistributed by this repository.

- DOS 1.x images with a legacy `FEh`/`FFh` FAT media byte are recognized as
  160 KB/320 KB media even when their boot sectors have no conventional BPB.
- A standard 360 KB image uses 40 cylinders, two heads, and nine sectors per
  track.  This geometry is retained after the emulator pads an image in memory.
- The Eagle Computer MS-DOS 2.0 image (`DOS2/Eagle_Computer_DOS20.IMD`, decode
  with `imd_decode.py`) boots to its OEM banner ("Eagle Computer MS-DOS
  version 2.00") and then fails at `Bad or missing Command Interpreter`.
  This is **not** the DOS 4.00 CDS/equipment-word issue: DOS 2.0's SYSINIT
  predates the `Fake_Floppy_Drv` logic, `\DEV\CON` opens normally, and the
  `\CONFIG.SYS` search executes (returning plain file-not-found).  The real
  failure is inside SYSINIT's `EXEC` of `\COMMAND.COM`: the Eagle OEM block
  driver reports an Eagle-native BPB (1 KiB sectors, media byte `0xFA`,
  patched over the FAT id `0xFD` at 0070:06B5), so DOS computes the root
  directory at logical sector 5 of 1 KiB, which the driver maps to 512-byte
  LBA 9/10 instead of the image's actual root at LBA 5.  The directory
  search then never sees `COMMAND.COM` and `EXEC` returns error 2.  Resolving
  this requires an Eagle OEM device/BPB compatibility layer (the IMD header
  identifies a "spirit/pc-plus" IBM-compatible diskette, while the driver
  defaults to its native 1 KiB-sector geometry).
- **MS-DOS 4.00 boots** (`DOS4/OPERATI3.IMG`, the bootable system disk): IO.SYS,
  MSDOS.SYS, CONFIG.SYS handling, and COMMAND.COM all run to an interactive
  `A>` prompt where `DIR`/`ECHO` and external programs like `GRAFTABL` work
  (see `tests/test_dos4_boot.py`).  The single blocker had been INT 11h: DOS
  4.0's SYSINIT tests equipment-word **bit 0** (floppy drives installed) and,
  with it clear, fakes drives A:/B: by zeroing the CDS DPB pointers — every
  path open then fails with error 3 and the boot ends at `Bad or missing
  Command Interpreter`.  INT 11h and the BDA word at `0040:0010` now report
  bit 0 set (and the memory size at `0040:0013` is a proper word).  Root
  cause localised with `probe_dos4_open.py` (kernel canonicalizer at
  `0286:8299` rejecting the CDS) and the MS-DOS 4.0 source release
  (`SYSINIT1.ASM` TEMPCDS / `Fake_Floppy_Drv`).
- **MS-DOS 5.00 boots** (`DOS5/Disk01.img`, the interactive 720 KB 3.5"
  Setup disk): the boot sector loads IO.SYS, DOS starts, and the Setup UI
  reaches its Welcome screen, advances through hardware configuration and
  into the install phase (see `tests/test_dos5_boot.py`).  Three contracts
  had to be fixed for it:
  1. **INT 13h geometry for 0xF9 media** — the byte is ambiguous (1.44 MB
     18-spt *and* 720 KB 9-spt both use it), so a 1440-sector image is now
     pinned to 80/2/9 CHS; previously every head-1 sector read landed 9
     tracks off and IO.SYS executed garbage (`ljmp 0x70:0` into zeros).
  2. **Port 61h refresh toggle** — DOS 5 IO.SYS's PS/2 keyboard init waits
     for bit 4 (DRAM-refresh check toggle) to flip; the port returned a
     constant and the wait loop spun forever.
  3. **INT 16h blocking semantics** — `AH=00`/`AH=10h` now wait (bounded
     spin) for a key instead of returning a phantom NUL immediately.
     MS-DOS 5 Setup drains the type-ahead buffer before issuing the real
     blocking read, and treats each phantom NUL as an unusable input event,
     spinning forever.  Because the drain eats any key queued ahead of it,
     the second ENTER must arrive *during* the blocked read —
     `DOSHarness.inject_background` injects from a thread for exactly
     that pattern.
- Xerox and SCP OEM images use their own direct hardware controller paths;
  they need dedicated device emulation rather than generic PC INT 13h support.
- **MS-DOS 6.22 boots** (`DOS6_22/disk01.img`, the EXEPACK-compressed 1.44 MB
  Setup disk): the kernel decompresses, DOS starts, and Setup reaches its
  welcome flow — without a hard disk it shows the legitimate "does not have
  a hard disk" dialog, and with a blank legacy HDD image it shows the full
  "To set up MS-DOS now, press ENTER" welcome (see `tests/test_dos6_boot.py`).
  The blocker was a quartet of CPU semantics bugs (all Unicorn-verified via
  the differential tracer `probe_dos6_diff.py`):
  1. `TEST r/m,r` (opcodes 84h/85h) ignored the ModRM **reg** field and
     always tested AL/AX — `TEST BX,BX` with AX=0 always reported ZF=1, so
     SYSINIT's EXEPACK bit-stream decompressor walked the wrong branch for
     every record, terminated early, and the relocated kernel was mostly
     zeros (boot slid into garbage via the relocation `retf`).
  2. `INC`/`DEC` (r16, r/m8, r/m16) never set **AF** (nibble borrow/carry).
  3. Logic ops left **AF** stale instead of clearing it.
  4. `SHR r/m,1` computed **OF** from the result; the SDM defines it as the
     MSB of the original operand.
  After the fixes the decompressor matches Unicorn for 200,000 consecutive
  instructions (register-exact).

  Verify the two supported Setup stages with the historical image present:

  ```bash
  python3 -m pytest -q tests/test_dos6_boot.py -m slow
  ```

  The test without a hard disk stops at the expected hardware dialog; the
  second test creates a private legacy HDD and reaches the full Setup welcome.

### BIOS Interrupt Handlers
- **INT 08h**: IRQ 0 timer handler (increments BDA ticks at 0x046C, calls INT 1Ch)
- **INT 09h**: IRQ 1 keyboard handler (reads ASCII from i8042, stores in kbd buffer, EOI)
- **INT 0Ah**: IRQ 2 cascade handler
- **INT 15h**: Miscellaneous (AH=87h move block, AH=88h ext memory size, AH=CA CRC-32)
- **INT 1Ah**: System time (AH=00h get ticks, AH=02h get RTC time, AH=04h get RTC date)
- **INT 1Ch**: Timer tick callback (chained by OS/TSR)
- **Exceptions**: INT 00h (divide by zero), INT 01h (NMI), INT 04h (into overflow)

## Boot Sector

The sample boot sector (`main.py::build_boot_sector()`) demonstrates:
1. Stack and segment initialization
2. Video mode setup (80x25 color)
3. String output via INT 10h AH=13h
4. Keyboard input via INT 16h
5. Graceful halt via HLT instruction

## Usage

```bash
cd x86-bios-emu
python3 main.py                          # Run with built-in boot sector
python3 main.py --boot file.bin          # Load external boot sector (512 bytes)
python3 main.py --step                   # Step mode: trace each instruction
python3 main.py --interactive            # Interactive: read keys from stdin
python3 main.py --gtk                    # GTK window display + real keyboard capture
python3 main.py --dos                    # Bundled DOS 3.3 + terminal keyboard
python3 main.py --dos --gtk              # Bundled DOS 3.3 in one GTK command
python3 main.py --dos --host-dir ./dos-files --gtk  # Read-only host folder as B:
python3 main.py --dos --host-dir ./DOS_sources/v1.25/source --host-dir-dos-text --gtk
python3 main.py --dos --host-dir ./dos-files --host-dir-write --persist --gtk
python3 main.py --dos --cpu-backend python --gtk  # Explicit reference CPU
python3 main.py --floppy disk.img --gtk  # Boot DOS floppy in a window
python3 main.py --create-hard-disk harddisk.img --hard-disk-cylinders 306
# For a larger FAT16-sized image, use: --hard-disk-cylinders 615
python3 main.py --floppy disk.img --hard-disk harddisk.img --persist --gtk
# In DOS: run FDISK, exit/relaunch, then run FORMAT C: /S
python3 main.py --floppy disk.img --hard-disk harddisk.img --boot-hard-disk --gtk
python3 main.py --boot file.bin --step   # Combine flags
python3 main.py --floppy disk.img         # Load FAT12 floppy image (auto-detect size)
python3 main.py --boot dos3.3.img         # Load DOS 3.3 boot sector
python3 main.py --boot dos3.3.img --step  # Step through DOS 3.3 boot
```

### Automated MS-DOS 6.22 installation

With the retail `Disk1.img`, `Disk2.img`, and `Disk3.img` files under
`DOS6_22/`, create and verify a new bootable 20.4 MiB FAT16 hard-disk image:

```bash
python3 install_dos622.py dos622-new.hdd
python3 install_dos622.py dos622-new.hdd --cpu-backend c  # Force native CPU
python3 main.py --floppy DOS6_22/Disk1.img \
  --hard-disk dos622-new.hdd --boot-hard-disk --gtk
```

The automation boots the real Microsoft Setup program, answers its dialogs,
restarts when requested, and swaps Setup Disks 2 and 3 in drive A. Setup itself
partitions and formats the hard disk, expands the distribution, and writes the
startup configuration—exactly as an interactive floppy installation would.
Work happens on a private temporary image and is published atomically only after
FAT16 validation and a hard-disk boot where `VER` must report MS-DOS 6.22.
Source floppy images are authenticated, hashed again afterward, and never written.
The installer defaults to `--cpu-backend auto`, which uses the native C-backed
Unicorn engine when available and applies the selected backend to partitioning,
Setup, and final boot verification. Use `--cpu-backend python` for the slower
reference implementation.

### Options
| Flag | Description |
|------|-------------|
| `--boot FILE` / `-b` | Load boot sector from binary file (padded to 512 bytes) |
| `--step` / `-s` | Print mnemonic + full register state every instruction |
| `--interactive` / `-i` | Read keyboard input from stdin (Ctrl+C to stop; needs a TTY) |
| `--gtk` / `-g` | Open a GTK window rendering the 80x25 VGA grid with proper keyboard capture (recommended for interactive DOS use) |
| `--gtk-font-size PT` | Pango font point size for `--gtk` (default 18) |
| `--no-serial` | Disable COM1 serial port output |
| `--dos` | Safely boot the bundled MS-DOS 3.3 disk and enable input (`--persist` is intentionally rejected) |
| `--floppy IMG` / `-f` | Load floppy image (FAT12, auto-detects 360KB–1.44MB) and mount filesystem |
| `--floppy-b IMG` | Load a second floppy image as drive B: (enables `DIR B:`, `COPY B:..`, DISKCOPY/DISKCOMP) |
| `--hard-disk IMG` | Attach an exact 1..1024-cylinder C/4/17 raw hard-disk image as BIOS drive 80h (tested at 306 cylinders/FAT12 and 615 cylinders/FAT16) |
| `--host-dir DIR` | Expose a host folder as read-only DOS drive B: |
| `--host-dir-dos-text` | Normalize known host text files to DOS CR/LF in the guest image (requires `--host-dir`; host files stay unchanged) |
| `--host-dir-write` | Enable explicit host-folder write-back (requires `--persist`) |
| `--host-dir-delete` | Delete host files removed by DOS (requires both write-back flags) |
| `--create-hard-disk IMG` | Create a blank legacy C/4/17 hard-disk image and exit; refuses to overwrite an existing file |
| `--hard-disk-cylinders N` | Cylinder count for `--create-hard-disk` (1..1024, default 306; 306 is about 10 MB) |
| `--boot-hard-disk` | Load the attached hard-disk MBR at 0000:7C00 and boot with DL=80h instead of booting floppy A: |
| `--cpu-backend {python,c}` | Select the CPU implementation; `python` is the complete reference path and default, while `c` requires the optional native backend |
| `--persist` | Write guest-modified sectors back to attached floppy/hard-disk images on exit (default off; never use on the shipped repo images) |

The emulator runs for ~1 second, displays the VGA screen, then exits with final CPU state.

### CPU backend separation

The Python CPU remains the reference implementation and the default. All
normal tests, BIOS callbacks, DOS harnesses, and full-fidelity debugging use
it unchanged. The `--cpu-backend` boundary lets a native implementation be
introduced without moving or weakening that path:

```bash
python3 main.py --dos --cpu-backend python --gtk
```

The `c` choice is intentionally opt-in and requires the optional `unicorn`
and `capstone` packages from `requirements-dev.txt`:

```bash
python3 -m pip install -r requirements-dev.txt
python3 main.py --dos --cpu-backend c --gtk
```

The native backend uses Unicorn's C x86 engine for 16-bit instruction
execution, while the existing Python BIOS, DOS, and device callbacks remain
authoritative for interrupts and I/O. This makes it useful for long-running
guest programs without changing the complete Python reference path. It is
still experimental: native RAM is shared directly with the emulator, and
translation blocks are flushed only around interrupts that can modify guest
code. Native instruction counts are batch-bounded around interrupt hooks, and
unusual software or hardware may require the Python backend for maximum
compatibility.

## Display modes

The emulator supports two VGA output paths:

- **Terminal** (default) — renders the 80x25 grid as an aligned box-drawing
  frame with batched ANSI colour escapes.  ANSI is auto-disabled when stdout
  isn't a TTY so output stays readable in pipes/logs.  With `--interactive`,
  xterm-compatible arrows, navigation keys, F1-F12, Shift+Tab, modified keys,
  Alt shortcuts, Enter, and Backspace are translated to DOS BIOS key events.
- **GTK** (`--gtk` / `-g`) — opens a real `Gtk.DrawingArea` window, paints
  each cell's CGA background + foreground colour, and captures key presses
  as physical set-1 make/break scan codes through the keyboard controller.
  Clipboard paste remains an intentional direct-text convenience. This is
  paired with Reset (full guest reboot), Refresh B: (host-folder reload), Paste/Copy (host clipboard text), Fullscreen, and a resizable
  A:/B:/C: media-status bar. Ctrl+Shift+C/R/V provide host copy/stop, reset,
  and paste shortcuts; Ctrl+Shift+F11 toggles fullscreen. Unshifted Ctrl
  combinations and F1-F12 reach DOS.
  The session indicator shows booting/running/stopped state and whether guest
  disk writes will be persisted or discarded. Dirty media is marked with `*`,
  and closing with unpersisted writes requires confirmation.
  the recommended path for interactive DOS use: it sidesteps the cbreak /
  scan-code-remapping pitfalls of terminal stdin, and Enter yields `0x0D`
  (CR) — what COMMAND.COM's DATE/TIME prompts expect.  Ctrl+C in the
  window stops the emulator; closing the window ends the run.

GTK requires PyGObject + Gtk 3 + PangoCairo (Debian/Ubuntu:
`apt install python3-gi gir1.2-gtk-3.0 gir1.2-pango-1.0`).  The dependency
is loaded lazily, so `main.py` imports fine without it; `--gtk` raises a
clear error only when actually used.

## Technical Details

### Memory Map
- 0x00000-0x9FFFF: 640K conventional memory
- 0xF0000-0xFFFFF: BIOS ROM (64K)
- Boot sector loaded at 0x7C00

### Real Mode Addressing
- Physical address = (segment << 4) + offset
- Example: CS:IP = 0x07C0:0x0000 → physical 0x7C00

### Interrupt Flow
1. CPU pushes FLAGS, CS, IP onto stack
2. BIOS handler called (Python function)
3. Handler modifies CPU registers
4. For normal interrupts: IP, CS, FLAGS popped (return)
5. For INT 19h (boot): CS:IP set to boot sector, no return

## DOS Boot Compatibility

The emulator can load and execute real DOS boot sectors:

```bash
python3 main.py --boot dos3.3.img          # Load DOS 3.3 boot sector
python3 main.py --boot dos3.3.img --step   # Step through DOS boot
python3 main.py --floppy dos3.3.img        # Load full floppy + mount FAT12
```

**Supported floppy formats:**
| Media | Size   | Media Byte | Geometry (C/H/SPT) |
|-------|--------|------------|---------------------|
| 5.25" | 360KB  | 0xFD       | 40/2/9              |
| 5.25" | 1.2MB  | 0xF8       | 80/2/15             |
| 3.5"  | 720KB  | 0xF1       | 80/2/9              |
| 3.5"  | 1.44MB | 0xF9       | 80/2/18             |

**Current DOS 3.3 status:** Boots to a fully interactive `A>` prompt.
Boot-sector relocation, IO.SYS relocation, MSDOS.SYS relocation, DOS kernel
initialisation, SYSINIT, and COMMAND.COM all run to completion. Internal
commands work: `DIR` lists all 34 files with sizes and timestamps, `ECHO`
prints text, `VER` reports the DOS version, `CLS` clears the screen, etc.

```
Current date is Mon  1-07-1980
Enter new date (mm-dd-yy):
Current time is  0:00:11.25
Enter new time:
Microsoft(R) MS-DOS(R)  Version 3.30
             (C)Copyright Microsoft Corp 1981-1987
A>DIR
 Volume in drive A has no label
 Directory of  A:\

IO       SYS    22357   7-24-87  12:00a
COMMAND  COM    25276   7-24-87  12:00a
...
SYS      COM     4725   7-24-87  12:00a
       34 File(s)      5120 bytes free
A>
```

To reach the interactive `A>` prompt, use the GTK display (recommended) and
serve the DATE/TIME prompts:

```bash
python3 main.py --floppy DOS3_3_525/DISK01.IMG --gtk
# When the 'Enter new date (mm-dd-yy):' prompt appears, type e.g. 01-01-1980
# and press Enter; same for 'Enter new time'.  The DOS A> prompt follows.
```

The terminal `--interactive` path also works and puts a real TTY into cbreak
mode. It decodes common xterm escape sequences for navigation, function, and
Alt keys; piped input can still have timing issues because the keys may arrive
before COMMAND.COM's prompt is up.

This was unblocked by five CPU-emulation bugs found via a Unicorn (QEMU-based)
differential single-step trace against memory snapshots captured at the
failing boundary (see `diff_trace.py`, `snapshot_capture.py`,
`tests/test_shift_flags.py`):

1. **Scalar shift flag semantics** (`cpu.py::_do_shift`): SHL/SHR/SAR/SAL
   were only updating CF/OF, leaving SF/ZF/PF stale. DOS's `SHL BX,1` after
   `XOR BH,BH` read the wrong PF and mis-dispatched every device open.
2. **XLAT segment-override prefix** (`cpu.py`, 0xD7): `CS: XLAT` was reading
   from `DS:BX+AL` instead of `CS:BX+AL`, corrupting DOS's country-info
   table lookup.
3. **LAHF/SAHF AH/AL swap** (`cpu.py`, 0x9E/0x9F): LAHF stored flags into
   AL instead of AH, and SAHF read from AL instead of AH. COMMAND.COM's
   internal command parser uses LAHF for string comparison, so every
   internal command returned "Bad command or file name".
4. **Physical address 16-bit wrap** (`cpu.py::_phys`): `[SI+disp8]` addressing
   with a negative displacement (e.g. SI=0x005C + disp=0xFFFF) computed
   `0x1005B` without wrapping to 16 bits, reading from the wrong physical
   address. DIR printed its header but could not find any files.
5. **REPE CMPSB/CMPSW/SCASB/SCASW with CX=0** (`cpu.py`, 0xA6-A7/AE-AF):
   the `while True` loop ran one comparison BEFORE checking CX=0, wrapping
   CX to 0xFFFF and corrupting SI/DI. DOS's FCB directory search used REPE
   CMPSB at a loop boundary where CX was 0, so every directory search
   returned "File not found".

The differential trace now runs 20,000+ instructions with zero divergence
between this CPU and Unicorn across the entire OPEN-CON and FCB-FINDF paths.

## DOS 3.3 diskette catalog

The shipped `DOS3_3_525/DISK01.IMG` is the bootable 360 KB system disk. Its
37 root entries are:

| Files | Role and emulator status |
|---|---|
| `IO.SYS`, `MSDOS.SYS`, `COMMAND.COM` | Boot files and command shell; DOS boot is tested. |
| `4201.CPI`, `5202.CPI`, `COUNTRY.SYS`, `MS330PP0.1` | Code-page, country, and installation data. |
| `ANSI.SYS`, `DRIVER.SYS` | Config drivers; boot smoke-tested. |
| `APPEND.EXE`, `ASSIGN.COM`, `FASTOPEN.EXE`, `GRAFTABL.COM`, `JOIN.EXE`, `MODE.COM`, `PRINT.COM`, `SUBST.EXE` | Device/path utilities; load or functional probes are covered. |
| `ATTRIB.EXE`, `LABEL.COM` | File attributes and volume labels; tested. |
| `CHKDSK.COM`, `RECOVER.COM` | Disk checking/recovery; CHKDSK is fully checked and RECOVER has a no-crash usage check. |
| `COMP.COM`, `DISKCOMP.COM`, `DISKCOPY.COM` | File/disk comparison and copying; tested. |
| `EDLIN.COM`, `EXE2BIN.EXE` | Editing and binary conversion; tested. |
| `FDISK.COM` | Fixed-disk partitioning; tested when `--hard-disk` is attached. Without one it says “No fixed disks present.” |
| `FIND.EXE`, `MORE.COM`, `SORT.EXE` | Text filtering, paging, and sorting; tested, including pipelines. |
| `FORMAT.COM`, `SYS.COM` | Formatting and system transfer; tested, including bootable fixed-disk workflows. |
| `GRAPHICS.COM`, `SELECT.COM`, `DISPLAY.SYS`, `KEYB.COM`, `NLSFUNC.EXE` | Hardware/locale-dependent tools; graceful fallback tested. |

The optional `DOS3_3_525/DISK02.IMG` contains 16 tool/data entries:

| Files | Role and emulator status |
|---|---|
| `BACKUP.COM`, `RESTORE.COM` | Backup/restore; single-file round-trip tested. |
| `DEBUG.COM` | Machine-code debugger/assembler; core commands tested. |
| `EGA.CPI`, `LCD.CPI`, `KEYBOARD.SYS` | Display and keyboard data files. |
| `GWBASIC.EXE` | BASIC interpreter; startup reaches its `Ok` prompt. |
| `LINK.EXE` | Object linker; startup/usage behavior tested. |
| `PRINTER.SYS`, `RAMDRIVE.SYS` | Config drivers; printer smoke and RAM-drive I/O tested. |
| `REPLACE.EXE` | Replace matching files; directory-tree behavior tested. |
| `SHARE.EXE` | File-sharing/locking support; load/no-crash behavior tested. |
| `TREE.COM`, `XCOPY.EXE` | Directory-tree listing/copying; host-verified tests cover both. |
| `FC.EXE` | File comparison; identical and differing files tested. |
| `MS330PP0.2` | Zero-length DOS package marker/data entry. |

The shell also provides internal commands that are not separate files:
`DIR`, `TYPE`, `COPY`, `DEL`, `REN`, `MD`, `CD`, `RD`, `CLS`, `VER`, `VOL`,
`DATE`, `TIME`, `SET`, `PATH`, `PROMPT`, `ECHO`, `PAUSE`, `REM`, `IF`, `FOR`,
`GOTO`, `SHIFT`, `CALL`, `ERRORLEVEL`, batch files, redirection, and pipes.

Attach Disk 2 as drive B: with:

```bash
python3 main.py --floppy DOS3_3_525/DISK01.IMG \
  --floppy-b DOS3_3_525/DISK02.IMG --gtk
```

Then use `DIR B:` or copy tools from `B:` to `A:`. FDISK additionally needs
an attached raw hard-disk image, such as `--hard-disk hd.img`. To create one
safely, use the guided command first:

```bash
python3 main.py --create-hard-disk hd.img --hard-disk-cylinders 306
python3 main.py --floppy DOS3_3_525/DISK01.IMG --hard-disk hd.img --gtk
```

Run `FDISK`, exit, relaunch the emulator, and then run `FORMAT C: /S`.

## Running DOS CPU benchmarks

An optional local copy of Landmark System Speed Test 6.00 is kept under
`DOS_tools/SPEED600/`. These historical benchmark files are not part of the
tracked repository; keep their original license and distribution terms. Map
the folder as drive B: and launch the benchmark from the GTK interface:

```bash
python3 main.py --dos --host-dir DOS_tools/SPEED600 --gtk
```

At the DOS prompt, run a short quiet test:

```dos
B:SPEED600 /B /NV /Q /05
```

`/B` skips the introductory screen, `/NV` disables the video test, `/Q`
silences the beeper, and `/05` exits after five seconds. A representative
run reached the following screen before returning to `A>`:

```text
CPU Type : Intel 80386DX
CPU Clock: 0.139 MHz
FPU Type : None
Video    : <Not tested>
```

These values are useful as a compatibility/run-through check, not as a
calibrated hardware score. The emulator provides a 16-bit real-mode CPU and
80x25 text VGA rendering, so Landmark's CPU identification, clock estimate,
and graphical speed bars are approximate or unavailable. The newer
`SPEEDSYS.EXE` benchmark in `DOS_tools/speedsys/` requires DOS 5+, a 386+,
VGA, and 4 MB of XMS, so it is not suitable for the bundled DOS 3.3 image.

## Testing

```bash
python3 -m pytest -q -m "not slow"      # fast tests (1389 tests, ~13s)
python3 -m pytest -q -m slow            # DOS boot/tool integration tests (84 tests)
python3 -m pytest -q                    # all 1473 tests
python3 -m pytest tests/test_shift_flags.py -q   # shift/XLAT/LAHF/REPE regression (21 tests)
python3 -m pytest tests/test_dos_boot.py -q -m slow  # DOS boot + commands
```

Coverage: CPU opcode dispatch and ModR/M decode (`test_cpu.py`, `test_cpu_gaps.py`),
shift-flag and XLAT segment-override semantics (`test_shift_flags.py`), LAHF/SAHF
correctness and REP-string CX=0 no-op behavior (`test_shift_flags.py`), BIOS
interrupt handlers (`test_bios.py`), FAT12/FAT16 BPB and cluster-chain parsing
(`test_fat12.py`, `test_fat16.py`), hardware devices (`test_hardware.py`, `test_keyboard.py`),
video rendering + scroll sync (`test_video.py`), end-to-end boot (`test_main.py`),
and real MS-DOS 3.3 boot + command execution (`test_dos_boot.py`, marked `slow`).

## Debugging & Tracing

The repo ships several purpose-built tools, developed while chasing the DOS-3.3
boot. The most powerful is the **differential tracer** (`diff_trace.py`), which
loads a saved CPU+memory snapshot into both this emulator's CPU and a
reference CPU (Unicorn / QEMU's TCG), single-steps them in lockstep, and reports
the first instruction where register/flag state diverges.

```bash
python3 snapshot_capture.py    # boots DOS, dumps 1MB + regs at a trigger point
python3 diff_trace.py          # my CPU vs Unicorn from that snapshot
```

`diff_trace.py` requires `unicorn` and `capstone` (both pip-installable); it
also requires `snapshot_capture.py` to have been run first to produce
`snapshot.bin` + `snapshot.regs`. The tracer routes INT 13h/10h/1Ah through
the Python BIOS handlers (so disk reads actually work), properly handles
REPE vs REPNE ZF conditions in Unicorn's REP loops, and propagates
undefined-flag state through flag-preserving instructions to avoid false
positives.

Diagnostic probes (one-shot, kept for future investigations):
- `trace_boot.py` — boot tracer with INT 13h/INT 10h call logging
- `trace_dos.py` — DOS-boot INT 21h/13h/2Fh call + return-value tracer
  (captures return values even for DOS-handled vectors via stack-return-site sniffing)
- `debug_dos.py` — DOS 3.3 boot debugger (INT 13h trace + BDA dump)
- `probe_ivt.py` / `probe_chain.py` / `probe_devchain.py` / `probe_devnames.py` /
  `probe_step.py` — IVT dumps, device-driver chain walker, single-step INT-handler
  tracers
- `probe_dos4_open.py` — DOS 4 boot diagnostic: traces the first failing path
  open into the kernel canonicalizer and dumps the CDS/DPB state
- `probe_dos6_diff.py` — DOS 6.22 boot diagnostic: single-step differential
  tracer (our CPU vs Unicorn) from a live SYSINIT EXEPACK-decoder state;
  stops at the first register/flag divergence
- `imd_decode.py` — ImageDisk (.IMD) floppy image decoder (raw output suitable
  for `--floppy`), used to unpack the Eagle DOS 2.0 reference disk

Smoke tests (require an X display):
- `check_gtk_smoke.py` — boots the sample boot sector under `--gtk`, grabs pixels
  from the DrawingArea, and verifies the POST banner was rendered
- `check_gtk_keys.py` — synthesises GDK key-press events and verifies the correct
  ASCII bytes (including 0x0D for Enter) reach the keyboard controller
- `check_interactive.py` — spawns the emulator in a pty, waits for the DATE prompt,
  types a date, and checks for the `A>` prompt

The differential methodology is portable: to chase a new corruption, capture the
state at the failing boundary with `snapshot_capture.py` (edit its trigger to
the interrupt of interest), then run `diff_trace.py` to localise the first
instruction-emulation divergence against a trusted reference.

## Limitations

- No protected mode support; no DMA emulation
- Two floppy drives max (A: and B: via `--floppy` / `--floppy-b`) plus one
  legacy C/4/17 hard disk (up to 1024 cylinders) at BIOS drive 80h. FDISK can create a maximum-size
  active primary partition; after relaunch, DOS discovers it as C: and
  `FORMAT C: /S` installs a bootable system plus normal file I/O. The MBR,
  partition filesystem, and fresh hard-disk boot to `C>` are verified.
  FDISK's requested warm restart is not emulated, so relaunch the emulator
  after partitioning. Extended partitions, multiple hard disks, and EDD/LBA
  BIOS services are not implemented.
- FAT12 and FAT16 **write** are supported (guest `COPY`/`DEL`/`REN`/`MKDIR`/`FORMAT`/`SYS`/
  `XCOPY` persist via INT 13h AH=03, and host-side `FAT12.write_file` /
  `FAT16.write_file` can
  inject fixtures, including `CONFIG.SYS` for driver boot-smoke tests).
  Use `--persist` to write the temp image back on exit.
- INT 20h program-terminate routes to the DOS-owned IVT entry (programs that
  exit via the old-style `INT 20h`, e.g. COMP.COM after its "Compare more files?"
  prompt, return to COMMAND.COM instead of halting the CPU).
- Redirection (`<`, `>`, `>>`) and COMMAND.COM pipelines (`|`) work, including
  multi-stage tool chains and output redirection on the final command; the
  tool tests also verify that temporary pipe files are removed.
- Full-disk `DISKCOPY` and `DISKCOMP` complete within the bounded tool-test
  budget. DISKCOPY is checked across all 720 sectors; DISKCOMP covers both
  identical and deliberately different images.
- Phase F differential hardening fixed EDLIN and REPLACE by
  decoding memory shift/rotate effective addresses only once; the old
  read-modify-write path consumed the displacement again on write and skipped
  into the next instruction. GWBASIC and PRINT were fixed by preserving the
  BIOS INT 1Ch callback stub instead of replacing its vector with `0000:0000`;
  GWBASIC also exposed and now guards the standard INT 10h cursor register ABI.
  BACKUP/RESTORE now have a host-verified single-file round-trip. The
  boot-loadable `CONFIG.SYS`
  drivers load to the `A>` prompt. Guest-installed INT 29h handlers are honored
  and ANSI.SYS clear/cursor/colour effects are verified directly in VGA text
  memory. RAMDRIVE registers `C:`; creating, listing, and reading a file there
  is covered end-to-end.
- Step-mode mnemonics are approximate (operand decoding is simplified)
- PIT channel 0 timing is wall-clock based at the classic ~18.2 Hz rate;
  heavily stalled/debugged sessions cap catch-up to four ticks per loop.
- CMOS RTC syncs with host time (no independent battery-backed clock)
- DOS DATE/TIME prompts accept typed input via the harness / `--interactive` / `--gtk`
- Undefined x86 flag bits (AF after INC, MUL/IMUL SF/ZF/PF) are masked out by
  the differential tracer — verified instruction-exact vs Unicorn on the
  checked-in snapshot (`tests/test_diff_smoke.py`)

See `PLAN.md` for the completed per-tool status matrix and the Phase F
differential-hardening workflow used to close tool regressions.

## Extending

- To add new instructions, add cases in `cpu.py::_dispatch()` and a regression
  test in `tests/test_cpu_gaps.py` or `tests/test_shift_flags.py`. For new
  arithmetic/shift/logic ops, set SF/ZF/PF/AF/CF/OF via the existing helpers
  (`_flags_add8`, `_flags_sub8`, `_flags_logic8`, ...).
- To add new BIOS interrupts, add handlers in `bios.py::handle_interrupt()`
  (and register them in `_register_handlers`).
- To fix a CPU-semantic bug found by differential tracing, run
  `python3 diff_trace.py` after editing `snapshot_capture.py`'s trigger to the
  boundary you suspect.
