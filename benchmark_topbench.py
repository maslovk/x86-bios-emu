"""Run TopBench for a bounded virtual-time sample.

TopBench is an interactive profiler and does not return to the DOS prompt
after starting. This tool measures a fixed instruction window instead.
"""

import argparse
from dataclasses import replace
import json
import os

from dosharness import DOSHarness
from machine_profiles import MACHINE_PROFILES, get_machine_profile


DEFAULT_TOPBENCH_DIR = os.path.join('DOS_tools', 'Topbench')


def build_parser():
    parser = argparse.ArgumentParser(
        description='Run TopBench for a bounded emulator instruction window')
    parser.add_argument('--hard-disk', required=True,
                        help='DOS hard-disk image to boot')
    parser.add_argument('--topbench-dir', default=DEFAULT_TOPBENCH_DIR,
                        help='directory containing TOPBENCH.EXE')
    parser.add_argument('--machine', choices=tuple(MACHINE_PROFILES),
                        default='generic')
    parser.add_argument('--cpu-backend', choices=('python', 'c'), default='c')
    parser.add_argument('--steps', type=int, default=1_500_000,
                        help='instructions to execute after launching')
    parser.add_argument('--json', action='store_true',
                        help='emit a machine-readable JSON result')
    parser.add_argument('--live', action='store_true',
                        help='use TopBench live score mode instead of -i')
    parser.add_argument('--cpi', type=float,
                        help='override profile cycles per instruction')
    return parser


def run_topbench(args):
    if args.steps <= 0:
        raise ValueError('--steps must be positive')
    profile = get_machine_profile(args.machine)
    if args.cpi is not None:
        if args.cpi <= 0:
            raise ValueError('--cpi must be positive')
        profile = replace(profile, cycles_per_instruction=args.cpi)
    harness = DOSHarness(
        hard_disk=args.hard_disk, host_dir=args.topbench_dir,
        boot_drive=0x80, cpu_backend=args.cpu_backend, machine=profile,
        emulated_timing=True)
    harness.boot_to_prompt()
    for source in ('TOPBENCH.EXE', 'DATABASE.INI'):
        copied = harness.run_command(
            f'COPY B:\\{source} C:\\{source}',
            max_steps=1_200_000, timeout_steps=800_000,
            probe_errorlevel=False)
        if copied.timed_out:
            raise RuntimeError(f'could not copy {source} to the DOS image')

    harness.emu.io.trace_port_accesses = {}
    harness.emu.io.trace_port_values = {}
    # -i prints the measured score and exits. -l continuously displays the
    # score and is useful when diagnosing a long-running calibration phase.
    option = '-l' if args.live else '-i'
    harness.inject_string(f'C:\\TOPBENCH.EXE {option}\r')
    before = harness.cpu.insn_count
    harness.run_steps(args.steps)
    profile = harness.emu.machine_profile
    address = ((harness.cpu.cs << 4) + harness.cpu.ip) & 0xFFFFF
    return {
        'machine': profile.id,
        'cpu_backend': args.cpu_backend,
        'requested_instructions': args.steps,
        'instructions': harness.cpu.insn_count - before,
        'cycle_count': harness.cpu.cycle_count,
        'emulated_time_seconds': harness.cpu.emulated_time,
        'video_mode': harness.emu.video.mode,
        'halted': harness.cpu.halted,
        'cs': harness.cpu.cs,
        'ip': harness.cpu.ip,
        'opcode_bytes': bytes(harness.emu.mem.read_byte((address + i) & 0xFFFFF)
                              for i in range(8)).hex(),
        'registers': {name: getattr(harness.cpu, name)
                      for name in ('ax', 'bx', 'cx', 'dx', 'si', 'di', 'sp', 'flags')},
        'bda_ticks': harness.emu.mem.read_dword(0x046C),
        'port_accesses': {
            f'{direction}{port:03X}': count
            for (direction, port), count
            in harness.emu.io.trace_port_accesses.items()
        },
        'port_values': {
            f'{port:03X}={value:02X}': count
            for (port, value), count
            in harness.emu.io.trace_port_values.items()
        },
        'screen': harness.vga_str(),
    }


def main(argv=None):
    args = build_parser().parse_args(argv)
    result = run_topbench(args)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print('TopBench bounded validation')
        print(f"machine: {result['machine']}")
        print(f"instructions: {result['instructions']}")
        print(f"cycles: {result['cycle_count']:.0f}")
        print(f"virtual seconds: {result['emulated_time_seconds']:.3f}")
        print(f"video mode: {result['video_mode']:02X}")
        print(f"halted: {result['halted']}")
        print(result['screen'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
