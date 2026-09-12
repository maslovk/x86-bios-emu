"""Fast tests for the transactional Volkov Commander build driver."""

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / 'scripts' / 'build_volkov'
LOADER = SourceFileLoader('build_volkov_script', str(SCRIPT))
SPEC = spec_from_loader(LOADER.name, LOADER)
build_volkov = module_from_spec(SPEC)
LOADER.exec_module(build_volkov)


def test_prompt_detection_supports_extra_host_drives():
    for drive in ('D', 'E', 'F'):
        assert build_volkov.at_prompt(f'output\n{drive}:\\>', drive)
        assert build_volkov.at_prompt(
            f'output\n{drive}:\\SOURCE>', drive)

    assert not build_volkov.at_prompt('E:\\>TASM VC', 'E')
    assert not build_volkov.at_prompt('E:\\>', 'D')


def test_stale_outputs_are_removed_only_from_staging(tmp_path):
    kept = tmp_path / 'VC.ASM'
    kept.write_text('source')
    for name in ('VC.COM', 'VC.EXE', 'VC.OBJ', 'VC.OVL', 'VCOVL.OBJ'):
        (tmp_path / name).write_bytes(b'stale')

    build_volkov.remove_stale_outputs(tmp_path)

    assert kept.read_text() == 'source'
    assert sorted(path.name for path in tmp_path.iterdir()) == ['VC.ASM']


def test_atomic_publish_replaces_complete_artifact(tmp_path):
    source = tmp_path / 'staged.com'
    destination = tmp_path / 'output' / 'VC.COM'
    source.write_bytes(b'complete-build')
    destination.parent.mkdir()
    destination.write_bytes(b'old')

    build_volkov.publish_atomic(source, destination)

    assert destination.read_bytes() == b'complete-build'
    assert not list(destination.parent.glob('.VC.COM.*.tmp'))


def test_tasmx_relaunches_after_guest_reset(monkeypatch):
    class Keyboard:
        def inject_key(self, value):
            pass

    class Emulator:
        def __init__(self):
            self.reset_requests = []
            self.kbd_ctrl = Keyboard()

    class Harness:
        def __init__(self):
            self.cpu = type('CPU', (), {
                'max_insns': 0,
                'insn_count': 0,
                })()
            self.emu = Emulator()
            self._scrollback = []
            self.screens = ['E:\\>', 'C:\\>', 'E:\\>']
            self.commands = []

        def vga_str(self):
            return self.screens[0]

        def inject_string(self, value):
            self.commands.append(value)

        def _transcript(self, start):
            return 'transcript'

    harness = Harness()
    driver = build_volkov.GuestDriver(harness)
    waits = iter(['C:\\>', 'E:\\>', 'E:\\>', 'E:\\>'])

    def fake_wait(predicate, budget, description):
        screen = next(waits)
        if screen.startswith('C:'):
            harness.emu.reset_requests.append('triple-fault')
        return screen

    def fake_change_drive(drive):
        harness.commands.append(f'{drive}:\r')

    monkeypatch.setattr(driver, 'wait', fake_wait)
    monkeypatch.setattr(driver, 'change_drive', fake_change_drive)

    assert driver.tasmx_command('TASMX VCOVL', 'E', 100) == 'transcript'
    assert harness.commands == [
        'TASMX VCOVL\r', 'E:\r', 'PATH D:\\;F:\\;C:\\DOS\r',
        'IF ERRORLEVEL 1 ECHO __VC_BUILD_FAILED__\r',
        'TASMX VCOVL\r',
    ]


def test_dpmi_configuration_answers_optional_database_comment(monkeypatch):
    from types import SimpleNamespace

    class Harness:
        def __init__(self):
            self.cpu = SimpleNamespace(max_insns=0, insn_count=0)
            self.commands = []
            self.screen = ''
            self.screens = iter([
                'Please enter comment for database file:',
                'DPMI16BI file updated\nD:\\>',
            ])

        def inject_string(self, text):
            self.commands.append(text)

        def run_steps(self, steps):
            self.screen = next(self.screens)

        def vga_str(self):
            return self.screen

    harness = Harness()
    driver = build_volkov.GuestDriver(harness)
    monkeypatch.setattr(driver, 'wait', lambda *args: 'Proceed? (space to continue')
    driver.configure_dpmi()
    assert harness.commands == ['DPMIINST.EXE\r', ' ', ' ', ' ', ' ', '\r']
