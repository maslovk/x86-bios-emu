# Simple BIOS Emulator

A Python-based x86 real-mode CPU emulator with a minimal BIOS implementation, VGA text mode video, keyboard, and floppy disk emulation.

## Architecture

```
simple-bios/
├── cpu.py       # x86 real-mode CPU core + step debugger (1700+ lines)
├── video.py     # VGA 80x25 text + I/O ports + COM1 serial + keyboard + disk
├── hardware.py  # PIT (8254), PIC (8259A), CMOS RTC (MC146818), Keyboard (i8042)
├── fat12.py     # FAT12 filesystem reader (BPB, FAT, directory, cluster chains)
├── bios.py      # BIOS ROM (IVT, POST, INT 10h/11h/12h/13h/15h/16h/19h/1Ah/1Ch/20h/29h/2Bh)
├── main.py      # Emulator harness + sample boot sector + IRQ dispatch + floppy loader
└── tests/       # pytest suite (373 tests: CPU, BIOS, video, hardware, keyboard, FAT12, integration)
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
  - System: INT, IRET, CLI, STI, HLT, XLAT
  - Segment overrides: ES:/CS:/SS:/DS: prefixes (applied to next memory instruction)
  - BP-based addressing defaults to SS segment (per x86 spec)

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
cd simple-bios
python3 main.py                          # Run with built-in boot sector
python3 main.py --boot file.bin          # Load external boot sector (512 bytes)
python3 main.py --step                   # Step mode: trace each instruction
python3 main.py --interactive            # Interactive: read keys from stdin
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
| `--interactive` / `-i` | Read keyboard input from stdin (Ctrl+C to stop) |
| `--no-serial` | Disable COM1 serial port output |
| `--floppy IMG` / `-f` | Load floppy image (FAT12, auto-detects 360KB–1.44MB) and mount filesystem |

The emulator runs for ~1 second, displays the VGA screen, then exits with final CPU state.

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
| 5.25" | 360KB  | 0xF8       | 40/2/8              |
| 5.25" | 1.2MB  | 0xF0       | 80/2/15             |
| 3.5"  | 720KB  | 0xF1       | 40/2/9              |
| 3.5"  | 1.44MB | 0xF9       | 80/2/18             |

**Current DOS 3.3 status:** Boot sector loads and executes (100K+ instructions). Stalls in memory copy loop due to uninitialized BDA data structures. Requires additional BIOS data setup and INT 13h multi-sector read refinement for full boot.

## Limitations

- No protected mode support
- No DMA emulation
- Single floppy disk only (FAT12, auto-detects size)
- FAT12 read-only (no write support)
- Step mode mnemonics are approximate (operand decoding is simplified)
- PIT timing is instruction-count-based (not real-time)
- CMOS RTC syncs with host time (no independent battery-backed clock)

## Extending

To add new instructions, add cases in `cpu.py::_dispatch()`.
To add new BIOS interrupts, add handlers in `bios.py::handle_interrupt()`.
