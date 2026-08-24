"""GTK-independent tests for host-key to PC-keyboard translation."""

from types import SimpleNamespace

from gtdisplay import GtkDisplay
from hardware import KeyboardController


class FakeGdk:
    class ModifierType:
        CONTROL_MASK = 0x01
        MOD1_MASK = 0x02
        SHIFT_MASK = 0x04

    def __getattr__(self, name):
        if name.startswith('KEY_'):
            return name
        raise AttributeError(name)

    @staticmethod
    def keyval_to_unicode(keyval):
        name = str(keyval)
        suffix = name[4:] if name.startswith('KEY_') else name
        return ord(suffix) if len(suffix) == 1 else 0


def make_display():
    display = GtkDisplay.__new__(GtkDisplay)
    display._Gdk = FakeGdk()
    display.on_key = None
    display.on_extended_key = None
    display.on_scan_code = None
    display.stop = False
    return display


def event(keyval, state=0):
    return SimpleNamespace(keyval=keyval, state=state)


def test_alt_f_reaches_bios_as_alt_key_event():
    display = make_display()
    controller = KeyboardController()
    display.on_scan_code = controller.inject_scan_code
    gdk = display._Gdk

    assert display._on_key_press(None, event(gdk.KEY_Alt_L))
    assert display._on_key_press(
        None, event(gdk.KEY_f, gdk.ModifierType.MOD1_MASK))
    assert display._on_key_release(None, event(gdk.KEY_Alt_L))

    assert controller.read_key_event() == (0x21, 0)
    assert controller.alt is False


def test_f5_is_a_function_key_not_a_text_macro():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    assert display._on_key_press(None, event(display._Gdk.KEY_F5))

    assert scans == [0x3F, 0xBF]


def test_ctrl_c_is_delivered_to_dos():
    display = make_display()
    controller = KeyboardController()
    display.on_scan_code = controller.inject_scan_code
    gdk = display._Gdk

    display._on_key_press(None, event(gdk.KEY_Control_L))
    display._on_key_press(
        None, event(gdk.KEY_c, gdk.ModifierType.CONTROL_MASK))
    display._on_key_release(None, event(gdk.KEY_Control_L))

    assert controller.read_key_event() == (0x2E, 0x03)
    assert display.stop is False


def test_arrow_key_keeps_enhanced_bios_event_path():
    display = make_display()
    scans = []
    display.on_extended_key = scans.append

    assert display._on_key_press(None, event(display._Gdk.KEY_Left))

    assert scans == [0x4B]


def test_ctrl_left_uses_bios_word_navigation_scan():
    display = make_display()
    scans = []
    display.on_extended_key = scans.append
    gdk = display._Gdk

    display._on_key_press(
        None, event(gdk.KEY_Left, gdk.ModifierType.CONTROL_MASK))

    assert scans == [0x73]


def test_shift_tab_uses_scan_event_instead_of_plain_tab():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    display._on_key_press(
        None, event(display._Gdk.KEY_ISO_Left_Tab,
                    display._Gdk.ModifierType.SHIFT_MASK))

    assert scans == [0x0F, 0x8F]
