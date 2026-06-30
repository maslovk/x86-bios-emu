# Simple BIOS Emulator

A Python-based x86 real-mode CPU emulator with a minimal BIOS implementation, VGA text mode video, keyboard, and floppy disk emulation.

## Architecture

```
x86-bios-emu/
├── cpu.py             # x86 real-mode CPU core + step debugger (2000+ lines)
├── bios.py            # BIOS ROM (IVT, POST, INT 10h–2Bh)
├── video.py           # VGA 80x25 text + I/O ports + COM1 serial + keyboard + disk
├── hardware.py        # PIT (8254), PIC (8259A), CMOS RTC (MC146818), Keyboard (i8042)
├── fat12.py           # FAT12 filesystem reader (BPB, FAT, directory, cluster chains)
├── main.py            # Emulator harness + sample boot sector + IRQ dispatch + floppy loader
├── gtdisplay.py        # Optional GTK window display (real keyboard capture, CGA colours)
├── trace_boot.py      # Boot tracer with INT 13h/INT 10h call logging
├── trace_dos.py       # DOS-boot INT 21h/13h/2Fh call + return-value tracer
├── debug_dos.py       # DOS 3.3 boot debugger (INT 13h trace + BDA dump)
├── snapshot_capture.py# Capture full CPU+1MB memory state at OPEN-CON for diff tracing
├── diff_trace.py      # Differential single-step tracer: my CPU vs Unicorn (QEMU)
├── probe_*.py         # IVT/device-chain/snapshot probes (one-shot diagnostics)
└── tests/             # pytest suite (449 tests: CPU, BIOS, video, hardware, keyboard, FAT12, shift flags, integration)
```

## Components

### CPU Core (`cpu.py`)
- Full x86 real-mode instruction decoder
- 16-bit registers: AX, BX, CX, DX, SP, BP, SI, DI
- Segment registers: CS, DS, ES, SS
- Flags: CF, PF, AF, ZF, SF, TF, IF, DF, OF
- Instruction support:
  - Data transfer: MOV (all forms), PUSH (reg/imm/memory), POP (reg/memory), XCHG, LES/LDS, MOV r/m,imm (C6/C7)
  - Arithmetic: ADD, SUB, INC, DEC, NEG, MUL, IMUL, DIV, IDIV, AAM, AAD, SALC
  - Logic: AND, OR, XOR, NOT, TEST, SHL, SHR, SAR, ROL, ROR
  - Control flow: JMP (near/far), JE/JZ, JNE/JNZ, JB/JAE, JL/JGE, JBE/JA, JO/JNO, JPE/JPO, CALL, RET, RETF, LOOP, JCXZ
  - String: MOVS[BW], CMPS[BW], STOS[BW], LODS[BW], SCAS[BW] (DF-aware, REP/REPNE)
  - Stack: PUSHA/POPA, ENTER/LEAVE
  - Flags: PUSHF/POPF, STC/CLD/STD/CMC, SETcc (all 16 conditions)
  - System: INT, IRET, CLI, STI, HLT, XLAT (honours segment-override prefix)
  - Segment overrides: ES:/CS:/SS:/DS: prefixes (applied to next memory instruction, including XLAT)
  - BP-based addressing defaults to SS segment (per x86 spec)
  - Shift/rotate flag semantics: scalar shifts (SHL/SHR/SAR/SAL) set SF/ZF/PF from the result and clear AF; rotates (ROL/ROR/RCL/RCR) only touch CF/OF, per Intel SDM

### BIOS ROM (`bios.py`)
- Interrupt Vector Table (IVT) initialization
- POST (Power-On Self-Test) with system info display
- Interrupt handlers:
  - **INT 08h**: IRQ 0 timer handler (BDA tick increment + INT 1Ch callback)
  - **INT 09h**: IRQ 1 keyboard handler (scan code → ASCII, EOI)
  - **INT 0Ah**: IRQ 2 cascade handler
  - **INT 10h**: Video services (AH=00h set mode, AH=13h write string)
  - **INT 11h**: Equipment list (returns 0x0410: 1 floppy, color VGA)
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
- Shift/Ctrl/Alt/CapsLock/NumLock/ScrollLock state tracking
- Scan code → ASCII translation with modifier application
- IRQ 1 generation on character available
- FIFO output buffer (multiple keys queued)
- LED control via 0xED command
- Self-test (0xAA), input port read (0xD0), command byte (0x20)

### FAT12 Filesystem (`fat12.py`)
- Full FAT12 parser for 1.44 MB floppy images
- BPB parsing: sector size, cluster size, FAT count, root entries
- FAT table: 12-bit packed entries, cluster chain following
- Root directory: 224 entries, 8.3 filename format
- File operations: find by name, read cluster chains, load to memory
- Extended INT 13h (AH=42h): LBA sector reads via Disk Access Packet (DAP)
- CLI: `--floppy image.img` loads and mounts FAT12 automatically

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
python3 main.py --floppy disk.img --gtk  # Boot DOS floppy in a window
python3 main.py --boot file.bin --step   # Combine flags
python3 main.py --floppy disk.img         # Load FAT12 floppy image (auto-detect size)
python3 main.py --boot dos3.3.img         # Load DOS 3.3 boot sector
python3 main.py --boot dos3.3.img --step  # Step through DOS 3.3 boot
```

### Options
| Flag | Description |
|------|-------------|
| `--boot FILE` / `-b` | Load boot sector from binary file (padded to 512 bytes) |
| `--step` / `-s` | Print mnemonic + full register state every instruction |
| `--interactive` / `-i` | Read keyboard input from stdin (Ctrl+C to stop; needs a TTY) |
| `--gtk` / `-g` | Open a GTK window rendering the 80x25 VGA grid with proper keyboard capture (recommended for interactive DOS use) |
| `--gtk-font-size PT` | Pango font point size for `--gtk` (default 18) |
| `--no-serial` | Disable COM1 serial port output |
| `--floppy IMG` / `-f` | Load floppy image (FAT12, auto-detects 360KB–1.44MB) and mount filesystem |

The emulator runs for ~1 second, displays the VGA screen, then exits with final CPU state.

## Display modes

The emulator supports two VGA output paths:

- **Terminal** (default) — renders the 80x25 grid as an aligned box-drawing
  frame with batched ANSI colour escapes.  ANSI is auto-disabled when stdout
  isn't a TTY so output stays readable in pipes/logs.  No keyboard capture.
- **GTK** (`--gtk` / `-g`) — opens a real `Gtk.DrawingArea` window, paints
  each cell's CGA background + foreground colour, and captures key presses
  directly (injecting ASCII bytes into the keyboard controller).  This is
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

**Current DOS 3.3 status:** Boots to the command interpreter.
Boot-sector relocation, IO.SYS relocation, MSDOS.SYS relocation, DOS kernel
initialisation and SYSINIT all run to completion. SYSINIT's standard-handle
opens (CON/AUX/NUL/PRN) and the COMMAND.COM open succeed, COMMAND.COM loads
and runs, and the emulator displays the familiar:

```
Current date is Mon  1-07-1980
Enter new date (mm-dd-yy):
```

To reach the interactive `A>` prompt, use the GTK display (recommended) and
serve the DATE/TIME prompts:

```bash
python3 main.py --floppy DOS3_3_525/DISK01.IMG --gtk
# When the 'Enter new date (mm-dd-yy):' prompt appears, type e.g. 01-01-1980
# and press Enter; same for 'Enter new time'.  The DOS A> prompt follows.
```

The terminal `--interactive` path also works but needs a real TTY (it puts
the terminal into cbreak mode); piped input has timing issues because the
keys arrive before COMMAND.COM's prompt is up.

This was unblocked by two CPU-emulation bugs found via a Unicorn (QEMU-based)
differential single-step trace against an identical OPEN-CON memory snapshot
(see `diff_trace.py`, `snapshot_capture.py`, `tests/test_shift_flags.py`):

1. **Scalar shift flag semantics** (`cpu.py::_do_shift`): SHL/SHR/SAR/SAL
   (D0-D3, reg 4/5/6/7) were only updating CF and OF, leaving SF/ZF/PF --
   and in the case of SHL-by-1 the parity flag specifically -- stale from
   the prior instruction. DOS's `MOV BL,AH; SHL BX,1; ...` after `XOR BH,BH`
   read the wrong PF/ZF and mis-dispatched the open. Fixed to set SF/ZF/PF
   (and clear AF) from the result, gated on count != 0; rotates left all
   arithmetic flags alone per the Intel SDM.

2. **XLAT segment-override prefix** (`cpu.py`, opcode 0xD7): XLAT was
   hard-coded to read from `DS:BX+AL` and ignored the segment-override
   prefix. A `CS: XLAT` (0x2E 0xD7) at `023E:5532` -- which looks up a byte
   in DOS's country-info table -- was reading from `DS:BX+AL` instead of
   `CS:BX+AL`, returning the wrong byte and corrupting every subsequent
   device/file open. Fixed to use `_default_data_seg()`.

The differential trace now runs 20,000+ instructions with zero divergence
between my CPU and Unicorn across the entire OPEN-CON local-qualify path.

## Testing

```bash
python3 -m pytest -q              # full suite (449 tests)
python3 -m pytest tests/test_cpu.py -q
python3 -m pytest tests/test_shift_flags.py -q   # shift/XLAT regression (10 tests)
python3 -m pytest tests/test_bios.py -q
python3 -m pytest tests/test_fat12.py -q
```

Coverage: CPU opcode dispatch and ModR/M decode (`test_cpu.py`, `test_cpu_gaps.py`),
shift-flag and XLAT segment-override semantics (`test_shift_flags.py`), BIOS
interrupt handlers (`test_bios.py`), FAT12 BPB/cluster-chain parsing
(`test_fat12.py`), hardware devices (`test_hardware.py`, `test_keyboard.py`),
video (`test_video.py`), and end-to-end boot (`test_main.py`).

## Debugging & Tracing

The repo ships several purpose-built tools, developed while chasing the DOS-3.3
boot. The most powerful is the **differential tracer** (`diff_trace.py`), which
loads a saved CPU+memory snapshot into both this emulator's CPU and a
reference CPU (Unicorn / QEMU's TCG), single-steps them in lockstep, and reports
the first instruction where register/flag state diverges.

```bash
python3 snapshot_capture.py    # boots DOS, dumps 1MB + regs at OPEN-CON
python3 diff_trace.py          # my CPU vs Unicorn from that snapshot
```

`diff_trace.py` requires `unicorn` and `capstone` (both pip-installable); it
also requires `snapshot_capture.py` to have been run first to produce
`snapshot.bin` + `snapshot.regs`.

Diagnostic probes (one-shot, kept for future investigations):
- `trace_boot.py` — boot tracer with INT 13h/INT 10h call logging
- `trace_dos.py` — DOS-boot INT 21h/13h/2Fh call + return-value tracer
  (captures return values even for DOS-handled vectors via stack-return-site sniffing)
- `debug_dos.py` — DOS 3.3 boot debugger (INT 13h trace + BDA dump)
- `probe_ivt.py` / `probe_chain.py` / `probe_devchain.py` / `probe_devnames.py` /
  `probe_step.py` — IVT dumps, device-driver chain walker, single-step INT-handler
  tracers

The differential methodology is portable: to chase a new corruption, capture the
state at the failing boundary with `snapshot_capture.py` (edit its trigger to
the interrupt of interest), then run `diff_trace.py` to localise the first
instruction-emulation divergence against a trusted reference.

## Limitations

- No protected mode support
- No DMA emulation
- Single floppy disk only (FAT12, auto-detects size)
- FAT12 read-only (no write support)
- Step mode mnemonics are approximate (operand decoding is simplified)
- PIT timing is instruction-count-based (not real-time), ~500 insns per PIT tick
- CMOS RTC syncs with host time (no independent battery-backed clock)
- DOS DATE/TIME prompts require `--interactive` with timed input to reach the `A>` prompt
- Undefined x86 flag bits (AF after INC, MUL/IMUL SF/ZF/PF) may differ from real hardware — the differential tracer masks these out since DOS never branches on them

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
