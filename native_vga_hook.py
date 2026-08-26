"""Build and install the optional compiled planar-VGA Unicorn hook."""

import ctypes
import hashlib
from pathlib import Path
import subprocess
import tempfile


class _State(ctypes.Structure):
    _fields_ = [('planes', ctypes.POINTER(ctypes.c_ubyte) * 4),
               ('seq', ctypes.POINTER(ctypes.c_ubyte)),
               ('gdc', ctypes.POINTER(ctypes.c_ubyte)),
               ('latches', ctypes.POINTER(ctypes.c_ubyte)),
               ('active', ctypes.POINTER(ctypes.c_ubyte)),
               ('dirty', ctypes.POINTER(ctypes.c_ubyte))]


class _BlockState(ctypes.Structure):
    _fields_ = [('address', ctypes.c_uint64), ('size', ctypes.c_uint32)]


def _pointer(buffer):
    return ctypes.cast((ctypes.c_ubyte * len(buffer)).from_buffer(buffer),
                       ctypes.POINTER(ctypes.c_ubyte))


def _library():
    source = Path(__file__).with_suffix('.c')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    output = Path(tempfile.gettempdir()) / ('x86-vga-hook-' + digest + '.so')
    if not output.exists():
        import unicorn
        package = Path(unicorn.__file__).resolve().parent
        command = ['cc', '-shared', '-O3', '-fPIC', str(source), '-o', str(output),
                   '-I', str(package / 'include'), '-L', str(package / 'lib'),
                   '-Wl,-rpath,' + str(package / 'lib'), '-lunicorn']
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    library = ctypes.CDLL(str(output))
    library.x86_vga_install.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(_State),
                                        ctypes.POINTER(ctypes.c_size_t)]
    library.x86_vga_install.restype = ctypes.c_int
    library.x86_block_install.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(_BlockState),
                                          ctypes.POINTER(ctypes.c_size_t)]
    library.x86_block_install.restype = ctypes.c_int
    return library


def install(uc, video):
    """Return keep-alive state if the hook installed, otherwise ``None``."""
    try:
        library = _library()
        buffers = list(video.graphics_planes) + [video.seq_regs, video.gdc_regs,
                   video.graphics_latches, video.native_graphics_active]
        dirty = bytearray((0,))
        state = _State()
        for index, plane in enumerate(video.graphics_planes):
            state.planes[index] = _pointer(plane)
        state.seq, state.gdc, state.latches, state.active = map(_pointer, buffers[4:])
        state.dirty = _pointer(dirty)
        hook = ctypes.c_size_t()
        handle = getattr(uc, '_uch', None)
        if not handle or library.x86_vga_install(handle, ctypes.byref(state),
                                                  ctypes.byref(hook)) != 0:
            return None
        return library, state, buffers, dirty, hook
    except (OSError, subprocess.SubprocessError):
        return None


def install_block_recorder(uc):
    """Install a native basic-block recorder, returning keep-alive state."""
    try:
        library = _library()
        state = _BlockState()
        hook = ctypes.c_size_t()
        handle = getattr(uc, '_uch', None)
        if not handle or library.x86_block_install(handle, ctypes.byref(state),
                                                    ctypes.byref(hook)) != 0:
            return None
        return library, state, hook
    except (OSError, subprocess.SubprocessError):
        return None
