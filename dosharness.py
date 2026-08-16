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
import shutil
import sys
import tempfile
from dataclasses import dataclass

# Repo-root importability: tests/conftest inserts the parent dir, but this
# module lives at the repo root and may be imported from ad-hoc scripts run
# from elsewhere, so make the directory containing *this* file importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import Emulator  # noqa: E402

# Canonical 5.25" DOS 3.3 distribution images shipped in this repo.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DISK01 = os.path.join(REPO_ROOT, 'DOS3_3_525', 'DISK01.IMG')
DISK02 = os.path.join(REPO_ROOT, 'DOS3_3_525', 'DISK02.IMG')

# Marker printed by errorlevel() — chosen to never appear in normal DOS
# output.  The probe checks for it as a *standalone line* (not a
# substring) so the echoed command line ``A>IF ERRORLEVEL 1 ECHO XY`` is
# not mistaken for the marker itself.
_FAIL_MARKER = 'XX_FAIL_XX'


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
                 host_dir=None, host_dir_write=False, boot_drive=0x00,
                 writable=False, settle_extra=2000):
        self.image_path = image_path
        self.image_b_path = image_b
        self.hard_disk_path = hard_disk
        self.host_dir = host_dir
        self.host_dir_write = host_dir_write
        self.boot_drive = boot_drive
        self.writable = writable
        self.settle_extra = settle_extra

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
                             persist=host_dir_write)
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

        bios_ref = self.emu.bios

        def hooked_interrupt(n):
            saved_flags = cpu.flags
            cpu._push(saved_flags)
            cpu.tf = False
            cpu.if_flag = False
            cpu._push(cpu.cs)
            cpu._push(cpu.ip)
            cpu.int_no_return = False
            bios_ref.handle_interrupt(cpu, n)
            if not cpu.int_no_return:
                self.emu._finish_interrupt_return(saved_flags)

        cpu._do_interrupt = hooked_interrupt
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
                row += chr(ch) if 0x20 <= ch <= 0x7E else ' '
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

    def run_steps(self, n):
        # Keep interrupt/timer servicing bounded while amortizing Python loop
        # overhead for long-running guest programs (MASM is a notable case).
        # Device handlers remain synchronous; only the periodic pump is
        # batched, so keyboard and PIT latency stays well below one batch.
        pit = 0
        remaining = n
        while remaining and not self.cpu.halted:
            batch = min(256, remaining)
            for _ in range(batch):
                if not self.cpu.execute():
                    remaining = 0
                    break
            remaining -= batch
            pit += batch
            if pit >= 500 and self.emu.pit:
                pit %= 500
                self.emu.io.tick(1.0 / 18.2)
            self._pump()
        return not self.cpu.halted

    def _pump(self):
        if self.emu.pic:
            self.emu._check_and_dispatch_irq()
        kc = self.emu.kbd_ctrl
        if kc and kc.has_data() and not getattr(kc, 'irq_pending', False):
            kc.irq_pending = True
            if self.emu.pic:
                self.emu.pic.raise_irq(1)

    def wait_for(self, text, max_steps=6_000_000):
        step = 0
        last_ip = None
        stuck = 0
        while step < max_steps:
            if not self.cpu.halted:
                if not self.cpu.execute():
                    break
                step += 1
            if step % 10000 == 0 and text in self.vga_str():
                return step
            if step % 500 == 0 and self.emu.pit:
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
        if not lines[-1].rstrip().endswith(('A>', 'B>', 'C>')):
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
        next_pit_tick = 500
        while step < limit:
            batch = min(256, limit - step)
            executed = 0
            while executed < batch and not self.cpu.halted:
                if not self.cpu.execute():
                    break
                executed += 1
            if not executed:
                break
            step += executed
            if step >= next_prompt_check and self._at_prompt(prev_screen):
                return step, False
            if step >= next_prompt_check:
                next_prompt_check += 5000
            if step >= next_pit_tick and self.emu.pit:
                self.emu.io.tick(1.0 / 18.2)
                next_pit_tick += 500
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
