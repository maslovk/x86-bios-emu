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
locks, and key-press callbacks inject bytes directly into the keyboard
controller with no marshalling.

Rendering uses the canonical Pango + PangoCairo path so font fallback to
monospace works across platforms; the CGA 16-colour palette is replicated
exactly (foreground = attr low nibble, background = attr high nibble).
"""

import sys


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

# GW-BASIC displays these command macros on its bottom status line.  They are
# inserted without Enter, matching the original interface: the user can edit
# the command (for example, add a filename after LOAD ") and then press Enter.
_GWBASIC_FUNCTION_KEYS = {
    1: 'LIST ', 2: 'RUN', 3: 'LOAD "', 4: 'SAVE "', 5: 'CONT',
    6: 'LPRINT', 7: 'TRON', 8: 'TROFF', 9: 'KEY', 10: 'SCREEN',
}

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
        Callback invoked once per keypress with the ASCII byte to inject.
        Pass ``None`` to ignore keyboard input.
    on_close : callable() | None
        Called once when the user closes the window; the loop should then
        stop (``pump()`` also returns True after this point).
    on_reset : callable() | None
        Called by the Reset button or Ctrl+R.  The emulator owns the reset
        operation so the display remains independent of CPU/device details.
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
                 font_size=18, title="Simple BIOS Emulator — VGA Text"):
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
        self.on_close = on_close
        self.on_reset = on_reset
        self.on_refresh = on_refresh
        self.on_eject = on_eject
        self.close_warning = close_warning
        self.stop = False        # set when window closed -> loop should exit
        self.font_size = font_size
        self.cursor_visible = True
        self._cursor_timer = None

        # --- window + drawing area ---
        self.window = Gtk.Window()
        self.window.set_title(title)
        self.window.connect('delete-event', self._on_delete)
        self.window.connect('destroy', self._on_destroy)
        self.window.connect('key-press-event', self._on_key_press)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.connect('draw', self._on_draw)

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
        self.media_label = Gtk.Label(label=media_status)
        self.media_label.set_xalign(0.0)
        self.session_label = Gtk.Label(label='Starting')
        self.session_label.set_xalign(1.0)
        controls.pack_start(reset, False, False, 0)
        controls.pack_start(refresh, False, False, 0)
        controls.pack_start(eject, False, False, 0)
        controls.pack_start(paste, False, False, 0)
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
        self.drawing_area.set_size_request(self.width_px, self.height_px)
        self.window.set_default_size(self.width_px, self.height_px + 34)
        self.window.set_resizable(False)

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

    def _on_destroy(self, _widget):
        self.stop = True
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass

    def _on_key_press(self, _widget, event):
        Gdk = self._Gdk
        keyval = event.keyval
        ch = Gdk.keyval_to_unicode(keyval)
        # Printable ASCII -> inject directly.  This is the path DOS's
        # DATE/TIME prompts use; injecting the ASCII byte (not a scan code)
        # keeps INT 16h AH=00 returning the exact typed character.
        if 0x20 <= ch <= 0x7E and not (event.state & Gdk.ModifierType.CONTROL_MASK):
            self._emit(ch)
            return True
        # Special keys that map to control characters DOS understands.
        specials = {
            Gdk.KEY_Return: 0x0D,
            Gdk.KEY_KP_Enter: 0x0D,
            Gdk.KEY_BackSpace: 0x08,
            Gdk.KEY_Escape: 0x1B,
            Gdk.KEY_Tab: 0x09,
            Gdk.KEY_ISO_Left_Tab: 0x09,
        }
        if keyval in specials:
            self._emit(specials[keyval])
            return True
        function_keys = {
            Gdk.KEY_F1: 1, Gdk.KEY_F2: 2, Gdk.KEY_F3: 3,
            Gdk.KEY_F4: 4, Gdk.KEY_F5: 5, Gdk.KEY_F6: 6,
            Gdk.KEY_F7: 7, Gdk.KEY_F8: 8, Gdk.KEY_F9: 9,
            Gdk.KEY_F10: 10,
        }
        function_number = function_keys.get(keyval)
        if function_number is not None:
            for byte in _GWBASIC_FUNCTION_KEYS[function_number].encode('ascii'):
                self._emit(byte)
            return True
        # Ctrl+C as a graceful "stop the emulator" shortcut.
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and keyval in (
                Gdk.KEY_c, Gdk.KEY_C):
            self.stop = True
            return True
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and keyval in (
                Gdk.KEY_r, Gdk.KEY_R):
            self._on_reset_clicked(None)
            return True
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and keyval in (
                Gdk.KEY_v, Gdk.KEY_V):
            self._on_paste_clicked(None)
            return True
        return False

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
        for y in range(video.height):
            row = video.buffer[y]
            for x in range(video.width):
                byte, attr = row[x]
                fg = attr & 0xF
                bg = (attr >> 4) & 0xF
                # Background fill.
                r, g, b = _CGA_RGB[bg]
                cr.set_source_rgb(r / 255.0, g / 255.0, b / 255.0)
                cr.rectangle(x * cw, y * ch, cw, ch)
                cr.fill()
                # Glyph (skip for blank cells to save Pango work).
                if 0x20 <= byte <= 0x7E:
                    layout.set_text(chr(byte), -1)
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
