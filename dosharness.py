"""MS-DOS 3.3 test harness.

Boots a real MS-DOS 3.3 floppy image to the ``A>`` prompt and lets tests type
commands and assert on VGA output and scrollback transcript.

Uses the in-tree emulator (:class:`main.Emulator`) directly — no subprocess —
so tests can inject keys, read VGA memory, and mount the written image
host-side with :mod:`fat12`.  Slow by design: a cold boot takes a few seconds
of host time, so read-only test suites should reuse one harness via the
module-scoped ``dos`` fixture in ``tests/tools/conftest.py``.
"""
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass

# Repo-root importability: tests/conftest inserts the parent dir, but this
# module lives at the repo root and may be imported from ad-hoc scripts run
# from elsewhere, so make the directory containing *this* file importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import Emulator  # noqa: E402
from video import decode_vga_char  # noqa: E402

# Canonical 5.25" DOS 3.3 distribution images shipped in this repo.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DISK01 = os.path.join(REPO_ROOT, 'DOS3_3_525', 'DISK01.IMG')
DISK02 = os.path.join(REPO_ROOT, 'DOS3_3_525', 'DISK02.IMG')

# Marker printed by errorlevel() — chosen to never appear in normal DOS
# output.  The probe checks for it as a *standalone line* (not a
# substring) so the echoed command line ``A>IF ERRORLEVEL 1 ECHO XY`` is
# not mistaken for the marker itself.
_FAIL_MARKER = 'XX_FAIL_XX'

# A DOS-era CPU executes roughly fifty thousand instructions in one BIOS
# timer period.  Servicing IRQ0 every 500 instructions made the harness run
# the 18.2 Hz timer about a hundred times too quickly, which can starve DOS's
# stack-switching INT 08 wrapper and timer-sensitive programs such as Pole.
PIT_INSTRUCTION_QUANTUM = 50_000


@dataclass
class CommandResult:
    """Outcome of running one DOS command via :meth:`DOSHarness.run_command`.

    Attributes:
        screen: the visible 80x25 VGA screen text at the moment the prompt
            returned (i.e. the command's own output, captured *before* the
            errorlevel probe so it is not disturbed by probe text).
        output: the full transcript for this command — every line that
            scrolled off the top plus the visible screen afterwards.  Use this
            for output longer than 25 rows.
        errorlevel: probed exit code (0 = success, 1 = error/fail), or None if
            the command timed out or the probe could not be scraped.
        timed_out: True if the watchdog aborted the run before a prompt
            returned (the caller should send more input or accept failure).
    """

    screen: str = ''
    output: str = ''
    errorlevel: int = None
    timed_out: bool = False
    steps: int = 0

    # ── String-compatibility shim for the pre-existing tests ────────────
    # The original DOSHarness.run_command returned the screen string and tests
    # wrote ``'X' in h.run_command(...)``.  CommandResult is not a string, but
    # proxying membership/str through ``.screen`` keeps those assertions green
    # without weakening them, while new tests use the structured fields.
    def __contains__(self, item):
        return item in self.screen

    def __str__(self):
        return self.screen

    def __repr__(self):
        el = 'None' if self.errorlevel is None else self.errorlevel
        return (f'CommandResult(errorlevel={el}, timed_out={self.timed_out}, '
                f'steps={self.steps}, screen={self.screen!r})')


class DOSHarness:
    """Boots DOS 3.3 to the A> prompt and can type commands.

    Args:
        image_path: floppy image to boot from (drive A).  Defaults to the
            shipped DISK01.IMG.
        image_b: optional second-drive image (drive B).  Stored for later
            wiring; full two-drive support lands with the disk-tool phase.
        hard_disk: optional raw C/4/17 image exposed as BIOS drive 80h.
        host_dir: optional host directory exposed read-only as drive B.
        boot_drive: BIOS drive to boot (00h for floppy A:, 80h for HDD).
        cpu_backend: CPU implementation name passed to :class:`main.Emulator`.
            ``python`` is the complete reference backend and default; ``c``
            is an explicit opt-in for a built native backend.
        writable: when True, the image(s) are copied to a private temp dir
            before booting, so any future writeback (host-side FAT12 writes,
            ``--persist``) can never mutate the repo images.  When False (the
            default for read-only tests) the image is loaded straight into the
            in-memory disk and never written back to disk.
        settle_extra: extra instruction steps injected before each keystroke
            in :meth:`inject_string`.  Larger values are slower but more
            robust against dropped keystrokes during heavy compute.
    """

    def __init__(self, image_path=DISK01, image_b=None, hard_disk=None,
                 host_dir=None, host_dir_write=False, host_mounts=None,
                 boot_drive=0x00, writable=False, settle_extra=2000,
                 cpu_backend='python', machine='generic',
                 emulated_timing=False):
        self.image_path = image_path
        self.image_b_path = image_b
        self.hard_disk_path = hard_disk
        self.host_dir = host_dir
        self.host_dir_write = host_dir_write
        self.host_mounts = dict(host_mounts or {})
        self.boot_drive = boot_drive
        self.writable = writable
        self.settle_extra = settle_extra
        self.cpu_backend = cpu_backend
        self.machine = machine
        self.emulated_timing = emulated_timing

        # Full scrollback transcript, accumulated by the Video scroll hook.
        self._scrollback = []
        # Pending ints requested via inject_key path at settle time may already
        # be flushed; this records how many scrollback lines existed when the
        # current command started, so .output is per-command.
        self._cmd_scroll_start = 0

        # Resolve the actual file to load, copying to a temp dir if writable.
        # The temp dir persists for the harness lifetime (kept on self) so the
        # loaded image stays available for host-side FAT12 inspection.
        self._tempdir = None
        load_path = self._materialise(image_path)
        load_path_b = self._materialise(image_b) if image_b else None
        load_path_hd = self._materialise(hard_disk) if hard_disk else None

        self.emu = Emulator(boot_file=None, step_mode=False,
                             floppy_image=load_path, floppy_b=load_path_b,
                             hard_disk=load_path_hd, boot_drive=boot_drive,
                             host_dir=host_dir, host_dir_write=host_dir_write,
                             host_mounts=dict(host_mounts or {}),
                             persist=host_dir_write,
                             cpu_backend=cpu_backend, machine=machine)
        self.emu.bios.initialize()
        if self.emu.pic:
            self.emu.pic.initialize()
        self.emu._setup_ivt_irq_handlers()
        # Wire the scrollback hook before any boot code renders.
        self.emu.video.on_scroll_line = self._on_scroll_line

        buf = bytearray(512)
        boot_disk = (self.emu.hard_disk if boot_drive == 0x80
                     else self.emu.disk)
        if boot_disk is None:
            raise ValueError("hard-disk boot requested without a hard disk")
        boot_disk.read_sector(0, buf)
        for i in range(512):
            self.emu.mem.write_byte(0x7C00 + i, buf[i])
        cpu = self.emu.cpu
        cpu.cs = 0
        cpu.ip = 0x7C00
        cpu.ds = 0
        cpu.es = 0
        cpu.ss = 0
        cpu.sp = 0x7C00
        cpu.dl = boot_drive
        self.emu._install_bios_interrupt_hook()

        # Keep the drive-B path for reference; the Emulator loaded it above
        # (drive B), wiring it into the BIOS for INT 13h DL=01 dispatch.
        self._load_path_b = load_path_b
        self._load_path_hd = load_path_hd
        self.cpu = cpu

    # ── Image materialisation ──────────────────────────────────────────

    def _materialise(self, image_path):
        """Return a path safe to boot from.

        For read-only sessions the repo image is fine (it is only read into
        memory).  For writable sessions a copy is made under a private temp
        dir so subsequent host-side writes can never touch the repo image.
        """
        if not self.writable:
            return image_path
        if self._tempdir is None:
            self._tempdir = tempfile.mkdtemp(prefix='dos-harness-')
        dst = os.path.join(self._tempdir, os.path.basename(image_path))
        shutil.copy2(image_path, dst)
        return dst

    def writable_image_path(self):
        """Path of the (possibly temp copy of the) drive-A image on disk.

        Tests that need to mount the image host-side with :class:`fat12.FAT12`
        after the guest wrote to it should use this path: it is the temp copy
        when ``writable=True``, and the original repo image otherwise.
        """
        if self.writable and self._tempdir is not None:
            return os.path.join(self._tempdir, os.path.basename(self.image_path))
        return self.image_path

    def cleanup(self):
        """Remove any temp image copies created for a writable session."""
        if self._tempdir is not None:
            shutil.rmtree(self._tempdir, ignore_errors=True)
            self._tempdir = None

    def flush_host_dir(self):
        """Persist guest writes made through the host-backed B: drive.

        ``DOSHarness`` runs the emulator directly rather than through
        :meth:`main.Emulator.run`, so the normal shutdown persistence hook is
        not called automatically.  Build and file-transfer tests can call
        this explicitly after a DOS command to make guest-created files
        visible in the host directory.
        """
        if self.host_dir and self.host_dir_write:
            self.emu._persist_host_dir()

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass

    # ── VGA access ─────────────────────────────────────────────────────

    def vga_text(self):
        lines = []
        for y in range(25):
            row = ''
            for x in range(80):
                ch = self.emu.mem.read_byte(0xB8000 + (y * 80 + x) * 2)
                row += decode_vga_char(ch)
            lines.append(row.rstrip())
        return lines

    def vga_str(self):
        return '\n'.join(self.vga_text())

    def _on_scroll_line(self, line):
        self._scrollback.append(line)

    def _transcript(self, since):
        """Scrollback lines (from index `since`) followed by the visible screen."""
        scrolled = self._scrollback[since:]
        visible = self.vga_str()
        parts = list(scrolled)
        parts.append(visible)
        return '\n'.join(p for p in parts if p)

    # ── CPU stepping ───────────────────────────────────────────────────

    def _execute_cpu_batch(self, budget):
        """Execute a bounded batch through an optional native backend."""
        native = getattr(self.cpu, 'execute_many', None)
        if native is not None and not getattr(self.cpu, 'step_mode', False):
            return native(budget)
        executed = 0
        while executed < budget and not self.cpu.halted:
            if not self.cpu.execute():
                break
            executed += 1
        return executed

    def run_steps(self, n):
        # Keep interrupt/timer servicing bounded while amortizing Python loop
        # overhead for long-running guest programs (MASM is a notable case).
        # Device handlers remain synchronous; only the periodic pump is
        # batched, so keyboard and PIT latency stays well below one batch.
        pit = 0
        last_cycles = self.cpu.cycle_count
        remaining = n
        while remaining:
            if self.cpu.halted:
                # HLT is DOS's normal wait primitive for console input. Give
                # queued keyboard/timer IRQs a chance to wake the guest. A
                # timer tick is important for native batches because Unicorn
                # returns from HLT with no instruction count to consume.
                if self.emu.pit:
                    self.emu.io.tick(1.0 / 18.2)
                self._pump()
                if self.cpu.halted:
                    remaining -= 1
                continue
            native = getattr(self.cpu, 'execute_many', None)
            batch_limit = getattr(self.cpu, 'native_batch_size', 4096) \
                if native is not None else 256
            batch = min(batch_limit, remaining)
            executed = self._execute_cpu_batch(batch)
            if not executed:
                break
            remaining -= executed
            if self.emulated_timing and self.emu.pit:
                current_cycles = self.cpu.cycle_count
                self.emu.io.tick(max(0.0, current_cycles - last_cycles) /
                                 self.cpu.cpu_clock_hz)
                last_cycles = current_cycles
            else:
                pit += executed
                if pit >= PIT_INSTRUCTION_QUANTUM and self.emu.pit:
                    pit %= PIT_INSTRUCTION_QUANTUM
                    self.emu.io.tick(1.0 / 18.2)
            self._pump()
        return not self.cpu.halted

    def _pump(self):
        if self.emu.pic:
            self.emu._check_and_dispatch_irq()
        self.emu._schedule_keyboard_irq()

    def wait_for(self, text, max_steps=6_000_000):
        step = 0
        last_cycles = self.cpu.cycle_count
        last_ip = None
        stuck = 0
        while step < max_steps:
            if self.cpu.halted:
                if self.emu.pit:
                    self.emu.io.tick(1.0 / 18.2)
                self._pump()
                if self.cpu.halted:
                    step += 1
                    continue
            if not self.cpu.halted:
                batch_limit = min(
                    getattr(self.cpu, 'native_batch_size', 4096),
                    max_steps - step) if hasattr(self.cpu, 'execute_many') else 1
                executed = self._execute_cpu_batch(batch_limit)
                if not executed:
                    break
                step += executed
            if step % 10000 == 0 and text in self.vga_str():
                return step
            if self.emulated_timing and self.emu.pit:
                current_cycles = self.cpu.cycle_count
                self.emu.io.tick(max(0.0, current_cycles - last_cycles) /
                                 self.cpu.cpu_clock_hz)
                last_cycles = current_cycles
            elif step % PIT_INSTRUCTION_QUANTUM == 0 and self.emu.pit:
                self.emu.io.tick(1.0 / 18.2)
            self._pump()
            cur = (self.cpu.cs << 4) + self.cpu.ip
            if cur == last_ip:
                stuck += 1
                if stuck > 500000:
                    return step
            else:
                stuck = 0
            last_ip = cur
        return step

    def inject_string(self, s, delay=None):
        """Type a string of ASCII characters, settling the CPU between keys.

        ``inject_key`` bypasses scan-code translation and buffers the exact
        ASCII byte (including control characters such as Ctrl-Z 0x1A and CR
        0x0D), so control chars work without extending the scan-code map.
        """
        if delay is None:
            delay = self.settle_extra
        for ch in s:
            self.emu.kbd_ctrl.inject_key(ord(ch))
            self.run_steps(delay)

    def inject_background(self, s, interval=0.05, repeat=1, stop_when=None,
                          stop_when_absent=None):
        """Start a background thread that re-injects ``s`` while stepping.

        Guests with a drain-then-block keyboard discipline (MS-DOS 5 Setup
        first flushes any pending key with INT 16h AH=01/AH=00, then does the
        real blocking AH=00 read) discard keys queued before the drain.  On
        real hardware the keystroke arrives during the blocked read; with a
        single-threaded runner that window is never open, so the injection
        must come from another thread while ``run_steps``/``wait_for`` keep
        the CPU spinning.  Keys landing in a drain window are discarded and
        re-injected on the next repeat.

        If ``stop_when`` is supplied, injection stops once that text appears
        on the VGA screen. If ``stop_when_absent`` is supplied, it stops once
        text that was present disappears. Returns the started thread (daemon;
        callers keep stepping in the main thread).
        """
        import threading

        def should_stop():
            if not stop_when and not stop_when_absent:
                return False
            screen = self.vga_str()
            return ((stop_when and stop_when in screen)
                    or (stop_when_absent
                        and stop_when_absent not in screen))

        def worker():
            for _ in range(repeat):
                for ch in s:
                    if should_stop():
                        return
                    time.sleep(interval)
                    if should_stop():
                        return
                    self.emu.kbd_ctrl.inject_key(ord(ch))
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t

    def inject_extended_background(self, scan_code, interval=0.05,
                                   repeat=1):
        """Re-inject an enhanced scan code while the guest is stepping.

        DOS Setup drains type-ahead before blocking for its menu key.  This
        mirrors :meth:`inject_background` for arrows and function keys so a
        raw scan code lands during that blocking window.
        """
        import threading

        def worker():
            for _ in range(repeat):
                time.sleep(interval)
                self.emu.kbd_ctrl.inject_extended_key(scan_code)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t

    def boot_to_prompt(self):
        """Boot through DATE/TIME prompts to the boot drive's DOS prompt."""
        self.wait_for('Enter new date')
        self.inject_string('\r')
        self.wait_for('Enter new time')
        self.inject_string('\r')
        self.wait_for('C>' if self.boot_drive == 0x80 else 'A>')

    # ── Prompt-return detection ────────────────────────────────────────

    def _at_prompt(self, prev_screen):
        """True when a fresh prompt sits on the bottom non-empty row.

        Detects ``A>`` / ``B>`` / ``C>`` at the end of the last non-blank line
        and requires the screen to have changed since the command was sent, so
        a pre-existing prompt is not mistaken for completion.
        """
        lines = [l for l in self.vga_text() if l.strip()]
        if not lines:
            return False
        last = lines[-1].rstrip()
        if (not last.endswith(('A>', 'B>', 'C>')) and
                re.search(r'[A-C]:\\[^>]*>$', last) is None):
            return False
        return self.vga_str() != prev_screen

    def _wait_prompt(self, prev_screen, max_steps, timeout_steps=None):
        """Run until a fresh prompt appears or a watchdog fires.

        Returns (steps, timed_out).
        ``timeout`` takes precedence over ``max_steps`` and is reported as a
        timeout rather than a hang so the harness never wedges pytest.
        """
        limit = timeout_steps if timeout_steps is not None else max_steps
        step = 0
        last_ip = None
        stuck = 0
        next_prompt_check = 5000
        next_pit_tick = PIT_INSTRUCTION_QUANTUM
        last_cycles = self.cpu.cycle_count
        while step < limit:
            if self.cpu.halted:
                if self.emu.pit:
                    self.emu.io.tick(1.0 / 18.2)
                self._pump()
                if self.cpu.halted:
                    step += 1
                    continue
            batch_limit = min(
                getattr(self.cpu, 'native_batch_size', 4096)
                if hasattr(self.cpu, 'execute_many') else 256,
                limit - step)
            executed = self._execute_cpu_batch(batch_limit)
            if not executed:
                break
            step += executed
            if step >= next_prompt_check and self._at_prompt(prev_screen):
                return step, False
            if step >= next_prompt_check:
                next_prompt_check += 5000
            if self.emulated_timing and self.emu.pit:
                current_cycles = self.cpu.cycle_count
                self.emu.io.tick(max(0.0, current_cycles - last_cycles) /
                                 self.cpu.cpu_clock_hz)
                last_cycles = current_cycles
            elif step >= next_pit_tick and self.emu.pit:
                self.emu.io.tick(1.0 / 18.2)
                next_pit_tick += PIT_INSTRUCTION_QUANTUM
            self._pump()
            cur = (self.cpu.cs << 4) + self.cpu.ip
            if cur == last_ip:
                stuck += 1
                if stuck > 2000000:
                    # CPU appeared to halt/stall: treat as a timeout so the
                    # caller sees .timed_out and the screen at failure point.
                    return step, True
            else:
                stuck = 0
            last_ip = cur
        timed_out = timeout_steps is not None or not self._at_prompt(prev_screen)
        return step, timed_out

    # ── Commands ───────────────────────────────────────────────────────

    def run_command(self, cmd, max_steps=8_000_000, timeout_steps=None,
                    probe_errorlevel=True):
        """Type ``cmd`` + Enter and run until a fresh prompt returns.

        Returns a :class:`CommandResult` whose ``.screen`` is the command's
        visible output (captured before any errorlevel probe), ``.output`` is
        the full per-command transcript (scrollback + visible), and
        ``.errorlevel`` is the probed exit code (None on timeout).
        """
        # Keep the CPU's global safety ceiling above this command's explicit
        # budget.  Long MASM/DOS builds legitimately exceed the historical
        # 50M default; the harness watchdog still bounds this invocation.
        requested = timeout_steps if timeout_steps is not None else max_steps
        self.cpu.max_insns = max(self.cpu.max_insns,
                                 self.cpu.insn_count + requested + 1)
        self.run_steps(20000)  # settle
        self._cmd_scroll_start = len(self._scrollback)
        prev_screen = self.vga_str()
        self.inject_string(cmd + '\r')

        steps, timed_out = self._wait_prompt(
            prev_screen, max_steps, timeout_steps)

        # Capture the command's visible output *before* disturbing the screen
        # with the errorlevel probe.
        screen = self.vga_str()

        errorlevel = None
        if not timed_out and probe_errorlevel:
            errorlevel = self.errorlevel()

        # Final transcript: scrollback that occurred during this command plus
        # the now-current visible screen (which may include probe text).
        output = self._transcript(self._cmd_scroll_start)

        return CommandResult(
            screen=screen,
            output=output,
            errorlevel=errorlevel,
            timed_out=timed_out,
            steps=steps,
        )

    def run_dialog(self, cmd, prompts, max_steps=8_000_000,
                   timeout_steps=None, probe_errorlevel=True):
        """Drive an interactive tool that prompts for input.

        Types ``cmd`` + Enter, then for each ``(wait_text, response)`` in
        ``prompts`` waits for ``wait_text`` to appear on screen and types
        ``response``.  After the last response, waits for the prompt to
        return.  Pre-feeding all responses up front does NOT work: DOS's line
        editor consumes the extra keystrokes during command typing, leaving
        the interactive prompt starved (the tool then spins on INT 16h AH=00's
        'no key' return with IF=0).  Sequential wait-then-type avoids that.
        """
        requested = timeout_steps if timeout_steps is not None else max_steps
        self.cpu.max_insns = max(self.cpu.max_insns,
                                 self.cpu.insn_count + requested + 1)
        self.run_steps(20000)  # settle
        self._cmd_scroll_start = len(self._scrollback)
        prev = self.vga_str()
        self.inject_string(cmd + '\r')
        timed_out = False
        for wait_text, response in prompts:
            self.wait_for(wait_text, max_steps=max_steps)
            if wait_text not in self.vga_str():
                timed_out = True
                break
            # A prompt can become visible while the guest is still inside its
            # output routine.  Some DOS tools subsequently use INT 21h/AH=0Ch
            # (flush keyboard buffer and read), which discards a response
            # injected immediately after the text first appears.  Let the
            # guest finish rendering and reach its input wait before typing.
            self.run_steps(20000)
            self.inject_string(response)
        steps = 0
        if not timed_out:
            steps, timed_out = self._wait_prompt(prev, max_steps, timeout_steps)
        screen = self.vga_str()
        errorlevel = None
        if not timed_out and probe_errorlevel:
            errorlevel = self.errorlevel()
        output = self._transcript(self._cmd_scroll_start)
        return CommandResult(
            screen=screen,
            output=output,
            errorlevel=errorlevel,
            timed_out=timed_out,
            steps=steps,
        )

    def errorlevel(self):
        """Probe the current DOS ERRORLEVEL by typing one IF command.

        ``IF ERRORLEVEL 1 ECHO XX_FAIL_XX`` echoes the fail marker iff the
        errorlevel is >= 1; otherwise nothing is echoed.  ``IF`` and ``ECHO``
        do not modify ERRORLEVEL, so the probe observes the value left by the
        previous command.  (``IF NOT ERRORLEVEL`` is deliberately avoided:
        this DOS 3.3 build rejects it with "Syntax error".)

        Returns 0 on success (errorlevel < 1), 1 on failure (errorlevel >= 1),
        or None if the probe did not reach a prompt.
        """
        self.run_steps(20000)  # settle
        self._cmd_scroll_start = len(self._scrollback)
        prev = self.vga_str()
        self.inject_string(f'IF ERRORLEVEL 1 ECHO {_FAIL_MARKER}\r')
        _ok, timed_out = self._wait_prompt(prev, max_steps=2_000_000)
        text = self._transcript(self._cmd_scroll_start)
        if timed_out:
            return None
        # Exact-line match: the echoed command line
        # ``A>IF ERRORLEVEL 1 ECHO XX_FAIL_XX`` contains the marker as a
        # substring, so a plain ``in`` test would false-positive on success;
        # only a standalone marker line means the IF branch fired.
        lines = {ln.strip() for ln in text.split('\n')}
        if _FAIL_MARKER in lines:
            return 1   # errorlevel >= 1
        return 0       # errorlevel < 1 (i.e. 0)

    # ── File creation ──────────────────────────────────────────────────

    def create_file(self, name, text):
        """Create a small text file inside DOS via ``COPY CON``.

        Types ``COPY CON <name>``, the body text, then Ctrl-Z (0x1A) + Enter to
        close the file.  Ctrl-Z goes through :meth:`inject_key` directly so no
        scan-code mapping is needed.  Returns the prompt screen.
        """
        return self._copy_con(name, text, as_bytes=False)

    def create_file_bytes(self, name, data):
        """Create a binary file inside DOS via ``COPY CON``.

        Like :meth:`create_file` but injects raw bytes (e.g. a tiny .COM), so
        the console path's control-character limitations apply: bytes 0x00
        (NUL, dropped), 0x0D/0x0A (Enter/newline, rewritten as CRLF) and 0x1A
        (Ctrl-Z, ends input) cannot survive the keyboard-to-file path and are
        rejected up front.  Returns the prompt screen.
        """
        return self._copy_con(name, data, as_bytes=True)

    def _copy_con(self, name, body, as_bytes=False):
        self.run_steps(20000)  # settle
        self._cmd_scroll_start = len(self._scrollback)
        prev = self.vga_str()
        self.inject_string(f'COPY CON {name}\r')
        # Give DOS a moment to open the CON target.
        self.run_steps(80000)
        if as_bytes:
            bad = {0x00: 'NUL', 0x0D: 'CR', 0x0A: 'LF', 0x1A: 'Ctrl-Z'}
            for b in body:
                if b in bad:
                    raise ValueError(
                        f'byte 0x{b:02X} ({bad[b]}) cannot be injected via '
                        f'COPY CON; pick an encoding that avoids it')
            for b in body:
                self.emu.kbd_ctrl.inject_key(b)
                self.run_steps(3000)
        else:
            self.inject_string(body)
        self.inject_string('\x1A\r')  # Ctrl-Z (EOF) + Enter
        _ok, _t = self._wait_prompt(prev, max_steps=4_000_000)
        return self.vga_str()

    # ── Drive manipulation ( groundwork for disk tools ) ───────────────

    def swap_disk(self, path):
        """Replace drive A's in-memory sectors with a freshly loaded image.

        Sets the ``media_changed`` flag on the disk so INT 13h AH=16h can
        report a media change to DOS (used by single-drive DISKCOPY prompts).
        Pass a path to an image file that the harness loads into memory.
        """
        with open(path, 'rb') as f:
            data = f.read()
        disk = self.emu.disk
        n_sectors = max(len(disk.sectors), len(data) // 512)
        sectors = [bytearray(512) for _ in range(n_sectors)]
        for i in range(len(data) // 512):
            sectors[i][:512] = data[i * 512:(i + 1) * 512]
        disk.sectors = sectors
        # Surface the media byte for INT 13h geometry/params lookups.
        if len(data) > 0x15:
            disk.media_type = data[0x15]
        disk.media_changed = True
        # Invalidate any FAT cache consumers; the harness keeps a fresh FAT12
        # view on demand via mount_candidate().
        self.emu.fat = None

    def persist_host_dir(self):
        """Flush guest writes on bridged drive B back to the host directory.

        This is useful for long-running DOS tools (assemblers and linkers)
        that create output files before the harness itself is torn down.
        It is a public wrapper around the emulator's normal write-back path.
        Returns ``True`` when a bridge is configured, otherwise ``False``.
        """
        if not self.host_dir or not self.host_dir_write:
            return False
        self.emu._persist_host_dir()
        return True
