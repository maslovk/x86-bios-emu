"""DOS CP437 glyph rendering helpers."""

from video import decode_vga_char


def test_decodes_dos_box_drawing_glyphs():
    assert decode_vga_char(0xC4) == '─'
    assert decode_vga_char(0xCD) == '═'
    assert decode_vga_char(0xDA) == '┌'
    assert decode_vga_char(0xB3) == '│'


def test_keeps_non_ui_high_bytes_blank():
    assert decode_vga_char(0x00) == ' '
    assert decode_vga_char(0x9B) == ' '
