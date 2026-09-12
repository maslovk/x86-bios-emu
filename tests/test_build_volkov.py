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


def test_tasmx_uses_checked_command_without_reset_or_key_injection(monkeypatch):
    from types import SimpleNamespace
    harness = SimpleNamespace(cpu=SimpleNamespace(max_insns=0, insn_count=0))
    driver = build_volkov.GuestDriver(harness)
    commands = []

    def command(*args):
        commands.append(args)
        return 'assembler output'

    monkeypatch.setattr(driver, 'command', command)
    assert driver.tasmx_command('TASMX VCOVL', 'E', 100) == 'assembler output'
    assert commands == [('TASMX VCOVL', 'E', 100)]


def test_tasmx_propagates_build_failure(monkeypatch):
    import pytest
    from types import SimpleNamespace
    harness = SimpleNamespace(cpu=SimpleNamespace(max_insns=0, insn_count=0))
    driver = build_volkov.GuestDriver(harness)

    def command(*args):
        raise build_volkov.BuildError('assembler failed')

    monkeypatch.setattr(driver, 'command', command)
    with pytest.raises(build_volkov.BuildError, match='assembler failed'):
        driver.tasmx_command('TASMX VCOVL', 'E', 100)


def test_wait_reports_borland_crash_without_spending_instruction_budget():
    import pytest
    from types import SimpleNamespace
    harness = SimpleNamespace(
        cpu=SimpleNamespace(max_insns=0, insn_count=0),
        vga_str=lambda: 'Unhandled exception 000D at 01EF:1234')
    driver = build_volkov.GuestDriver(harness)
    with pytest.raises(build_volkov.BuildError, match='Unhandled exception'):
        driver.wait(lambda screen: False, 600_000_000, 'TASMX')


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
