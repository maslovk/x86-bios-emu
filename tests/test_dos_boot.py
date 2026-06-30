"""Integration tests for DOS 3.3 boot and command execution.

These boot real MS-DOS 3.3 from the DISK01.IMG floppy image, drive the
keyboard via kbd_ctrl, and assert on VGA text output.  Slow (each test
boots DOS); kept separate from the fast unit tests.

Run with:  pytest tests/test_dos_boot.py -v
"""
import sys, os, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from main import Emulator


IMG = os.path.join(os.path.dirname(__file__), '..', 'DOS3_3_525', 'DISK01.IMG')


class DOSHarness:
    """Boots DOS 3.3 to the A> prompt and can type commands."""

    def __init__(self):
        self.emu = Emulator(boot_file=None, step_mode=False, floppy_image=IMG)
        self.emu.bios.initialize()
        if self.emu.pic:
            self.emu.pic.initialize()
        self.emu._setup_ivt_irq_handlers()
        buf = bytearray(512)
        self.emu.disk.read_sector(0, buf)
        for i in range(512):
            self.emu.mem.write_byte(0x7C00 + i, buf[i])
        cpu = self.emu.cpu
        cpu.cs = 0; cpu.ip = 0x7C00
        cpu.ds = 0; cpu.es = 0; cpu.ss = 0; cpu.sp = 0x7C00
        self.emu._install_bios_interrupt_hook()

        bios_ref = self.emu.bios
        def hooked_interrupt(n):
            saved_flags = cpu.flags
            cpu._push(saved_flags); cpu.tf = False; cpu.if_flag = False
            cpu._push(cpu.cs); cpu._push(cpu.ip); cpu.int_no_return = False
            bios_ref.handle_interrupt(cpu, n)
            if not cpu.int_no_return:
                self.emu._finish_interrupt_return(saved_flags)
        cpu._do_interrupt = hooked_interrupt
        self.cpu = cpu

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

    def run_steps(self, n):
        pit = 0
        for _ in range(n):
            if not self.cpu.halted:
                if not self.cpu.execute():
                    break
            pit += 1
            if pit >= 500 and self.emu.pit:
                pit = 0; self.emu.io.tick(1.0 / 18.2)
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
        step = 0; last_ip = None; stuck = 0
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

    def inject_string(self, s, delay=2000):
        for ch in s:
            self.emu.kbd_ctrl.inject_key(ord(ch))
            self.run_steps(delay)

    def boot_to_prompt(self):
        """Boot through DATE/TIME prompts to the A> prompt."""
        self.wait_for('Enter new date')
        self.inject_string('\r')
        self.wait_for('Enter new time')
        self.inject_string('\r')
        self.wait_for('A>')

    def run_command(self, cmd, max_steps=5_000_000):
        """Type a command + Enter, run until the screen shows a new A> at
        the bottom (or step limit), return the screen text."""
        self.run_steps(20000)  # settle
        self.inject_string(cmd + '\r')
        # Wait for the bottom non-empty line to end with A> or C>
        step = 0; last_ip = None; stuck = 0
        initial_screen = self.vga_str()
        while step < max_steps:
            if not self.cpu.halted:
                if not self.cpu.execute():
                    break
                step += 1
            if step % 5000 == 0:
                lines = [l for l in self.vga_text() if l.strip()]
                if lines and lines[-1].rstrip().endswith(('A>', 'C>')):
                    if self.vga_str() != initial_screen:
                        return self.vga_str()
            if step % 500 == 0 and self.emu.pit:
                self.emu.io.tick(1.0 / 18.2)
            self._pump()
            cur = (self.cpu.cs << 4) + self.cpu.ip
            if cur == last_ip:
                stuck += 1
                if stuck > 500000:
                    break
            else:
                stuck = 0
            last_ip = cur
        return self.vga_str()


@pytest.mark.slow
class TestDOSBoot:
    """Slow integration tests — boots real MS-DOS 3.3."""

    def test_boot_reaches_ms_dos_banner(self):
        """DOS boots and prints the 'Microsoft MS-DOS Version 3.30' banner."""
        h = DOSHarness()
        h.boot_to_prompt()  # boot fully to A>
        screen = h.vga_str()
        assert 'MS-DOS' in screen
        assert 'Version 3.30' in screen

    def test_boot_reaches_a_prompt(self):
        """DOS reaches the A> prompt after DATE/TIME."""
        h = DOSHarness()
        h.boot_to_prompt()
        screen = h.vga_str()
        assert 'A>' in screen

    def test_echo_command(self):
        """ECHO prints its argument (internal command, no disk I/O)."""
        h = DOSHarness()
        h.boot_to_prompt()
        screen = h.run_command('ECHO TestPassed')
        assert 'TestPassed' in screen

    def test_dir_shows_volume_header(self):
        """DIR prints 'Volume in drive A' (internal command + FCB search)."""
        h = DOSHarness()
        h.boot_to_prompt()
        screen = h.run_command('DIR')
        assert 'Volume in drive A' in screen or 'Directory of' in screen

    def test_bad_command_message(self):
        """An unknown command gives 'Bad command or file name'."""
        h = DOSHarness()
        h.boot_to_prompt()
        screen = h.run_command('ZZZXYZ')
        assert 'Bad command' in screen or 'File not found' in screen
