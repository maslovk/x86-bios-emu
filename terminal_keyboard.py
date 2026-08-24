"""Decode terminal escape sequences into DOS/BIOS keyboard events."""


ASCII = 'ascii'
EXTENDED = 'extended'


_ALT_SCANS = {
    ord('1'): 0x02, ord('2'): 0x03, ord('3'): 0x04,
    ord('4'): 0x05, ord('5'): 0x06, ord('6'): 0x07,
    ord('7'): 0x08, ord('8'): 0x09, ord('9'): 0x0A,
    ord('0'): 0x0B, ord('-'): 0x0C, ord('='): 0x0D,
    ord('q'): 0x10, ord('w'): 0x11, ord('e'): 0x12,
    ord('r'): 0x13, ord('t'): 0x14, ord('y'): 0x15,
    ord('u'): 0x16, ord('i'): 0x17, ord('o'): 0x18,
    ord('p'): 0x19, ord('['): 0x1A, ord(']'): 0x1B,
    ord('a'): 0x1E, ord('s'): 0x1F, ord('d'): 0x20,
    ord('f'): 0x21, ord('g'): 0x22, ord('h'): 0x23,
    ord('j'): 0x24, ord('k'): 0x25, ord('l'): 0x26,
    ord(';'): 0x27, ord("'"): 0x28, ord('`'): 0x29,
    ord('\\'): 0x2B, ord('z'): 0x2C, ord('x'): 0x2D,
    ord('c'): 0x2E, ord('v'): 0x2F, ord('b'): 0x30,
    ord('n'): 0x31, ord('m'): 0x32, ord(','): 0x33,
    ord('.'): 0x34, ord('/'): 0x35, ord(' '): 0x39,
    0x0D: 0x1C, 0x09: 0x0F, 0x08: 0x0E,
}
_ALT_SCANS.update({
    ord('!'): 0x02, ord('@'): 0x03, ord('#'): 0x04,
    ord('$'): 0x05, ord('%'): 0x06, ord('^'): 0x07,
    ord('&'): 0x08, ord('*'): 0x09, ord('('): 0x0A,
    ord(')'): 0x0B, ord('_'): 0x0C, ord('+'): 0x0D,
    ord('{'): 0x1A, ord('}'): 0x1B, ord(':'): 0x27,
    ord('"'): 0x28, ord('~'): 0x29, ord('|'): 0x2B,
    ord('<'): 0x33, ord('>'): 0x34, ord('?'): 0x35,
})


def _function_scan(base, modifier):
    """Return the IBM BIOS scan code for a modified F-key."""
    if modifier == 2:  # Shift
        return base + 0x19 if base <= 0x44 else 0x87 + base - 0x57
    if modifier in (3, 4, 7, 8):  # Any Alt combination
        return base + 0x2D if base <= 0x44 else 0x8B + base - 0x57
    if modifier in (5, 6):  # Ctrl, Shift+Ctrl
        return base + 0x23 if base <= 0x44 else 0x89 + base - 0x57
    return base


def _navigation_scan(base, modifier):
    ctrl = {
        0x47: 0x77, 0x48: 0x8D, 0x49: 0x84,
        0x4B: 0x73, 0x4D: 0x74, 0x4F: 0x75,
        0x50: 0x91, 0x51: 0x76, 0x52: 0x92, 0x53: 0x93,
    }
    alt = {
        0x47: 0x97, 0x48: 0x98, 0x49: 0x99,
        0x4B: 0x9B, 0x4D: 0x9D, 0x4F: 0x9F,
        0x50: 0xA0, 0x51: 0xA1, 0x52: 0xA2, 0x53: 0xA3,
    }
    if modifier in (3, 4, 7, 8):
        return alt[base]
    if modifier in (5, 6):
        return ctrl[base]
    return base


def _build_sequences():
    sequences = {}
    navigation = {
        b'A': 0x48, b'B': 0x50, b'C': 0x4D, b'D': 0x4B,
        b'H': 0x47, b'F': 0x4F,
    }
    for suffix, scan in navigation.items():
        sequences[b'\x1b[' + suffix] = (EXTENDED, scan)
        sequences[b'\x1bO' + suffix] = (EXTENDED, scan)
        for modifier in range(2, 9):
            sequences[b'\x1b[1;' + str(modifier).encode() + suffix] = (
                EXTENDED, _navigation_scan(scan, modifier))

    tilde_navigation = {
        1: 0x47, 2: 0x52, 3: 0x53, 4: 0x4F,
        5: 0x49, 6: 0x51, 7: 0x47, 8: 0x4F,
    }
    for number, scan in tilde_navigation.items():
        prefix = b'\x1b[' + str(number).encode()
        sequences[prefix + b'~'] = (EXTENDED, scan)
        for modifier in range(2, 9):
            sequences[prefix + b';' + str(modifier).encode() + b'~'] = (
                EXTENDED, _navigation_scan(scan, modifier))

    function_keys = {
        b'P': 0x3B, b'Q': 0x3C, b'R': 0x3D, b'S': 0x3E,
    }
    for suffix, scan in function_keys.items():
        sequences[b'\x1bO' + suffix] = (EXTENDED, scan)
        for modifier in range(2, 9):
            sequences[b'\x1b[1;' + str(modifier).encode() + suffix] = (
                EXTENDED, _function_scan(scan, modifier))

    tilde_functions = {
        11: 0x3B, 12: 0x3C, 13: 0x3D, 14: 0x3E,
        15: 0x3F, 17: 0x40, 18: 0x41, 19: 0x42,
        20: 0x43, 21: 0x44, 23: 0x57, 24: 0x58,
    }
    for number, scan in tilde_functions.items():
        prefix = b'\x1b[' + str(number).encode()
        sequences[prefix + b'~'] = (EXTENDED, scan)
        for modifier in range(2, 9):
            sequences[prefix + b';' + str(modifier).encode() + b'~'] = (
                EXTENDED, _function_scan(scan, modifier))
    return sequences


_SEQUENCES = _build_sequences()
_SEQUENCES[b'\x1b[Z'] = (EXTENDED, 0x0F)  # Shift+Tab
_SEQUENCE_KEYS = sorted(_SEQUENCES, key=len, reverse=True)
_PREFIXES = {
    sequence[:length]
    for sequence in _SEQUENCES
    for length in range(1, len(sequence))
}


class TerminalKeyDecoder:
    """Incrementally decode xterm-compatible key sequences.

    A short timeout distinguishes a standalone Escape key from the prefix of
    an arrow/function key or Alt chord. Call :meth:`flush` from the host loop
    even when no new bytes arrive.
    """

    ESCAPE_TIMEOUT = 0.03

    def __init__(self):
        self.buffer = bytearray()
        self.escape_started = None

    def feed(self, data, now):
        if data:
            self.buffer.extend(data)
        return self._decode(force=False, now=now)

    def flush(self, now):
        force = (self.buffer and self.buffer[0] == 0x1B
                 and self.escape_started is not None
                 and now - self.escape_started >= self.ESCAPE_TIMEOUT)
        return self._decode(force=force, now=now)

    def _decode(self, force, now):
        events = []
        while self.buffer:
            if self.buffer[0] != 0x1B:
                value = self.buffer.pop(0)
                # Real terminals normally map Enter to LF and Backspace to
                # DEL.  DOS expects CR and BS respectively.
                value = {0x0A: 0x0D, 0x7F: 0x08}.get(value, value)
                events.append((ASCII, value))
                continue

            if self.escape_started is None:
                self.escape_started = now
            raw = bytes(self.buffer)
            complete = next(
                (sequence for sequence in _SEQUENCE_KEYS
                 if raw.startswith(sequence)),
                None)
            if complete is not None:
                del self.buffer[:len(complete)]
                self.escape_started = None
                events.append(_SEQUENCES[complete])
                continue
            if raw in _PREFIXES and not force:
                break

            if len(self.buffer) >= 2 and self.buffer[1] not in (ord('['), ord('O')):
                value = self.buffer[1]
                scan = _ALT_SCANS.get(value)
                if scan is None and 0x41 <= value <= 0x5A:
                    scan = _ALT_SCANS.get(value + 0x20)
                if scan is not None:
                    del self.buffer[:2]
                    self.escape_started = None
                    events.append((EXTENDED, scan))
                    continue

            if not force and (len(self.buffer) == 1 or raw in _PREFIXES):
                break

            # Unknown or timed-out sequence: preserve Escape and decode the
            # remaining bytes normally instead of swallowing user input.
            self.buffer.pop(0)
            self.escape_started = None
            events.append((ASCII, 0x1B))
        return events
