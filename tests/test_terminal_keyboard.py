"""Tests for terminal byte-stream to BIOS keyboard-event decoding."""

from terminal_keyboard import ASCII, EXTENDED, TerminalKeyDecoder


def test_plain_input_normalizes_terminal_enter_and_backspace():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'ab\n\x7f', 1.0) == [
        (ASCII, ord('a')),
        (ASCII, ord('b')),
        (ASCII, 0x0D),
        (ASCII, 0x08),
    ]


def test_split_arrow_sequence_waits_for_completion():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'\x1b', 1.0) == []
    assert decoder.feed(b'[A', 1.01) == [(EXTENDED, 0x48)]


def test_multiple_sequences_and_text_can_share_one_read():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'a\x1b[A\x1b[15~b', 1.0) == [
        (ASCII, ord('a')),
        (EXTENDED, 0x48),
        (EXTENDED, 0x3F),
        (ASCII, ord('b')),
    ]


def test_navigation_and_shift_tab_sequences():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'\x1b[H\x1b[3~\x1b[6~\x1b[Z', 1.0) == [
        (EXTENDED, 0x47),
        (EXTENDED, 0x53),
        (EXTENDED, 0x51),
        (EXTENDED, 0x0F),
    ]


def test_function_key_sequence_variants():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'\x1bOP\x1b[15~\x1b[24~', 1.0) == [
        (EXTENDED, 0x3B),
        (EXTENDED, 0x3F),
        (EXTENDED, 0x58),
    ]


def test_modified_navigation_and_function_keys_use_bios_scans():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'\x1b[1;5D\x1b[15;2~\x1b[15;3~', 1.0) == [
        (EXTENDED, 0x73),  # Ctrl+Left
        (EXTENDED, 0x58),  # Shift+F5
        (EXTENDED, 0x6C),  # Alt+F5
    ]


def test_alt_printable_key_uses_physical_bios_scan():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'\x1bf\x1bF\x1b!', 1.0) == [
        (EXTENDED, 0x21),
        (EXTENDED, 0x21),
        (EXTENDED, 0x02),
    ]


def test_alt_takes_precedence_in_combined_modifier_sequences():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'\x1b[1;7D\x1b[15;8~', 1.0) == [
        (EXTENDED, 0x9B),  # Ctrl+Alt+Left
        (EXTENDED, 0x6C),  # Shift+Ctrl+Alt+F5
    ]


def test_standalone_escape_is_emitted_after_timeout():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'\x1b', 1.0) == []
    assert decoder.flush(1.02) == []
    assert decoder.flush(1.04) == [(ASCII, 0x1B)]


def test_unknown_sequence_preserves_every_input_byte():
    decoder = TerminalKeyDecoder()

    assert decoder.feed(b'\x1b[x', 1.0) == [
        (ASCII, 0x1B),
        (ASCII, ord('[')),
        (ASCII, ord('x')),
    ]
