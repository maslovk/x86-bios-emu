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
    assert display._on_key_release(
        None, event(gdk.KEY_f, gdk.ModifierType.MOD1_MASK))
    assert display._on_key_release(None, event(gdk.KEY_Alt_L))

    assert controller.read_key_event() == (0x21, 0)
    assert controller.alt is False


def test_standalone_alt_emits_physical_make_and_break():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append
    gdk = display._Gdk

    assert display._on_key_press(None, event(gdk.KEY_Alt_L))
    assert display._on_key_release(None, event(gdk.KEY_Alt_L))

    assert scans == [0x38, 0xB8]


def test_f5_is_a_function_key_not_a_text_macro():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    assert display._on_key_press(None, event(display._Gdk.KEY_F5))
    assert display._on_key_release(None, event(display._Gdk.KEY_F5))

    assert scans == [0x3F, 0xBF]


def test_ctrl_c_is_delivered_to_dos():
    display = make_display()
    controller = KeyboardController()
    display.on_scan_code = controller.inject_scan_code
    gdk = display._Gdk

    display._on_key_press(None, event(gdk.KEY_Control_L))
    display._on_key_press(
        None, event(gdk.KEY_c, gdk.ModifierType.CONTROL_MASK))
    display._on_key_release(
        None, event(gdk.KEY_c, gdk.ModifierType.CONTROL_MASK))
    display._on_key_release(None, event(gdk.KEY_Control_L))

    assert controller.read_key_event() == (0x2E, 0x03)
    assert display.stop is False


def test_arrow_key_emits_physical_enhanced_make_and_break():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    assert display._on_key_press(None, event(display._Gdk.KEY_Left))
    assert display._on_key_release(None, event(display._Gdk.KEY_Left))

    assert scans == [0xE0, 0x4B, 0xE0, 0xCB]


def test_arrow_auto_repeat_repeats_make_until_one_break():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    display._on_key_press(None, event(display._Gdk.KEY_Down))
    display._on_key_press(None, event(display._Gdk.KEY_Down))
    display._on_key_release(None, event(display._Gdk.KEY_Down))

    assert scans == [0xE0, 0x50, 0xE0, 0x50, 0xE0, 0xD0]


def test_ctrl_left_uses_physical_modifier_and_navigation_scans():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append
    gdk = display._Gdk

    display._on_key_press(None, event(gdk.KEY_Control_L))
    display._on_key_press(
        None, event(gdk.KEY_Left, gdk.ModifierType.CONTROL_MASK))
    display._on_key_release(
        None, event(gdk.KEY_Left, gdk.ModifierType.CONTROL_MASK))
    display._on_key_release(None, event(gdk.KEY_Control_L))

    assert scans == [0x1D, 0xE0, 0x4B, 0xE0, 0xCB, 0x9D]


def test_shift_tab_uses_scan_event_instead_of_plain_tab():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    display._on_key_press(
        None, event(display._Gdk.KEY_ISO_Left_Tab,
                    display._Gdk.ModifierType.SHIFT_MASK))
    display._on_key_release(
        None, event(display._Gdk.KEY_ISO_Left_Tab,
                    display._Gdk.ModifierType.SHIFT_MASK))

    assert scans == [0x0F, 0x8F]


def test_keypad_digit_retains_keypad_scan_code():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    display._on_key_press(None, event(display._Gdk.KEY_KP_1))
    display._on_key_release(None, event(display._Gdk.KEY_KP_1))

    assert scans == [0x4F, 0xCF]


def test_keypad_divide_retains_e0_prefix():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    display._on_key_press(None, event(display._Gdk.KEY_KP_Divide))
    display._on_key_release(None, event(display._Gdk.KEY_KP_Divide))

    assert scans == [0xE0, 0x35, 0xE0, 0xB5]


def test_printable_key_uses_8042_make_and_break_path():
    display = make_display()
    direct_ascii = []
    scans = []
    display.on_key = direct_ascii.append
    display.on_scan_code = scans.append

    assert display._on_key_press(None, event(display._Gdk.KEY_a))
    assert display._on_key_release(None, event(display._Gdk.KEY_a))

    assert direct_ascii == []
    assert scans == [0x1E, 0x9E]


def test_shifted_printable_is_translated_by_keyboard_controller():
    display = make_display()
    controller = KeyboardController()
    display.on_scan_code = controller.inject_scan_code
    gdk = display._Gdk

    display._on_key_press(None, event(gdk.KEY_Shift_L))
    display._on_key_press(
        None, event(gdk.KEY_A, gdk.ModifierType.SHIFT_MASK))

    assert controller.read_key_event() == (0x1E, ord('A'))

    display._on_key_release(
        None, event(gdk.KEY_A, gdk.ModifierType.SHIFT_MASK))
    display._on_key_release(None, event(gdk.KEY_Shift_L))
    assert controller.shift is False


def test_num_lock_off_keypad_navigation_stays_on_keypad():
    display = make_display()
    scans = []
    display.on_scan_code = scans.append

    display._on_key_press(None, event(display._Gdk.KEY_KP_Left))
    display._on_key_release(None, event(display._Gdk.KEY_KP_Left))

    assert scans == [0x4B, 0xCB]
