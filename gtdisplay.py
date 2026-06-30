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
    font_size : int
        Pango font point size.  Cell width/height are derived from this by
        measuring an 'M' via Pango, so the rendered grid is always aligned.
    title : str
        Window title.
    """

    def __init__(self, video, on_key=None, on_close=None,
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
        self.stop = False        # set when window closed -> loop should exit
        self.font_size = font_size

        # --- window + drawing area ---
        self.window = Gtk.Window()
        self.window.set_title(title)
        self.window.connect('destroy', self._on_destroy)
        self.window.connect('key-press-event', self._on_key_press)

        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.connect('draw', self._on_draw)
        self.window.add(self.drawing_area)

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
        self.window.set_default_size(self.width_px, self.height_px)
        self.window.set_resizable(False)

        # Reusable layout for per-cell glyph drawing (text swapped each draw).
        self._layout = self.drawing_area.create_pango_layout('')
        self._layout.set_font_description(self.font_desc)

        self.window.show_all()

    # ── Gtk signal handlers ────────────────────────────────────────

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
        # Ctrl+C as a graceful "stop the emulator" shortcut.
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and keyval in (
                Gdk.KEY_c, Gdk.KEY_C):
            self.stop = True
            return True
        return False

    def _emit(self, byte):
        if self.on_key:
            self.on_key(byte & 0xFF)

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
        try:
            self.window.destroy()
        except Exception:
            pass
