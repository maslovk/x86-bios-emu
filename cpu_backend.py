"""CPU backend selection.

The Python :class:`cpu.CPU` implementation is the reference backend and is
always available.  The optional native backend is deliberately loaded through
this small factory instead of being imported from the emulator and BIOS code;
that keeps the Python execution path complete and makes the future C ABI an
isolated implementation detail.
"""

PYTHON_BACKEND = 'python'
C_BACKEND = 'c'
BACKENDS = (PYTHON_BACKEND, C_BACKEND)


class CPUBackendError(RuntimeError):
    """Raised when a requested CPU backend cannot be constructed."""


def normalize_backend(name):
    """Validate and normalize a backend name."""
    value = PYTHON_BACKEND if name is None else str(name).strip().lower()
    if value not in BACKENDS:
        raise ValueError(
            f"unknown CPU backend {name!r}; choose 'python' or 'c'")
    return value


def create_cpu(backend, memory, io_ports):
    """Construct a CPU implementation behind the backend boundary.

    The native module is intentionally optional: normal installs and the
    complete reference test suite require no compiler or binary extension.
    The eventual module must expose ``create_cpu(memory, io_ports)`` and return
    an object implementing the CPU surface used by :mod:`main`, :mod:`bios`,
    and :mod:`dosharness` (registers, flags, ``execute()``, stack helpers, and
    interrupt state).
    """
    selected = normalize_backend(backend)
    if selected == PYTHON_BACKEND:
        from cpu import CPU
        return CPU(memory, io_ports)

    try:
        import c_cpu_native
    except ImportError as exc:
        raise CPUBackendError(
            "the C CPU backend is not built; use --cpu-backend python "
            "for the complete reference implementation") from exc

    factory = getattr(c_cpu_native, 'create_cpu', None)
    if factory is None:
        raise CPUBackendError(
            "the C CPU backend is invalid: c_cpu_native.create_cpu() "
            "is missing")
    try:
        return factory(memory, io_ports)
    except Exception as exc:
        raise CPUBackendError(
            f"the C CPU backend failed to initialize: {exc}") from exc
