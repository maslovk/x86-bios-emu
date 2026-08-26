"""Canonical machine timing profiles.

Profiles describe hardware targets and validation data.  They do not directly
force benchmark scores; the CPU and device timing models consume these values
as they become cycle-aware.
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class MachineProfile:
    """Hardware identity and timing parameters for a supported target."""

    id: str
    name: str
    cpu_model: str
    cpu_clock_hz: int
    timing_model: str
    cycles_per_instruction: float
    bus_clock_hz: int
    isa_clock_hz: int
    pit_clock_hz: int = 1_193_180
    video_adapter: str = 'CGA'
    memory_wait_states: int = 0
    io_wait_states: int = 0
    vram_wait_cycles: int = 0
    cache_enabled: bool = False
    benchmark_targets: object = MappingProxyType({})

    def __post_init__(self):
        if self.cpu_clock_hz <= 0 or self.bus_clock_hz <= 0:
            raise ValueError('machine clocks must be positive')
        if self.cycles_per_instruction <= 0:
            raise ValueError('cycles_per_instruction must be positive')
        if self.pit_clock_hz <= 0:
            raise ValueError('PIT clock must be positive')
        for name in ('memory_wait_states', 'io_wait_states', 'vram_wait_cycles'):
            if getattr(self, name) < 0:
                raise ValueError(f'{name} cannot be negative')

    def as_dict(self):
        """Return a serializable description suitable for diagnostics."""
        return {
            'id': self.id,
            'name': self.name,
            'cpu': {'model': self.cpu_model, 'clock_hz': self.cpu_clock_hz,
                    'timing_model': self.timing_model,
                    'cycles_per_instruction': self.cycles_per_instruction},
            'bus': {'clock_hz': self.bus_clock_hz,
                    'isa_clock_hz': self.isa_clock_hz},
            'pit': {'clock_hz': self.pit_clock_hz},
            'video': {'adapter': self.video_adapter,
                      'vram_wait_cycles': self.vram_wait_cycles},
            'memory': {'wait_states': self.memory_wait_states,
                       'cache_enabled': self.cache_enabled},
            'io_wait_states': self.io_wait_states,
            'benchmark_targets': dict(self.benchmark_targets),
        }


MACHINE_PROFILES = MappingProxyType({
    'generic': MachineProfile(
        'generic', 'Generic IBM-compatible PC', '8088-compatible',
        4_772_727, '8088', 12.0, 4_772_727, 4_772_727, 4_772_727),
    'ibm-pc-5150': MachineProfile(
        'ibm-pc-5150', 'IBM PC 5150', '8088', 4_772_727,
        '8088', 16.0, 4_772_727, 4_772_727, video_adapter='CGA',
        benchmark_targets=MappingProxyType({'landmark_mhz': 4.77})),
    'ibm-pc-xt': MachineProfile(
        'ibm-pc-xt', 'IBM PC XT 5160', '8088', 4_772_727,
        '8088', 16.0, 4_772_727, 4_772_727, video_adapter='CGA',
        benchmark_targets=MappingProxyType({
            'topbench_score': 4,
            'topbench_memory': 3774,
            'topbench_opcode': 1753,
            'topbench_vidram': 2652,
            'topbench_memea': 1935,
            'topbench_3dgame': 1851,
            'landmark_mhz': 4.77,
        })),
    '486dx2-66': MachineProfile(
        '486dx2-66', '486DX2-66', 'i486DX2', 66_000_000,
        'i486', 2.0, 33_000_000, 8_333_333,
        video_adapter='VLB', memory_wait_states=2,
        io_wait_states=2, vram_wait_cycles=2, cache_enabled=True,
        benchmark_targets=MappingProxyType({'landmark_mhz': 66.0})),
})


def get_machine_profile(profile_id):
    """Resolve a profile ID, raising a useful error for unknown targets."""
    try:
        return MACHINE_PROFILES[profile_id]
    except KeyError as exc:
        choices = ', '.join(MACHINE_PROFILES)
        raise ValueError(f'unknown machine profile {profile_id!r}; '
                         f'choose from: {choices}') from exc
