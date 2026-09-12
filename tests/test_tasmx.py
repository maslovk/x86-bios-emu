"""Run the real protected-mode assembler, link its output, and execute it.

Requires the local DOS 6.22 installation and Borland TASM 4.0 tools; neither
is redistributed by the repository. All guest disk writes use private copies.
"""

from pathlib import Path
import os
import shutil
import runpy

import pytest

from dosharness import DOSHarness


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'DOS_sources/TASM40/BIN'
HDD = ROOT / 'dos622-new.hdd'
FLOPPY = ROOT / 'DOS6_22/Disk1.img'
DPMIINST = ROOT / 'DOS_sources/TASM/DPMIINST.EXE'
LINK = ROOT / 'DOS_sources/v4.0/src/TOOLS/LINK.EXE'
TOOL_FILES = ('TASMX.EXE', 'RTM.EXE', 'DPMI16BI.OVL', 'DPMI32VM.OVL')
pytestmark = [pytest.mark.slow, pytest.mark.skipif(
    not all(p.is_file() for p in (HDD, FLOPPY, DPMIINST, *(TOOLS / f for f in TOOL_FILES))),
    reason='local DOS 6.22 and TASM 4.0 fixtures are required')]


@pytest.mark.parametrize('linker', ['microsoft', 'turbo'])
def test_tasmx_assembles_links_and_runs(tmp_path, linker):
    linker_path = TOOLS / 'TLINK.EXE' if linker == 'turbo' else LINK
    if not linker_path.is_file():
        pytest.skip(f'local {linker} linker fixture is required')
    tools = tmp_path / 'tools'
    work = tmp_path / 'work'
    tools.mkdir()
    work.mkdir()
    for name in TOOL_FILES:
        shutil.copy2(TOOLS / name, tools / name)
    configured_dpmi = os.environ.get('TASMX_DPMI_OVL')
    if configured_dpmi:
        shutil.copy2(configured_dpmi, tools / 'DPMI16BI.OVL')
    shutil.copy2(DPMIINST, tools / 'DPMIINST.EXE')
    shutil.copy2(linker_path, tools / linker_path.name)
    source = """.MODEL SMALL
.STACK 100h
.DATA
message DB 'Hello from TASMX', 13, 10, '$'
.CODE
start:
    mov ax, @data
    mov ds, ax
    mov dx, OFFSET message
    mov ah, 9
    int 21h
    mov ax, 4C00h
    int 21h
END start
"""
    (work / 'HELLO.ASM').write_bytes(source.replace('\n', '\r\n').encode('ascii'))
    harness = DOSHarness(
        image_path=str(FLOPPY), hard_disk=str(HDD), boot_drive=0x80,
        writable=True, host_mounts={'D': str(tools), 'E': str(work)},
        host_dir_write=True, cpu_backend='python')
    try:
        original_at_prompt = harness._at_prompt

        def checked_prompt(previous):
            screen = harness.vga_str()
            assert 'unhandled exception' not in screen.lower(), screen
            return original_at_prompt(previous)

        # The Borland crash handler waits for a key instead of exiting.
        # Report it immediately, not after the DOS prompt watchdog expires.
        harness._at_prompt = checked_prompt
        build = runpy.run_path(str(ROOT / 'scripts/build_volkov'))
        driver = build['GuestDriver'](harness)
        driver.wait(lambda screen: build['at_prompt'](screen, 'C'),
                    35_000_000, 'DOS C: prompt')
        driver.change_drive('D')
        if configured_dpmi:
            driver.configure_dpmi()
        else:
            # Borland's supported machine-configuration option: the same
            # A20/386 return methods verified for this emulator. DPMIINST
            # computes the BIOS checksum and updates its own database;
            # neither executable nor overlay code is patched by the test.
            driver.command('DPMIINST -f02/02/04/0000/0000', 'D', 10_000_000)
        harness.emu._persist_host_dir()
        driver.change_drive('C')
        resets_before = list(harness.emu.reset_requests)

        def command(text):
            print(f'  running {text}', flush=True)
            result = harness.run_command(text, max_steps=20_000_000)
            assert not result.timed_out, result.output
            assert result.errorlevel == 0, result.output
            print(f'  completed {text} ({result.steps:,} steps)', flush=True)
            return result.output

        command('PATH D:\\;C:\\DOS')
        output = command('TASMX E:\\HELLO.ASM,E:\\HELLO.OBJ')
        assert 'Error messages:    None' in output
        assert 'Warning messages:  None' in output
        if linker == 'turbo':
            output = command('TLINK E:\\HELLO.OBJ,E:\\HELLO.EXE')
            assert 'Turbo Link  Version 6.00' in output
        else:
            command('LINK E:\\HELLO.OBJ,E:\\HELLO.EXE,NUL;')
        assert 'Hello from TASMX' in command('E:\\HELLO.EXE')
        assert harness.emu.reset_requests == resets_before
        harness.emu._persist_host_dir()

        obj = (work / 'HELLO.OBJ').read_bytes()
        records = []
        offset = 0
        while offset < len(obj):
            length = int.from_bytes(obj[offset + 1:offset + 3], 'little')
            record = obj[offset:offset + 3 + length]
            assert length >= 1 and len(record) == length + 3
            assert sum(record) & 0xFF == 0  # OMF record checksum
            records.append(record[0])
            offset += len(record)
        assert records[0] == 0x80  # THEADR
        assert 0xA0 in records     # LEDATA
        assert records[-1] == 0x8A # MODEND
        assert (work / 'HELLO.EXE').read_bytes().startswith(b'MZ')
    finally:
        harness.cleanup()
