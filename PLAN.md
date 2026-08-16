# PLAN: Boot MS-DOS 3.3 and exercise every DOS tool

This is an execution plan for an LLM agent working in this repository. Each
phase lists **features** (with exact files and functions to change), **tests**
(exact test files and assertions), and **acceptance criteria** that can be
verified by running commands. Work through phases in order — later phases
depend on earlier ones. After every phase: `python3 -m pytest -q` must stay
green (including `-m slow`), and nothing in `DOS3_3_525/` may be modified
(tests always work on temp copies of the images).

## 0. Current state (verified 2026-08-16)

Phases A–J are complete and Phase K is underway. MS-DOS 3.3 boots from the
shipped floppy images or from a prepared hard-disk image,
the full internal/external tool matrix is covered, writable workflows operate
on temporary images, and the shipped images are protected by a session-level
hash guard. The suite now contains 1,451 tests (1,371 fast and 80 slow); the
Phase K CLI slice passes the complete fast suite with no xfails.

The original blocking gaps are resolved: writable and multi-drive INT 13h,
FAT12 mutation support, reusable DOS harness fixtures, decimal-adjust and trap
CPU semantics, safe FPU escape decoding, image persistence/copy-on-write, and
Ctrl-C/Ctrl-Break injection all have implementation and regression coverage.

## 1. Tool inventory and target tiers

From the root directories of the two shipped images:

**DISK01.IMG**: 4201.CPI 5202.CPI ANSI.SYS APPEND.EXE ASSIGN.COM ATTRIB.EXE
CHKDSK.COM COMMAND.COM COMP.COM COUNTRY.SYS DISKCOMP.COM DISKCOPY.COM
DISPLAY.SYS DRIVER.SYS EDLIN.COM EXE2BIN.EXE FASTOPEN.EXE FDISK.COM FIND.EXE
FORMAT.COM GRAFTABL.COM GRAPHICS.COM IO.SYS JOIN.EXE KEYB.COM LABEL.COM
MODE.COM MORE.COM MS330PP0.1 MSDOS.SYS NLSFUNC.EXE PRINT.COM RECOVER.COM
SELECT.COM SORT.EXE SUBST.EXE SYS.COM

**DISK02.IMG**: BACKUP.COM DEBUG.COM EGA.CPI FC.EXE GWBASIC.EXE KEYBOARD.SYS
LCD.CPI LINK.EXE MS330PP0.2 PRINTER.SYS RAMDRIVE.SYS REPLACE.COM RESTORE.COM
SHARE.EXE TREE.COM XCOPY.EXE

Classification (a tool is "passing" when its test in `tests/tools/` is green):

- **Tier 1 — must fully work** (needs phases 2–4):
  - Internal commands: DIR TYPE COPY DEL REN MD CD RD CLS VER VOL DATE TIME
    SET PATH PROMPT ECHO PAUSE REM VERIFY BREAK, batch files
    (IF/FOR/GOTO/SHIFT/CALL via `%0`-style args), ERRORLEVEL.
  - External: ATTRIB CHKDSK COMP FIND SORT MORE TREE FC LABEL VOL EDLIN
    DEBUG EXE2BIN XCOPY REPLACE SYS FORMAT DISKCOMP DISKCOPY.
- **Tier 2 — should run without crashing, core function verified**:
  APPEND ASSIGN SUBST JOIN FASTOPEN PRINT MODE SHARE RECOVER BACKUP RESTORE
  GWBASIC LINK GRAFTABL; FDISK creates an active primary partition when a
  temporary hard disk is attached.
- **Tier 3 — out of scope, must fail *gracefully* (clean error, no emulator
  crash/hang)**: KEYB/NLSFUNC/SELECT/DISPLAY/GRAPHICS
  (codepage/printer hardware), ANSI.SYS/DRIVER.SYS/RAMDRIVE.SYS/PRINTER.SYS
  as CONFIG.SYS drivers get one boot-smoke test each.

Track status in a table at the bottom of this file; update it as tests land.

---

## Phase A — Test infrastructure (do this first)

### Features

1. **Extract the harness**: move `DOSHarness` from `tests/test_dos_boot.py`
   into a new `dosharness.py` at repo root (importable by tests and by ad-hoc
   debugging scripts). Keep the existing API (`boot_to_prompt`,
   `run_command`, `vga_str`, `inject_string`, `wait_for`) and add:
   - `DOSHarness(image_path, image_b=None, writable=False)` — always copies
     the image(s) to a temp dir when `writable=True`; never touches repo
     images.
   - `run_command(cmd)` returns a `CommandResult` with `.screen` (final VGA
     text), `.output` (scrollback captured from a video-scroll hook — add a
     `Video.on_scroll_line` callback in `video.py` that the harness uses to
     accumulate lines scrolled off the top; without this, output longer than
     25 lines is unverifiable), and `.errorlevel` (see next item).
   - `errorlevel()` — implemented by typing
     `IF ERRORLEVEL 1 ECHO XX_FAIL_XX` + `IF NOT ERRORLEVEL 1 ECHO XX_OK_XX`
     and scraping the marker. This is how tests distinguish "tool printed
     something" from "tool succeeded".
   - `swap_disk(path)` — replaces the in-memory sector list of drive A (for
     single-drive DISKCOPY prompts), raising the media-change flag added in
     Phase C.
2. **Session-scoped boot**: booting takes ~3–5 s of host time. Add a pytest
   fixture in `tests/tools/conftest.py`:
   - `dos` (module-scoped): boots DISK01 once, reused by read-only tests.
   - `dos_rw` (function-scoped): fresh boot on a temp copy, for tests that
     write. Mark every test `@pytest.mark.slow` and add a new marker
     `tools` in `pytest.ini`.
3. **Fixture-file injection** requires host-side FAT12 write (Phase B
   feature 1). Until then, tests that need input files create them *inside
   DOS* via `COPY CON file.txt` (the harness gets a helper
   `create_file(name, text)` that types `COPY CON name`, the text, then
   Ctrl-Z + Enter — Ctrl-Z is ASCII 0x1A; verify `KeyboardController.
   _ascii_to_scan` maps control chars, extend it if not).
4. **Watchdog**: `run_command` must accept `timeout_steps` and return with
   `.timed_out = True` instead of hanging pytest, and capture the screen at
   timeout for the failure message.

### Tests

- `tests/tools/test_harness.py`:
  - `test_boot_to_prompt_disk01` — screen contains `A>`.
  - `test_scrollback_capture` — `DIR` output (>25 lines with `/-P` off? use
    `TYPE` of a long generated file) appears fully in `.output`.
  - `test_errorlevel_zero_and_nonzero` — `VER` → errorlevel 0; running a
    nonexistent command name still yields a prompt and errorlevel probe works.
  - `test_create_file_copy_con` — `create_file('T.TXT','hello')` then
    `TYPE T.TXT` shows `hello`.
  - `test_repo_images_untouched` — sha256 of `DOS3_3_525/*.IMG` unchanged
    after a `dos_rw` session (also enforce via a session-end autouse fixture).

### Acceptance

`python3 -m pytest tests/tools/test_harness.py -q -m slow` green;
`python3 -m pytest -q` (fast suite) still 477+ passing and still <30 s.

---

## Phase B — Real disk writes end-to-end

### Features

1. **`fat12.py` write support** (host side, for tests and file injection):
   - `FAT12.write_file(name, data)` — allocate clusters (first-fit from FAT),
     write both FAT copies, add/replace a root-directory entry (8.3 name,
     date/time, size), write data sectors via `disk.write_sector`.
   - `FAT12.delete_file(name)`, `FAT12.read_file_by_name(name)` convenience.
   - Only root directory support is required (fixtures don't need subdirs;
     guest-created subdirectories are read via `read_dir(cluster)` — add it,
     it's the same parser as `read_root_directory` over a cluster chain).
2. **Fix INT 13h AH=03** (`bios.py::_int13h`): mirror the AH=02 CHS→LBA code
   (factor the shared CHS decode + geometry lookup into a helper
   `_chs_to_lba(cpu)` — the copy in AH=02 is already duplicated in AH=0E),
   read `cpu.al` sectors from ES:BX guest memory, `disk.write_sector` each.
   Set AH=error 04h/CF=1 on out-of-range LBA, AL=sectors written.
3. **Correct INT 13h subfunction map** (`bios.py:547-568`):
   - AH=04 → Verify sectors (decode CHS, check range, CF=0, AL=count).
   - AH=05 → Format track: decode the ES:BX address-field table (4 bytes per
     sector: C,H,R,N), zero-fill each addressed sector via `write_sector`.
     This plus AH=17h/18h below is what FORMAT.COM needs.
   - AH=15h → Get disk type (AL=... floppy with change-line = 2), move the
     current AH=0D logic here; make AH=0D return CF=1/AH=01 (invalid).
   - AH=16h → Media change status (returns the flag set by
     `DOSHarness.swap_disk`, then clears it).
   - AH=17h/AH=18h → Set media type for format: accept, store geometry,
     CF=0; AH=18h returns ES:DI → diskette parameter table (reuse the one
     from `_setup_diskette_tables`).
4. **Persistence** (`main.py`): `--floppy` gains a `--persist` flag — on
   clean exit, write `disk.sectors` back to the image path. Default off.
   The `Disk` object grows `dirty: bool` set by `write_sector`.
5. **Two-sided truth check**: after the guest writes, host-side
   `FAT12(disk).mount()` must parse what DOS wrote. Any divergence is a bug
   in one of the two FAT implementations — debug with `diff_trace.py` only
   if it's CPU-level; otherwise dump both FATs.

### Tests

- `tests/test_fat12_write.py` (fast, no DOS boot): write/read-back roundtrip,
  2-cluster file chain correctness, FAT mirror equality, directory entry
  fields, delete + re-create reuses clusters, disk-full → `FAT12Error`.
- `tests/test_bios_disk_write.py` (fast): raw INT 13h AH=03 via a stub CPU —
  write 2 sectors at C/H/S (1,1,2), read back via AH=02, compare; AH=04
  verify OK + out-of-range error; AH=05 formats 9 sectors; AH=15h/16h/18h
  register contracts.
- `tests/tools/test_file_io.py` (slow, `dos_rw`):
  - `COPY CON A.TXT` → `TYPE A.TXT` roundtrip (already needed by harness).
  - `COPY A.TXT B.TXT` → `TYPE B.TXT` matches; **host-side check**: after the
    test, mount the temp image with `FAT12` and assert `B.TXT` content.
  - `DEL`, `REN`, `MD SUB` + `CD SUB` + file create in subdir + `RD` refusal
    while non-empty, success when empty.
  - `EDLIN NEW.TXT` — insert two lines (`1i`, text, Ctrl-C, `E`), verify file
    host-side. EDLIN's Ctrl-C insert terminator works through the exact-byte
    keyboard path; Phase F fixed the memory-shift RMW decoder bug that had
    skipped into the following instruction before termination was reached.

### Acceptance

`COPY`, `DEL`, `REN`, `MD/CD/RD` tests green; host-side FAT12 mount of the
written image agrees with what DOS shows in `DIR`.

---

## Phase C — Second drive and disk-to-disk tools

### Features

1. **Drive B:** `main.py::Emulator(..., floppy_b=None)`; `Disk` instances
   held as `self.disks = {0x00: disk_a, 0x01: disk_b}`. `bios.py::_int13h`
   dispatches on DL (0x00/0x01); DL≥2 or missing B: → CF=1, AH=01.
   Equipment word (`_int11h` and BDA 0x410) reports 2 drives when B: present
   (bits 6-7 = 01). CLI: `--floppy-b DOS3_3_525/DISK02.IMG`.
2. **Media change plumbing**: `swap_disk` (Phase A) sets
   `disk.media_changed = True`; INT 13h AH=16h reports and clears it. DOS 3.3
   uses this to invalidate its buffers after "Insert diskette ... press any
   key".
3. **Blank-image factory**: `fat12.py` gains
   `make_blank_image(path, size=360*1024)` writing a valid unformatted (or
   formatted-empty) image — needed as FORMAT's victim and DISKCOPY's target.

### Tests

- `tests/test_two_drives.py` (fast): INT 13h AH=02 with DL=1 reads drive B
  sectors; DL=1 with no B: sets CF; equipment word bits.
- `tests/tools/test_drive_b.py` (slow, boot A=DISK01, B=temp copy of DISK02):
  - `DIR B:` lists DEBUG.COM, GWBASIC.EXE.
  - `COPY B:TREE.COM A:` then `TREE` runs from A:.
  - `B:` prompt changes to `B>`; `A:` returns.
- `tests/tools/test_disk_tools.py` (slow):
  - `DISKCOMP A: B:` on identical temp images → "Compare OK"; on differing →
    reports differences, errorlevel 1.
  - `DISKCOPY A: B:` from DISK01 copy to blank 360K image → afterwards
    host-side `FAT12` mount of B shows the same root listing as A;
    `DISKCOMP A: B:` → OK.
  - `FORMAT B:` on a blank image → answer `Y`/volume-label prompts via
    harness; afterwards host-side mount succeeds and shows 0 files;
    "bytes available" line matches 362496-ish for 360K.
  - `SYS B:` after FORMAT (needs FORMAT /S or SYS) → host-side mount shows
    IO.SYS/MSDOS.SYS first two directory entries; copy COMMAND.COM and boot a
    second emulator from the resulting B image to the `A>` prompt.
  - `XCOPY A:*.COM B:` → host-side count of `.COM` files matches source.
  - `BACKUP A:*.TXT B:` / `RESTORE B: A:*.TXT` roundtrip (Tier 2 — content
    check only, accept its control-file format as opaque).

### Acceptance

DISKCOPY→DISKCOMP→FORMAT→SYS chain green on temp images; repo images'
hashes unchanged (Phase A guard test).

---

## Phase D — CPU + keyboard completeness for the tool suite

### Features

1. **Implement BCD ops** (`cpu.py:925`): real DAA, DAS, AAA, AAS with
   correct AL/AH, CF/AF (and SDM-defined ZF/SF/PF for DAA/DAS). Delete the
   skip. Cross-check semantics with the differential tracer rather than
   trusting memory: write a tiny snapshot that executes each op over a table
   of AL/AF/CF combinations and run `diff_trace.py` against Unicorn.
2. **Control-char keyboard injection** (`hardware.py`): extend
   `_ascii_to_scan` so Ctrl-C (0x03), Ctrl-Z (0x1A), Ctrl-S, Ctrl-Break, ESC
   inject correct scan-code sequences with a synthetic Ctrl make/break around
   them; BIOS `_irq1_keyboard` must translate Ctrl+letter to ASCII 1–26, and
   Ctrl-Break must invoke INT 1Bh and stuff 0x0000 into the key buffer.
3. **Trap flag single-step** (`cpu.py::execute`): if TF was set *before* the
   instruction (latch it at fetch), raise `_do_interrupt(1)` after the
   instruction completes (except after the instruction that set TF via
   POPF/IRET — implement the one-instruction delay). INT/INTO/IRET clear TF
   as already done. This enables DEBUG `T` and `P`.
4. **FPU escape correctness check** (`cpu.py:1603`): confirm `_skip_disp` is
   called so D8–DF consume the full ModR/M+displacement (it is — just add a
   regression test), and optionally set a "no 8087" status so WAIT (0x9B) is
   a no-op (verify it is).
5. Any opcode DEBUG/GWBASIC/LINK trip over: when a tool halts the CPU with
   `[UNKNOWN OPCODE]` (`cpu.py:1970`), capture a snapshot at that CS:IP with
   `snapshot_capture.py` and add the opcode with a `tests/test_cpu_gaps.py`
   regression, per the README "Extending" recipe. Budget expectation: a few
   stragglers (e.g. 0x60–0x6F 186+ ops if any tool uses them — DOS 3.3 era
   tools are 8086-clean, so treat 186+ opcodes as out of scope unless hit).

### Tests

- `tests/test_bcd.py` (fast): DAA/DAS/AAA/AAS truth tables (the classic
  16-case AF/CF matrix each), plus AAM/AAD regression alongside.
- `tests/test_trap_flag.py` (fast): PUSHF/POP-set TF → next instruction
  executes then vectors through IVT 1; POPF that sets TF delays one
  instruction; INT clears TF in the handler.
- `tests/test_keyboard.py` additions: Ctrl-C/Ctrl-Z/Ctrl-Break scan-code
  sequences produce the right BIOS buffer contents / INT 1Bh call.
- `tests/tools/test_debug_tool.py` (slow, B: = DISK02 copy):
  - `B:DEBUG` → `-` prompt appears.
  - `-D 100 10F` dumps 16 bytes; `-E 100 41 42` then `-D 100 101` shows
    `41 42`.
  - `-A 100` / `MOV AX,1234` / blank / `-T` → register dump shows AX=1234
    (exercises the assembler *and* TF single-step).
  - `-R` shows registers; `-Q` exits to `A>`.
  - `-N`+`-W` writes a .COM file; host-side FAT12 check.
- `tests/tools/test_gwbasic.py` (slow, Tier 2): `B:GWBASIC` → "Ok" prompt;
  `PRINT 2+2` → ` 4`; `10 PRINT "HI"` / `RUN` → HI; `SAVE "P"` +
  host-side file exists; `SYSTEM` returns to `A>`.

### Acceptance

DEBUG assemble/trace/write test green — this is the single most demanding
consumer of CPU correctness in the suite.

---

## Phase E — Full tool sweep

### Features

Mostly none — this phase is writing tests against what Phases A–D built, and
fixing whatever falls out. Known small features:

1. `PRINT` needs INT 17h to report printer ready (exists at `bios.py:694` —
   verify status bits) and a way to observe output: give the BIOS printer
   handler an `output: list` the harness can read.
2. `MODE COM1:96,N,8,1` exercises INT 14h AH=00 (DOS 3.3 expresses the
   baud rate in hundreds) — just test it.
3. `SHARE`/`FASTOPEN`/`APPEND` are TSRs: test = loads without crash, prompt
   returns, and a follow-up `DIR` still works (memory not corrupted).
4. `CTTY COM1` + serial: redirect console via `Serial`, execute a command over
   COM1, then switch back with `CTTY CON`; the watchdog ensures it cannot hang.

### Tests — one file per tool family in `tests/tools/`

| File | Commands covered | Core assertions |
|------|------------------|-----------------|
| `test_internal_basic.py` | VER VOL DATE TIME CLS SET PATH PROMPT ECHO | banner text, settable+queryable state, CLS empties screen |
| `test_internal_files.py` | DIR TYPE COPY DEL REN (from Phase B) | + wildcards: `COPY *.TXT`, `DEL *.BAK`, `DIR *.COM /W` |
| `test_batch.py` | batch: ECHO OFF, REM, PAUSE, IF, FOR, GOTO, SHIFT, CALL args, ERRORLEVEL | write .BAT via `create_file`, run, assert flow (e.g. FOR %%F IN loop output) |
| `test_attrib_label.py` | ATTRIB +R/-R, LABEL, VOL | +R makes DEL fail; label round-trips and shows in DIR header |
| `test_chkdsk.py` | CHKDSK, CHKDSK /F on clean disk | reported total/free bytes match host-side FAT12 math exactly |
| `test_text_tools.py` | FIND SORT MORE COMP FC, pipelines | FIND "x" counts lines; SORT reorders; multi-stage pipes filter/sort and clean up temp files; MORE paginates (send space); COMP/FC identical vs differing files, errorlevel |
| `test_tree_xcopy_replace.py` | TREE XCOPY REPLACE | build subdir tree in DOS, TREE output matches; XCOPY /S copies tree (host-verified); REPLACE updates only existing |
| `test_edlin.py` | EDLIN insert/list/edit/delete lines | file content host-verified (needs Ctrl-C from Phase D) |
| `test_exe2bin_link.py` | EXE2BIN, LINK (Tier 2) | DEBUG-assembled trivial .EXE? Simpler: EXE2BIN on a fixture .EXE injected host-side; LINK with no input exits with usage, errorlevel ≠ crash |
| `test_disk_tools.py` | FORMAT SYS DISKCOPY DISKCOMP RECOVER (from Phase C) | as Phase C |
| `test_tsr_and_devices.py` | SHARE FASTOPEN APPEND ASSIGN SUBST JOIN PRINT MODE GRAFTABL | load-without-crash + one functional probe each (e.g. `SUBST Z: A:\` then `DIR Z:`) |
| `test_debug_tool.py`, `test_gwbasic.py` | (from Phase D) | |
| `test_config_sys.py` | ANSI.SYS DRIVER.SYS RAMDRIVE.SYS via injected CONFIG.SYS | boots to `A>` with driver loaded; ANSI: `PROMPT $e[7m$p$g` changes attribute bytes in VRAM; RAMDRIVE: `DIR C:` works if INT 15h ext-mem path suffices, else graceful-fail assertion |
| `test_tier3_graceful.py` | FDISK KEYB NLSFUNC SELECT DISPLAY GRAPHICS | each: runs, prints an error or exits ≤ N steps, `A>` prompt returns, next command still works |

Every slow test: `@pytest.mark.slow`, `@pytest.mark.tools`, hard step-limit
watchdog, and on failure print `.screen` + last 50 scrollback lines.

### Acceptance

- `python3 -m pytest -q -m "slow and tools"` green.
- Status table below fully filled in.
- README updated: limitations list rewritten (FAT12 write now supported,
  drive B:, tool matrix linked to this file).

---

## Phase F — Differential hardening (continuous, not last)

Whenever a tool misbehaves *without* crashing the CPU:

1. Reproduce under `tests/tools/`, mark `xfail(strict=True)` with a comment.
2. Edit `snapshot_capture.py`'s trigger to the failing INT boundary, run
   `python3 snapshot_capture.py && python3 diff_trace.py`.
3. If Unicorn diverges → fix `cpu.py`, add a fast regression to
   `tests/test_cpu_gaps.py`, flip the xfail.
4. If no divergence in 20k+ instructions → the bug is in `bios.py` device
   semantics; instrument the specific INT 13h/16h/21h path with `trace_dos.py`.

Also add one new *fast* CI-able diff check:
`tests/test_diff_smoke.py` — if `unicorn` is importable, run 5,000 lockstep
instructions from the checked-in snapshot trigger and assert zero
divergences; `pytest.skip` otherwise.

---

## Phase G — Legacy hard disk and FDISK

### Features

1. `video.py::Disk` accepts a variable sector count and optional legacy CHS
   geometry while preserving the existing 1.44MB floppy defaults.
2. `main.py::Emulator` and `DOSHarness` accept a raw hard-disk image, expose it
   as BIOS drive `80h`, and only write it back when `--persist` is explicit.
   The supported geometry is C/4/17 with an exact whole-cylinder image size;
   the integration test uses 306 cylinders (10,653,696 bytes, about 10MB).
3. INT 13h routes `DL=80h` without aliasing it to floppy A:, implements legacy
   CHS read/write/verify, reports geometry through AH=08h, reports hard-disk
   type and total sectors through AH=15h, and publishes one fixed disk at BDA
   `0040:0075`.

### Tests

- `tests/test_hard_disk.py` (fast): BDA count, AH=08h geometry, AH=15h sector
  count, CHS read/write isolation from floppy A:, exact image loading, and
  opt-in persistence.
- `tests/tools/test_fdisk.py` (slow): attach a private blank 306/4/17 image,
  use FDISK to create the maximum-size active primary partition, and verify
  the MBR signature, partition type, LBA bounds, unused entries, and source
  image immutability host-side.

### Acceptance

FDISK reaches its normal restart prompt after writing a valid active FAT12
partition spanning sectors 17–20807; the full suite stays green and shipped
floppy hashes remain unchanged.

---

## Phase H — DOS drive C: and fixed-disk filesystem

### Features

1. `video.py::DiskView` provides a bounded, sector-offset view into a parent
   disk so host filesystem tools can mount a partition without copying it;
   writes propagate through the parent and preserve its dirty tracking.
2. A fresh floppy boot discovers the Phase G active partition as drive C:.
   DOS `FORMAT C:` creates its FAT12 BPB/FAT/root layout, after which normal
   DOS file commands operate on the fixed disk.

### Tests

- `tests/test_hard_disk.py` (fast): partition-view offset I/O and bounds.
- `tests/tools/test_hard_disk_filesystem.py` (slow): boot with the Phase G MBR
  layout, verify C: visibility, complete fixed-disk FORMAT, write/read
  `C:\HELLO.TXT` in DOS, mount the partition at LBA 17 with host FAT12, and
  prove the original source image was not modified.

### Acceptance

`DIR C:`, `FORMAT C:`, guest file write/read, and host-side FAT12 verification
all agree on a private 10MB image.

---

## Phase I — Boot MS-DOS from the hard disk

### Features

1. BIOS INT 19h, `main.py::Emulator`, and `DOSHarness` accept an explicit
   boot drive. Drive `80h` loads the hard-disk MBR at `0000:7C00`, publishes
   `80h` in the BIOS Data Area, and preserves DL=`80h` for MBR/partition boot
   code. Floppy A: remains the default.
2. CLI `--boot-hard-disk` selects the attached `--hard-disk` image. DOS 3.3
   `FORMAT C: /S` installs IO.SYS, MSDOS.SYS, and COMMAND.COM, producing a
   directly bootable system partition.

### Tests

- `tests/test_hard_disk.py` (fast): INT 19h loads drive 80h, sets BDA boot
  drive and DL, validates the signature, and transfers to `0000:7C00`.
- `tests/tools/test_hard_disk_boot.py` (slow): create an MBR with FDISK,
  relaunch from floppy, run `FORMAT C: /S`, write a marker file, then start a
  fresh emulator at drive 80h and verify the `C>` prompt and marker contents.

### Acceptance

The complete FDISK → FORMAT /S → MBR → partition boot chain reaches a
working MS-DOS `C>` prompt on an isolated image.

---

## Phase J — FAT16 and larger hard disks

### Features

1. The existing variable-cylinder C/4/17 hard-disk path is validated at
   615/4/17 (41,820 sectors, 21,411,840 bytes), beyond DOS 3.3's FAT12 size
   range. No BIOS geometry change is needed; legacy CHS already supports the
   larger image.
2. `fat12.py::FAT16` reuses the DOS BPB, root-directory, cluster I/O, and
   host mutation APIs while implementing 16-bit FAT entries, FAT16 cluster
   range validation, mirrored FAT writes, allocation, and free-space handling.
3. DOS FDISK selects partition type `04h`, FORMAT creates a FAT16 filesystem,
   and `FORMAT C: /S` remains directly bootable through the MBR.

### Tests

- `tests/test_fat16.py` (fast): synthetic FAT16 BPB and multi-cluster reads,
  last-cluster bounds, host writes through a `DiskView`, FAT mirror agreement,
  fresh-mount round-trip, and rejection of FAT12-sized volumes.
- `tests/tools/test_fat16_hard_disk.py` (slow): FDISK a private 615/4/17 disk,
  assert its active `04h` partition, FORMAT it with system files, verify its
  BPB and marker through host FAT16, then fresh-boot from drive `80h` and read
  the marker at `C>`.

### Acceptance

The complete FDISK → FAT16 FORMAT /S → host verification → MBR boot workflow
passes on an isolated 20 MB image, while the 10 MB FAT12 workflows remain
green.

---

## Phase K — User experience

This phase improves the path from cloning the repository to comfortably using
the emulator without changing guest compatibility.

### Slice 1 — Guided launch and actionable CLI errors (complete)

1. `python3 main.py --dos` selects the bundled MS-DOS 3.3 disk by an absolute
   repository-relative path and enables terminal input. `--dos --gtk` is the
   corresponding one-command graphical launch. Bundled media remains
   protected: `--dos --persist` is rejected with instructions to copy the
   image and use `--floppy` instead.
2. CLI help is grouped by boot media, display/input, and runtime behavior, and
   includes working launch examples. Missing files, invalid GTK font sizes,
   mutually exclusive media, and hard-disk boot without an attached image fail
   before startup with concise argparse errors instead of tracebacks.
3. The documented `--no-serial` option now suppresses host COM1 echo while
   preserving the emulated serial device and its captured output.
4. A native Python launched from a Snap-packaged editor now discards only the
   inherited Snap GTK/GIO library settings before loading PyGObject, avoiding
   mixed-runtime linker failures. Python interpreters genuinely running inside
   the Snap and normal non-Snap environments are left unchanged.
5. Closing a normal GTK session prints one concise stop summary. Register and
   memory diagnostics are reserved for explicit `--step` sessions.
6. GTK rendering is capped at roughly 30 frames per second instead of drawing
   the full 80x25 Pango grid after nearly every guest instruction. BIOS
   teletype output now correctly treats BH as a display-page number and retains
   the existing text attribute, so DOS prompts are visible instead of being
   rendered black-on-black.
7. The BIOS positions the cursor at the start of the next line after its boot
   status, keeping DOS's date message separate. Interactive terminal and GTK
   sessions no longer stop at the non-interactive 10-million-instruction
   safety limit.

### Tests

- `tests/test_cli.py` (fast): bundled-DOS normalization and protection, GTK
  launch selection, grouped help/examples, invalid-option diagnostics, missing
  image reporting, and functional serial-output suppression.
  It also covers native/Snap GTK environment isolation without importing GTK.
- `tests/test_bios.py` (fast) verifies INT 10h AH=0Eh page/attribute semantics;
  `tests/test_dos_boot.py` confirms the real DOS date prompt has visible
  foreground attributes before continuing the normal boot workflow.

### Next UX slices

- ✅ Guided hard-disk image creation: `--create-hard-disk IMG` creates an exact
  legacy C/4/17 image (configurable with `--hard-disk-cylinders`), refuses
  accidental overwrite, and prints the FDISK/FORMAT next steps.
- ✅ Clearer run-state/persistence summaries and clean-exit reporting in the
  terminal and GTK session indicator.
- ✅ GTK quality-of-life controls: soft reset, live media status, and clipboard
  paste (buttons plus Ctrl+R/Ctrl+V shortcuts).
- ✅ Dirty-media warnings and true guest restart: modified A:/B:/C: media is
  marked in GTK, non-persistent shutdowns warn before discarding writes, and
  Reset reinitializes memory, CPU, BIOS, IRQ state, and boot execution.

## Phase L — Host-folder FAT bridge (Milestone 1 in progress)

Goal: make a host directory visible to DOS without manually editing floppy
images. The bridge will be a virtual read-only FAT drive, not a direct host
filesystem syscall path from guest code.

### Milestone 1 — Read-only root directory

1. Add `--host-dir PATH` and `--host-drive B|C` (default `B`) to the CLI.
2. Build an in-memory FAT12 image from the directory using the existing
   `Disk`/`FAT12` abstractions: BPB, mirrored FATs, root entries, and data.
3. Expose it through normal INT 13h so DOS can use `DIR`, `TYPE`, and execute
   `.COM`/`.EXE` files without special DOS hooks.
4. Support regular files and recursive directories with deterministic 8.3
   names; reject collisions, symlinks, and files that exceed the image.
5. Keep it read-only: guest writes return a protected-media error and never
   modify the host directory. `--persist` is incompatible with this bridge.

### Milestone 2 — Usable workflow

- Show the mapped host path in the GTK/terminal media status.
- ✅ Add a GTK Refresh B: control that rebuilds the virtual image between
- commands, plus Eject B:; Refresh B: reinserts the mapped host folder.
- ✅ Add recursive read-only directories after root-file semantics are tested.
- Document `python3 main.py --dos --host-dir ./dos-files --gtk`.

### Milestone 3 — Optional write-back (complete)

Host writes require explicit opt-in, temporary-file staging, 8.3 collision
handling, and close-time review of every changed file. The initial explicit
`--host-dir-write --persist` path now stages regular files and directories;
Deletion is separately opt-in with `--host-dir-delete`; persistence reports
every created/updated/deleted path and never deletes silently. Host files
changed independently since session start are detected and skipped as
conflicts.

### Acceptance tests

- Fixture files appear through `DIR B:` and `TYPE B:FILE.TXT`.
- A host `.COM` fixture executes from the mapped drive.
- Long names, symlinks, collisions, oversize files, and traversal attempts are
  rejected before boot with concise CLI errors.
- Guest write attempts fail without changing host files.
- Existing floppy/hard-disk tests and the shipped-image hash guard stay green.

The read/list criteria are covered by the slow
`test_host_folder_bridge_is_visible_as_drive_b` integration test, and the
`.COM` execution criterion by `test_host_folder_bridge_executes_com_program`.
The read-only criterion is covered by
`test_host_folder_bridge_rejects_guest_writes`.
Explicit write-back is covered by
`test_host_folder_bridge_writes_back_guest_file`.

---

## Ground rules for the executing agent

- Never modify `DOS3_3_525/*.IMG`; the Phase A hash-guard test enforces this.
- Never weaken an existing test to make a new feature pass.
- Keep the fast suite (`-m "not slow"`) under 30 s; anything that boots DOS
  is `slow`.
- Fix CPU semantics only with SDM-correct behavior verified against Unicorn
  (`diff_trace.py`), not by patching around a symptom.
- One phase per commit series; commit messages in the existing style
  (`cpu: ...`, `bios: ...`, `tests: ...`).
- Update the status table below in the same commit as each tool's test.

## Status table

| Tool | Tier | Test | Status |
|------|------|------|--------|
| DIR/TYPE/VER/... (internal, read-only) | 1 | test_internal_basic.py | ✅ Phase E (VER/VOL/CLS/ECHO/SET/PATH/DATE/TIME) |
| COPY/DEL/REN/MD/CD/RD | 1 | test_file_io.py (core) | ✅ Phase B; wildcards in test_internal_files.py ✅ Phase E |
| Batch files | 1 | test_batch.py | ✅ Phase E (IF EXIST/GOTO/@ECHO OFF, FOR, %1 args, REM/PAUSE) |
| ATTRIB / LABEL | 1 | test_attrib_label.py | ✅ Phase E (+R/-R host-verified, LABEL/VOL round-trip) |
| CHKDSK | 1 | test_chkdsk.py | ✅ Phase E (totals match host-side FAT12 math) |
| FIND/SORT/MORE/COMP/FC/pipelines | 1 | test_text_tools.py | ✅ Phase E/F (redirection and multi-stage pipes; FC via B:) |
| TREE/XCOPY/REPLACE | 1 | test_tree_xcopy_replace.py | ✅ Phase E/F (all host-verified; REPLACE fixed by memory shift/rotate displacement single-decode) |
| EDLIN | 1 | test_edlin.py | ✅ Phase F (insert/save host-verified; fixed memory shift/rotate displacement double-decode) |
| DEBUG | 1 | test_debug_tool.py | ✅ Phase D (-A/-T/-R/-D/-E/-Q) |
| EXE2BIN / LINK | 1/2 | test_exe2bin_link.py | ✅ Phase E (EXE2BIN usage; LINK loads banner+prompt) |
| FORMAT/SYS/DISKCOPY/DISKCOMP | 1 | test_disk_tools.py | ✅ Phase C/E/F (FORMAT/SYS host-verified; SYS disk boots a fresh emulator; DISKCOPY exact 720-sector match; DISKCOMP identical/different paths) |
| RECOVER | 2 | test_disk_tools.py | ✅ Phase E (usage returns, no crash) |
| BACKUP/RESTORE | 2 | test_disk_tools.py | ✅ Phase F (single-file BACKUP→delete→RESTORE round-trip host-verified) |
| GWBASIC | 2 | test_gwbasic.py | ✅ Phase F (reaches `Ok`; fixed null old-INT-1Ch chain and INT 10h cursor ABI) |
| TSRs/devices (SHARE/FASTOPEN/APPEND/PRINT/MODE/CTTY/ASSIGN/SUBST/JOIN/GRAFTABL) | 2 | test_tsr_and_devices.py | ✅ Phase E/F (load-without-crash; SUBST E: functional; PRINT resident; MODE COM1 configured; CTTY COM1 round-trip) |
| CONFIG.SYS drivers (ANSI/DRIVER/RAMDRIVE) | 2 | test_config_sys.py | ✅ Phase E/F (boot-smoke; RAMDRIVE C: read/write; ANSI clear/cursor/colour rendering in VRAM) |
| FDISK | 2 | test_fdisk.py | ✅ Phase G (active primary FAT12 partition; MBR host-verified on private HDD image) |
| FORMAT C:/fixed-disk files | 2 | test_hard_disk_filesystem.py | ✅ Phase H (fresh-boot C: discovery; FORMAT and file round-trip host-verified) |
| Hard-disk boot | 2 | test_hard_disk_boot.py | ✅ Phase I (FDISK → FORMAT /S → fresh MBR boot to C>; file readback) |
| FAT16 hard disk | 2 | test_fat16.py, test_fat16_hard_disk.py | ✅ Phase J (20MB type 04h partition; FORMAT /S, host round-trip, direct boot) |
| KEYB/NLSFUNC/SELECT/DISPLAY/GRAPHICS | 3 | test_tier3_graceful.py | ✅ Phase E (graceful return; SELECT declined via dialog) |
