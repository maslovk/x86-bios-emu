# PLAN: Boot MS-DOS 3.3 and exercise every DOS tool

This is an execution plan for an LLM agent working in this repository. Each
phase lists **features** (with exact files and functions to change), **tests**
(exact test files and assertions), and **acceptance criteria** that can be
verified by running commands. Work through phases in order — later phases
depend on earlier ones. After every phase: `python3 -m pytest -q` must stay
green (including `-m slow`), and nothing in `DOS3_3_525/` may be modified
(tests always work on temp copies of the images).

## 0. Current state (verified 2026-07-03)

Working: DOS 3.3 boots from `DOS3_3_525/DISK01.IMG` to the `A>` prompt;
`DIR`, `VER`, `CLS`, `TYPE`-style internal commands work; 477 tests pass.

Verified gaps that block "test all DOS tools":

| # | Gap | Where | Symptom |
|---|-----|-------|---------|
| 1 | INT 13h AH=03 (write sectors) returns success **without writing** | `bios.py:521` | COPY/FORMAT/SYS/EDLIN save all silently fail or corrupt |
| 2 | INT 13h subfunction semantics wrong: AH=04 handled as "recalibrate" (real: Verify), AH=05 as "media changed" (real: Format Track), AH=0D as "get disk type" (real AH=15h), AH=16h/17h/18h missing | `bios.py:547-568` | FORMAT and media-detection paths mis-dispatch |
| 3 | DAA/DAS/AAA/AAS silently skipped | `cpu.py:925` | Wrong decimal output in tools that use BCD (DEBUG, GWBASIC, CHKDSK stats) |
| 4 | TF set → no INT 1 after instruction; INT 3/INTO exist as vectors but single-step trap never fires | `cpu.py` `execute()` | DEBUG `T`/`P` commands can't work |
| 5 | FPU escapes D8–DF skipped (ModR/M consumed) | `cpu.py:1603` | OK for most tools (equipment word reports no 8087, so GWBASIC uses software FP) — keep, but must consume ModR/M *with displacement* correctly |
| 6 | Single floppy drive only; DL ignored in INT 13h | `bios.py::_int13h`, `main.py` | DISKCOPY/DISKCOMP/`COPY A: B:` impossible |
| 7 | `fat12.py` is read-only | `fat12.py` | Tests can't inject fixture files pre-boot, can't verify guest writes host-side |
| 8 | No image persistence / copy-on-write; guest writes (once fixed) mutate only in-memory sectors | `main.py::_load_floppy`, `video.py::Disk` | Can't assert on written files after emulator exit; risk of mutating repo images |
| 9 | `DOSHarness` lives inside `tests/test_dos_boot.py` | tests | Not reusable for a per-tool suite |
| 10 | No Ctrl-C / Ctrl-Break path (INT 1Bh, kbd controller has no break scan-code injection) | `hardware.py`, `bios.py` | Can't test BREAK, can't abort runaway tools (MORE, SORT, PRINT) |

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
  GWBASIC LINK GRAFTABL.
- **Tier 3 — out of scope, must fail *gracefully* (clean error, no emulator
  crash/hang)**: FDISK (no hard disk), KEYB/NLSFUNC/SELECT/DISPLAY/GRAPHICS
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
    host-side. (EDLIN uses Ctrl-C to exit insert mode — needs Phase D item 2;
    if not yet available, use Ctrl-Z? No — EDLIN needs Ctrl-C, so mark this
    test `xfail` until Phase D and note it here.)

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
    IO.SYS/MSDOS.SYS first two directory entries; **stretch**: boot a second
    emulator from the resulting B image to the `A>` prompt (COMMAND.COM must
    be copied too).
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
2. `MODE COM1:9600,N,8,1` exercises INT 14h AH=00 (exists) — just test it.
3. `SHARE`/`FASTOPEN`/`APPEND` are TSRs: test = loads without crash, prompt
   returns, and a follow-up `DIR` still works (memory not corrupted).
4. `CTTY COM1` + serial: redirect console via the existing `Serial` —
   Tier 2 stretch; skip if DOS device chain fights back, but it must not
   hang (watchdog catches it).

### Tests — one file per tool family in `tests/tools/`

| File | Commands covered | Core assertions |
|------|------------------|-----------------|
| `test_internal_basic.py` | VER VOL DATE TIME CLS SET PATH PROMPT ECHO | banner text, settable+queryable state, CLS empties screen |
| `test_internal_files.py` | DIR TYPE COPY DEL REN (from Phase B) | + wildcards: `COPY *.TXT`, `DEL *.BAK`, `DIR *.COM /W` |
| `test_batch.py` | batch: ECHO OFF, REM, PAUSE, IF, FOR, GOTO, SHIFT, CALL args, ERRORLEVEL | write .BAT via `create_file`, run, assert flow (e.g. FOR %%F IN loop output) |
| `test_attrib_label.py` | ATTRIB +R/-R, LABEL, VOL | +R makes DEL fail; label round-trips and shows in DIR header |
| `test_chkdsk.py` | CHKDSK, CHKDSK /F on clean disk | reported total/free bytes match host-side FAT12 math exactly |
| `test_text_tools.py` | FIND SORT MORE COMP FC | FIND "x" counts lines; SORT reorders; SORT </R; MORE paginates (send space); COMP/FC identical vs differing files, errorlevel |
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
| DIR/TYPE/VER/... (internal, read-only) | 1 | test_internal_basic.py | ✅ (pre-existing) |
| COPY/DEL/REN/MD/CD/RD | 1 | test_file_io.py (core) | ✅ Phase B (wildcards in test_internal_files.py: ⬜ Phase E) |
| Batch files | 1 | test_batch.py | ⬜ Phase E |
| ATTRIB / LABEL | 1 | test_attrib_label.py | ⬜ Phase E |
| CHKDSK | 1 | test_chkdsk.py | ⬜ Phase E |
| FIND/SORT/MORE/COMP/FC | 1 | test_text_tools.py | ⬜ Phase E |
| TREE/XCOPY/REPLACE | 1 | test_tree_xcopy_replace.py | ⬜ Phase E |
| EDLIN | 1 | test_file_io.py (xfail) | ⬜ Phase D (needs Ctrl-C) |
| DEBUG | 1 | test_debug_tool.py | ⬜ Phase D |
| EXE2BIN / LINK | 1/2 | test_exe2bin_link.py | ⬜ Phase E |
| FORMAT/SYS/DISKCOPY/DISKCOMP | 1 | test_disk_tools.py | ⬜ Phase C |
| RECOVER | 2 | test_disk_tools.py | ⬜ Phase C |
| BACKUP/RESTORE | 2 | test_disk_tools.py | ⬜ Phase C |
| GWBASIC | 2 | test_gwbasic.py | ⬜ Phase D |
| TSRs (SHARE/FASTOPEN/APPEND/PRINT/MODE/ASSIGN/SUBST/JOIN/GRAFTABL) | 2 | test_tsr_and_devices.py | ⬜ Phase E |
| CONFIG.SYS drivers (ANSI/DRIVER/RAMDRIVE) | 2 | test_config_sys.py | ⬜ Phase E |
| FDISK/KEYB/NLSFUNC/SELECT/DISPLAY/GRAPHICS | 3 | test_tier3_graceful.py | ⬜ Phase E |
