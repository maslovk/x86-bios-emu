"""GTK display for the BIOS emulator VGA text mode.

Replaces the terminal-rendered box + ANSI escape output with a real GUI
window that does proper keyboard capture.  This sidesteps the cbreak-mode
and scan-code/ASCII remapping issues that make typing into DOS COMMAND.COM's
DATE/TIME prompts unreliable on a stock terminal.

Architecture
------------
The emulator loop runs in the main thread; between batches of CPU
instructions it calls ``GtkDisplay.pump()``, which:

  1. queues a redraw of the 80x25 VGA cell grid, and
  2. drains pending Gtk events non-blockingly
     (``Gtk.events_pending()`` + ``Gtk.main_iteration_do(False)``).

Because everything runs in the main thread, there is no GIL dance, no
locks, and key callbacks inject physical set-1 make/break sequences directly
into the keyboard controller with no marshalling.

Rendering uses the canonical Pango + PangoCairo path so font fallback to
monospace works across platforms; the CGA 16-colour palette is replicated
exactly (foreground = attr low nibble, background = attr high nibble).
"""

import sys

from video import decode_vga_char


# CGA 16-colour palette, RGB 0-255 each.  Index = attr nibble value.
_CGA_RGB = [
    (0x00, 0x00, 0x00),   # 0  black
    (0x00, 0x00, 0xAA),   # 1  blue
    (0x00, 0xAA, 0x00),   # 2  green
    (0x00, 0xAA, 0xAA),   # 3  cyan
    (0xAA, 0x00, 0x00),   # 4  red
    (0xAA, 0x00, 0xAA),   # 5  magenta
    (0xAA, 0x55, 0x00),   # 6  brown
    (0xAA, 0xAA, 0xAA),   # 7  light grey
    (0x55, 0x55, 0x55),   # 8  dark grey
    (0x55, 0x55, 0xFF),   # 9  light blue
    (0x55, 0xFF, 0x55),   # 10 light green
    (0x55, 0xFF, 0xFF),   # 11 light cyan
    (0xFF, 0x55, 0x55),   # 12 light red
    (0xFF, 0x55, 0xFF),   # 13 light magenta
    (0xFF, 0xFF, 0x55),   # 14 yellow
    (0xFF, 0xFF, 0xFF),   # 15 bright white
]

_FUNCTION_KEY_SCANS = {
    1: 0x3B, 2: 0x3C, 3: 0x3D, 4: 0x3E, 5: 0x3F, 6: 0x40,
    7: 0x41, 8: 0x42, 9: 0x43, 10: 0x44, 11: 0x57, 12: 0x58,
}

_SET1_CHAR_KEYS = {
    '`~': 0x29, '1!': 0x02, '2@': 0x03, '3#': 0x04,
    '4$': 0x05, '5%': 0x06, '6^': 0x07, '7&': 0x08,
    '8*': 0x09, '9(': 0x0A, '0)': 0x0B, '-_': 0x0C,
    '=+': 0x0D, 'qQ': 0x10, 'wW': 0x11, 'eE': 0x12,
    'rR': 0x13, 'tT': 0x14, 'yY': 0x15, 'uU': 0x16,
    'iI': 0x17, 'oO': 0x18, 'pP': 0x19, '[{': 0x1A,
    ']}': 0x1B, 'aA': 0x1E, 'sS': 0x1F, 'dD': 0x20,
    'fF': 0x21, 'gG': 0x22, 'hH': 0x23, 'jJ': 0x24,
    'kK': 0x25, 'lL': 0x26, ';:': 0x27, "'\"": 0x28,
    '\\|': 0x2B, 'zZ': 0x2C, 'xX': 0x2D, 'cC': 0x2E,
    'vV': 0x2F, 'bB': 0x30, 'nN': 0x31, 'mM': 0x32,
    ',<': 0x33, '.>': 0x34, '/?': 0x35, ' ': 0x39,
}


def _set1_scan_for_char(ch):
    """Return the physical set-1 key for a printable host character."""
    for characters, scan_code in _SET1_CHAR_KEYS.items():
        if ch in characters:
            return scan_code
    return None

# IBM CGA cursor blink toggles every 16 display fields.  The standard CGA
# refresh is 59.92 Hz, so each on/off transition is 16 / 59.92 = 267 ms.
# GTK timers are wall-clock based, but this preserves the real-machine rate.
CURSOR_BLINK_INTERVAL_MS = 267


class GtkDisplay:
    """A GTK window that renders the emulator's VGA text buffer.

    Parameters
    ----------
    video : video.Video
        The shared VGA model.  ``pump()`` calls ``_sync_from_memory()``
        before each redraw so the displayed grid reflects whatever DOS has
        written into 0xB8000.
    on_key : callable(int) | None
        Callback used for direct text injection such as host clipboard paste.
        Physical keypresses use ``on_scan_code`` instead.
    on_extended_key : callable(int) | None
        Callback invoked for enhanced keys such as the arrow keys with their
        IBM PC/AT set-1 scan code. Pass ``None`` to ignore enhanced keys.
    on_scan_code : callable(int) | None
        Callback invoked for raw set-1 make/break bytes. Modifier chords and
        function keys use this path so DOS receives normal BIOS key events.
    on_close : callable() | None
        Called once when the user closes the window; the loop should then
        stop (``pump()`` also returns True after this point).
    on_reset : callable() | None
        Called by the Reset button or Ctrl+Shift+R. The emulator owns the
        reset operation so the display remains independent of CPU details.
    on_refresh : callable() | None
        Called by the Refresh B: button to rebuild host-folder media.
    on_eject : callable() | None
        Called by the Eject B: button to detach host-folder media.
    media_status : str
        Short media summary shown below the VGA grid.
    font_size : int
        Pango font point size.  Cell width/height are derived from this by
        measuring an 'M' via Pango, so the rendered grid is always aligned.
    title : str
        Window title.
    """

    def __init__(self, video, on_key=None, on_close=None, on_reset=None,
                 on_refresh=None, on_eject=None,
                 close_warning=None,
                 media_status="A: none  B: none  C: none",
                 font_size=18, title="Simple BIOS Emulator — VGA Text",
                 on_extended_key=None, on_scan_code=None):
        # Lazy import so ``main.py`` can be imported without GTK installed
        # (e.g. in CI / test runs).  Only --gtk actually needs gi.
        try:
            import gi
            gi.require_version('Gtk', '3.0')
            gi.require_version('PangoCairo', '1.0')
            from gi.repository import Gtk, Gdk, Pango, PangoCairo, GLib
        except (ImportError, ValueError) as e:
            raise RuntimeError(
                "GTK display requires PyGObject + Gtk 3 + PangoCairo. "
                f"Install with your OS package manager (e.g. "
                f"'apt install python3-gi gir1.2-gtk-3.0').  Original error: {e}"
            ) from e

        self._Gtk = Gtk
        self._Gdk = Gdk
        self._Pango = Pango
        self._PangoCairo = PangoCairo
        self._GLib = GLib

        self.video = video
        self.on_key = on_key
        self.on_extended_key = on_extended_key
        self.on_scan_code = on_scan_code
        self.on_close = on_close
        self.on_reset = on_reset
        self.on_refresh = on_refresh
        self.on_eject = on_eject
        self.close_warning = close_warning
        self.stop = False        # set when window closed -> loop should exit
        self.font_size = font_size
        self.cursor_visible = True
        self._cursor_timer = None
        self.fullscreen = False
        self.selection_start = None
        self.selection_end = None

        # --- window + drawing area ---
        self.window = Gtk.Window()
        self.window.set_title(title)
        self.window.connect('delete-event', self._on_delete)
        self.window.connect('destroy', self._on_destroy)
        self.window.connect('window-state-event', self._on_window_state)
        self.window.connect('key-press-event', self._on_key_press)
        self.window.connect('key-release-event', self._on_key_release)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.connect('draw', self._on_draw)
        self.drawing_area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.BUTTON_RELEASE_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK)
        self.drawing_area.connect('button-press-event', self._on_button_press)
        self.drawing_area.connect('motion-notify-event', self._on_motion)
        self.drawing_area.connect('button-release-event', self._on_button_release)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_border_width(4)
        reset = Gtk.Button.new_with_label('Reset')
        reset.connect('clicked', self._on_reset_clicked)
        refresh = Gtk.Button.new_with_label('Refresh B:')
        refresh.connect('clicked', self._on_refresh_clicked)
        eject = Gtk.Button.new_with_label('Eject B:')
        eject.connect('clicked', self._on_eject_clicked)
        paste = Gtk.Button.new_with_label('Paste')
        paste.connect('clicked', self._on_paste_clicked)
        copy = Gtk.Button.new_with_label('Copy')
        copy.connect('clicked', self._on_copy_clicked)
        fullscreen = Gtk.Button.new_with_label('Fullscreen')
        fullscreen.connect('clicked', self._on_fullscreen_clicked)
        self.media_label = Gtk.Label(label=media_status)
        self.media_label.set_xalign(0.0)
        self.session_label = Gtk.Label(label='Starting')
        self.session_label.set_xalign(1.0)
        controls.pack_start(reset, False, False, 0)
        controls.pack_start(refresh, False, False, 0)
        controls.pack_start(eject, False, False, 0)
        controls.pack_start(paste, False, False, 0)
        controls.pack_start(copy, False, False, 0)
        controls.pack_start(fullscreen, False, False, 0)
        controls.pack_start(self.media_label, True, True, 0)
        controls.pack_start(self.session_label, False, False, 0)

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        layout.pack_start(self.drawing_area, True, True, 0)
        layout.pack_start(controls, False, False, 0)
        self.window.add(layout)

        # --- measure cell size from the font so the grid is always aligned ---
        self.font_desc = Pango.FontDescription.from_string(
            f"monospace {font_size}")
        probe = self.drawing_area.create_pango_layout('M')
        probe.set_font_description(self.font_desc)
        pw, ph = probe.get_pixel_size()
        # +1/+3 give a tiny bit of inter-cell padding so glyphs never touch.
        self.cell_w = max(1, pw) + 1
        self.cell_h = max(1, ph) + 3

        self.width_px = self.cell_w * video.width
        self.height_px = self.cell_h * video.height
        self.window.set_default_size(self.width_px, self.height_px + 34)
        self.window.set_resizable(True)

        # Reusable layout for per-cell glyph drawing (text swapped each draw).
        self._layout = self.drawing_area.create_pango_layout('')
        self._layout.set_font_description(self.font_desc)

        self.window.show_all()
        # Keep the cursor blink in GTK's event loop, independent of guest speed.
        self._cursor_timer = GLib.timeout_add(
            CURSOR_BLINK_INTERVAL_MS, self._blink_cursor)

    # ── Gtk signal handlers ────────────────────────────────────────

    def _on_delete(self, _widget, _event):
        if not self.close_warning:
            return False
        message = self.close_warning()
        if not message:
            return False
        dialog = self._Gtk.MessageDialog(
            transient_for=self.window,
            flags=self._Gtk.DialogFlags.MODAL,
            message_type=self._Gtk.MessageType.WARNING,
            buttons=self._Gtk.ButtonsType.CANCEL,
            text=message)
        dialog.add_button('Close anyway', self._Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        return response != self._Gtk.ResponseType.OK

    def _on_window_state(self, _widget, event):
        self.fullscreen = bool(
            event.new_window_state & self._Gdk.WindowState.FULLSCREEN)
        return False

    def _on_destroy(self, _widget):
        self.stop = True
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass

    def _physical_scan_for_key(self, keyval):
        """Return the set-1 make sequence for a host key, if representable."""
        Gdk = self._Gdk
        keypad_keys = {
            Gdk.KEY_KP_7: (0x47,), Gdk.KEY_KP_8: (0x48,),
            Gdk.KEY_KP_9: (0x49,), Gdk.KEY_KP_Subtract: (0x4A,),
            Gdk.KEY_KP_4: (0x4B,), Gdk.KEY_KP_5: (0x4C,),
            Gdk.KEY_KP_6: (0x4D,), Gdk.KEY_KP_Add: (0x4E,),
            Gdk.KEY_KP_1: (0x4F,), Gdk.KEY_KP_2: (0x50,),
            Gdk.KEY_KP_3: (0x51,), Gdk.KEY_KP_0: (0x52,),
            Gdk.KEY_KP_Decimal: (0x53,), Gdk.KEY_KP_Multiply: (0x37,),
            Gdk.KEY_KP_Divide: (0xE0, 0x35),
            Gdk.KEY_KP_Enter: (0xE0, 0x1C),
        }
        sequence = keypad_keys.get(keyval)
        if sequence is not None:
            return sequence

        special_keys = {
            Gdk.KEY_Return: (0x1C,), Gdk.KEY_BackSpace: (0x0E,),
            Gdk.KEY_Escape: (0x01,), Gdk.KEY_Tab: (0x0F,),
            Gdk.KEY_ISO_Left_Tab: (0x0F,),
        }
        sequence = special_keys.get(keyval)
        if sequence is not None:
            return sequence

        function_keys = {
            Gdk.KEY_F1: 1, Gdk.KEY_F2: 2, Gdk.KEY_F3: 3,
            Gdk.KEY_F4: 4, Gdk.KEY_F5: 5, Gdk.KEY_F6: 6,
            Gdk.KEY_F7: 7, Gdk.KEY_F8: 8, Gdk.KEY_F9: 9,
            Gdk.KEY_F10: 10, Gdk.KEY_F11: 11, Gdk.KEY_F12: 12,
        }
        function_number = function_keys.get(keyval)
        if function_number is not None:
            return (_FUNCTION_KEY_SCANS[function_number],)

        ch = Gdk.keyval_to_unicode(keyval)
        if 0x20 <= ch <= 0x7E:
            scan_code = _set1_scan_for_char(chr(ch))
            if scan_code is not None:
                return (scan_code,)
        return None

    @staticmethod
    def _break_sequence(make_sequence):
        if make_sequence[0] == 0xE0:
            return 0xE0, make_sequence[1] | 0x80
        return (make_sequence[0] | 0x80,)

    def _on_key_press(self, _widget, event):
        Gdk = self._Gdk
        keyval = event.keyval
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)

        modifier_scans = {
            Gdk.KEY_Shift_L: (0x2A,), Gdk.KEY_Shift_R: (0x36,),
            Gdk.KEY_Control_L: (0x1D,), Gdk.KEY_Control_R: (0xE0, 0x1D),
            Gdk.KEY_Alt_L: (0x38,), Gdk.KEY_Alt_R: (0xE0, 0x38),
            Gdk.KEY_Caps_Lock: (0x3A,), Gdk.KEY_Num_Lock: (0x45,),
            Gdk.KEY_Scroll_Lock: (0x46,),
        }
        modifier = modifier_scans.get(keyval)
        if modifier is not None:
            self._emit_scan_sequence(modifier)
            return True

        # Reserve Ctrl+Shift host shortcuts; unshifted Ctrl combinations must
        # reach DOS (Ctrl+C is BREAK and Ctrl+V is meaningful in editors).
        if ctrl and shift and keyval in (Gdk.KEY_c, Gdk.KEY_C):
            if self._copy_selection():
                return True
            self.stop = True
            return True
        if ctrl and shift and keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self._on_reset_clicked(None)
            return True
        if ctrl and shift and keyval in (Gdk.KEY_v, Gdk.KEY_V):
            self._on_paste_clicked(None)
            return True
        if ctrl and shift and keyval == Gdk.KEY_F11:
            self._toggle_fullscreen()
            return True

        # Representable PC keys travel through the same set-1 make/break and
        # 8042 translation path as a physical keyboard. Paste deliberately
        # remains direct text injection because it is a host convenience.
        make_sequence = self._physical_scan_for_key(keyval)
        if make_sequence is not None:
            self._emit_scan_sequence(make_sequence)
            return True
        # Enhanced keys are delivered to DOS as scan codes with an ASCII
        # value of zero.  Setup's menus use the Up/Down arrows to select
        # actions (for example, Exit versus allocating a disk); forwarding
        # only an ASCII byte drops these keys before INT 16h can see them.
        extended_keys = {
            Gdk.KEY_Up: 0x48,
            Gdk.KEY_Down: 0x50,
            Gdk.KEY_Left: 0x4B,
            Gdk.KEY_Right: 0x4D,
            Gdk.KEY_Home: 0x47,
            Gdk.KEY_End: 0x4F,
            Gdk.KEY_Page_Up: 0x49,
            Gdk.KEY_Page_Down: 0x51,
            Gdk.KEY_Insert: 0x52,
            Gdk.KEY_Delete: 0x53,
            Gdk.KEY_KP_Up: 0x48,
            Gdk.KEY_KP_Down: 0x50,
            Gdk.KEY_KP_Left: 0x4B,
            Gdk.KEY_KP_Right: 0x4D,
            Gdk.KEY_KP_Home: 0x47,
            Gdk.KEY_KP_End: 0x4F,
            Gdk.KEY_KP_Page_Up: 0x49,
            Gdk.KEY_KP_Page_Down: 0x51,
            Gdk.KEY_KP_Insert: 0x52,
            Gdk.KEY_KP_Delete: 0x53,
        }
        scan_code = extended_keys.get(keyval)
        if scan_code is not None:
            ctrl_navigation = {
                0x47: 0x77, 0x48: 0x8D, 0x49: 0x84,
                0x4B: 0x73, 0x4D: 0x74, 0x4F: 0x75,
                0x50: 0x91, 0x51: 0x76, 0x52: 0x92, 0x53: 0x93,
            }
            alt_navigation = {
                0x47: 0x97, 0x48: 0x98, 0x49: 0x99,
                0x4B: 0x9B, 0x4D: 0x9D, 0x4F: 0x9F,
                0x50: 0xA0, 0x51: 0xA1, 0x52: 0xA2, 0x53: 0xA3,
            }
            if alt:
                scan_code = alt_navigation[scan_code]
            elif ctrl:
                scan_code = ctrl_navigation[scan_code]
            self._emit_extended(scan_code)
            return True
        return False

    def _on_key_release(self, _widget, event):
        Gdk = self._Gdk
        modifier_breaks = {
            Gdk.KEY_Shift_L: (0xAA,), Gdk.KEY_Shift_R: (0xB6,),
            Gdk.KEY_Control_L: (0x9D,),
            Gdk.KEY_Control_R: (0xE0, 0x9D),
            Gdk.KEY_Alt_L: (0xB8,), Gdk.KEY_Alt_R: (0xE0, 0xB8),
            Gdk.KEY_Caps_Lock: (0xBA,), Gdk.KEY_Num_Lock: (0xC5,),
            Gdk.KEY_Scroll_Lock: (0xC6,),
        }
        sequence = modifier_breaks.get(event.keyval)
        if sequence is not None:
            self._emit_scan_sequence(sequence)
            return True

        make_sequence = self._physical_scan_for_key(event.keyval)
        if make_sequence is not None:
            self._emit_scan_sequence(self._break_sequence(make_sequence))
            return True
        return False

    def _cell_at(self, x, y):
        allocation = self.drawing_area.get_allocation()
        scale = min(allocation.width / self.width_px,
                    allocation.height / self.height_px)
        scale = max(0.1, scale)
        x = (x - (allocation.width - self.width_px * scale) / 2) / scale
        y = (y - (allocation.height - self.height_px * scale) / 2) / scale
        col = max(0, min(self.video.width - 1, int(x / self.cell_w)))
        row = max(0, min(self.video.height - 1, int(y / self.cell_h)))
        return col, row

    def _on_button_press(self, _widget, event):
        if event.button == 1:
            self.selection_start = self._cell_at(event.x, event.y)
            self.selection_end = self.selection_start
            self.drawing_area.queue_draw()
            return True
        return False

    def _on_motion(self, _widget, event):
        if self.selection_start is not None and event.state & self._Gdk.ModifierType.BUTTON1_MASK:
            self.selection_end = self._cell_at(event.x, event.y)
            self.drawing_area.queue_draw()
            return True
        return False

    def _on_button_release(self, _widget, event):
        if event.button == 1 and self.selection_start is not None:
            self.selection_end = self._cell_at(event.x, event.y)
            self.drawing_area.queue_draw()
            return True
        return False

    def _selection_cells(self):
        if self.selection_start is None or self.selection_end is None:
            return None
        (sx, sy), (ex, ey) = self.selection_start, self.selection_end
        if (sy, sx) > (ey, ex):
            sx, sy, ex, ey = ex, ey, sx, sy
        return sx, sy, ex, ey

    def _copy_selection(self):
        bounds = self._selection_cells()
        if bounds is None:
            return False
        sx, sy, ex, ey = bounds
        self.video._sync_from_memory()
        lines = []
        for row in range(sy, ey + 1):
            text = ''.join(decode_vga_char(ch)
                           for ch, _attr in self.video.buffer[row][sx:ex + 1])
            lines.append(text.rstrip())
        text = '\n'.join(lines)
        clipboard = self._Gtk.Clipboard.get(self._Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        return True

    def _on_copy_clicked(self, _button):
        self._copy_selection()

    def _on_fullscreen_clicked(self, _button):
        self._toggle_fullscreen()

    def _toggle_fullscreen(self):
        if self.fullscreen:
            self.window.unfullscreen()
        else:
            self.window.set_resizable(True)
            self.window.fullscreen()
            self.window.present()
        self.fullscreen = not self.fullscreen

    def _on_reset_clicked(self, _button):
        if self.on_reset:
            self.on_reset()

    def _on_refresh_clicked(self, _button):
        if self.on_refresh:
            self.on_refresh()

    def _on_eject_clicked(self, _button):
        if self.on_eject:
            self.on_eject()

    def _on_paste_clicked(self, _button):
        clipboard = self._Gtk.Clipboard.get(self._Gdk.SELECTION_CLIPBOARD)
        clipboard.request_text(self._on_clipboard_text)

    def _on_clipboard_text(self, _clipboard, text):
        if text:
            for byte in text.replace('\n', '\r').encode('utf-8'):
                self._emit(byte)

    def set_media_status(self, text):
        self.media_label.set_text(text)

    def set_session_status(self, text):
        self.session_label.set_text(text)

    def show_cursor(self):
        """Restore the cursor's visible phase and request a redraw."""
        self.cursor_visible = True
        self.drawing_area.queue_draw()

    def _emit(self, byte):
        if self.on_key:
            self.on_key(byte & 0xFF)

    def _emit_extended(self, scan_code):
        if self.on_extended_key:
            self.on_extended_key(scan_code & 0xFF)

    def _emit_scan_sequence(self, sequence):
        if self.on_scan_code:
            for scan_code in sequence:
                self.on_scan_code(scan_code & 0xFF)

    def _blink_cursor(self):
        """Toggle the cursor and request a redraw; stop after window close."""
        if self.stop:
            self._cursor_timer = None
            return False
        self.cursor_visible = not self.cursor_visible
        self.drawing_area.queue_draw()
        return True

    def _on_draw(self, _area, cr):
        """Render the full 80x25 grid: bg colour rect + fg glyph per cell."""
        PangoCairo = self._PangoCairo
        video = self.video
        video._sync_from_memory()
        cw, ch = self.cell_w, self.cell_h
        layout = self._layout
        allocation = _area.get_allocation()
        scale = min(allocation.width / self.width_px,
                    allocation.height / self.height_px)
        scale = max(0.1, scale)
        cr.set_antialias(0)  # pixel-stable CGA cells; avoid scaled hairlines
        cr.set_source_rgb(0.0, 0.0, 0.0)
        cr.rectangle(0, 0, allocation.width, allocation.height)
        cr.fill()
        cr.save()
        cr.translate((allocation.width - self.width_px * scale) / 2,
                     (allocation.height - self.height_px * scale) / 2)
        cr.scale(scale, scale)
        for y in range(video.height):
            row = video.buffer[y]
            for x in range(video.width):
                byte, attr = row[x]
                fg = attr & 0xF
                bg = (attr >> 4) & 0xF
                selected = False
                bounds = self._selection_cells()
                if bounds is not None:
                    sx, sy, ex, ey = bounds
                    selected = sx <= x <= ex and sy <= y <= ey
                if selected:
                    bg, fg = 1, 15
                # Background fill.
                r, g, b = _CGA_RGB[bg]
                cr.set_source_rgb(r / 255.0, g / 255.0, b / 255.0)
                cr.rectangle(x * cw, y * ch, cw + 1, ch + 1)
                cr.fill()
                # Glyph (skip for blank cells to save Pango work).
                glyph = decode_vga_char(byte)
                if glyph != ' ':
                    layout.set_text(glyph, -1)
                    r, g, b = _CGA_RGB[fg]
                    cr.set_source_rgb(r / 255.0, g / 255.0, b / 255.0)
                    cr.move_to(x * cw + 1, y * ch)
                    PangoCairo.show_layout(cr, layout)
                if (self.cursor_visible and x == video.cur_x
                        and y == video.cur_y):
                    # A bright underline remains legible over blank cells and
                    # colored DOS text while blinking clearly.
                    cr.set_source_rgb(1.0, 1.0, 1.0)
                    # Keep the underline inside the final row; drawing it at
                    # the exact widget boundary can be clipped on row 24.
                    cr.rectangle(x * cw, y * ch + ch - 4, cw, 3)
                    cr.fill()
        cr.restore()

    # ── public API ─────────────────────────────────────────────────

    def pump(self):
        """Process pending Gtk events (keyboard, redraw, window-close) and
        return True if the window was closed (loop should exit)."""
        Gtk = self._Gtk
        self.drawing_area.queue_draw()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        return self.stop

    def close(self):
        """Destroy the window after the loop exits."""
        self.stop = True
        try:
            self.window.destroy()
        except Exception:
            pass
