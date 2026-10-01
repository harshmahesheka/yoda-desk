"""Yoda Desk's two popups: the Jedi Archives (the reading list) and the scribe droid's note box.

Both are small focusable windows that open beside Yoda, close when you click away, and sit in
their own window group (see own_group) so they never lift Yoda's desktop window above your apps.
"""
import datetime as dt
import html
import math
import os
import sys
import time

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from library import RANKS, save_config_value  # noqa: E402

try:  # to take keyboard focus when opened by a shortcut rather than a click
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11
except (ImportError, ValueError):
    GdkX11 = None


# Font families in order of preference; Pango uses the first one installed.
SANS = "Ubuntu, Cantarell, Noto Sans, DejaVu Sans, Sans"


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


# ---------- the Jedi Archives: your reading list, week and rank

GREEN = (0.56, 0.84, 0.42)
GOLD = (0.97, 0.80, 0.42)
READING_WIDTH = 440
MAX_LIST_HEIGHT = 340

READING_CSS = f"""
.sd-popup {{
  background-color: rgba(14, 16, 22, 0.95);
  border: 1px solid rgba(255, 255, 255, 0.10);
  border-radius: 20px;
  padding: 22px 22px 16px 22px;
  font-family: {SANS};
  color: rgba(255, 255, 255, 0.92);
}}
.sd-caps {{ font-size: 8.5pt; color: rgba(255, 255, 255, 0.45); }}
.sd-heading {{ font-size: 16pt; font-weight: 300; color: rgba(255, 255, 255, 0.95); }}
.sd-subheading {{ font-size: 9pt; color: rgba(185, 232, 160, 0.75); }}
.sd-big {{ font-size: 24pt; font-weight: 300; color: rgba(255, 255, 255, 0.95); }}
.sd-muted {{ font-size: 9pt; color: rgba(255, 255, 255, 0.5); }}
.sd-good {{ font-size: 9pt; color: #9fdc7c; }}
.sd-sep {{ background-color: rgba(255, 255, 255, 0.08); min-height: 1px; margin: 14px 0 10px 0; }}
.sd-popup list, .sd-popup row {{ background-color: transparent; }}
.sd-popup scrolledwindow, .sd-popup viewport {{ background-color: transparent; border: none; }}
.sd-popup row {{ border-radius: 12px; padding: 7px 8px 7px 10px; outline: none; }}
.sd-popup row:hover {{ background-color: rgba(255, 255, 255, 0.06); }}
.sd-paper {{ font-size: 10.5pt; color: rgba(255, 255, 255, 0.92); }}
.sd-popup button {{
  background-image: none; box-shadow: none; text-shadow: none;
  border-radius: 999px; font-size: 9pt; padding: 5px 14px;
  color: rgba(255, 255, 255, 0.85);
  background-color: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.12);
}}
.sd-popup button:hover {{ background-color: rgba(255, 255, 255, 0.12); }}
.sd-popup button.sd-check {{
  min-width: 30px; min-height: 30px; padding: 0;
  color: #b9e8a0;
  background-color: rgba(140, 210, 100, 0.10);
  border-color: rgba(140, 210, 100, 0.45);
}}
.sd-popup button.sd-check:hover {{ background-color: rgba(140, 210, 100, 0.28); }}
.sd-popup button.sd-step {{ min-width: 22px; min-height: 20px; padding: 0 6px; font-size: 10pt; }}
.sd-popup button.sd-close {{ border: none; background-color: transparent; padding: 2px 8px; }}
.sd-popup button.sd-close:hover {{ background-color: rgba(255, 255, 255, 0.08); }}
.sd-toast {{ font-size: 9pt; color: rgba(255, 255, 255, 0.7); }}
"""


def caps(text, spacing=3):
    return f'<span letter_spacing="{spacing * 1024}">{html.escape(text.upper())}</span>'


def ago(t):
    days = int((time.time() - t) // 86400)
    if days <= 0:
        return "added today"
    if days == 1:
        return "added yesterday"
    if days < 14:
        return f"added {days} days ago"
    return f"added {days // 7} weeks ago"


class ReadingPopup:
    def __init__(self, app, library):
        self.app = app
        self.lib = library
        self.undo_token = None
        self.undo_timer = None
        self.anchor = None
        self.bounds = None  # (top, bottom) of the screen's work area
        self.max_list = MAX_LIST_HEIGHT
        self.ignore_until = 0  # guards the ✓ buttons against a double-click

        provider = Gtk.CssProvider()
        provider.load_from_data(READING_CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.win = Gtk.Window(title="Jedi Archives")
        self.win.set_decorated(False)
        self.win.set_app_paintable(True)
        self.win.set_keep_above(True)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.win.set_resizable(False)
        visual = self.win.get_screen().get_rgba_visual()
        if visual:
            self.win.set_visual(visual)
        self.win.connect("draw", self._clear_bg)
        self.win.connect("key-press-event", self._on_key)
        self.win.connect("focus-out-event", lambda *_: GLib.timeout_add(150, self._maybe_hide))
        self.win.connect("delete-event", lambda *_: self.hide() or True)
        self.win.connect("realize", lambda w: own_group(w))

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box.get_style_context().add_class("sd-popup")
        self.box.set_size_request(READING_WIDTH, -1)
        self.win.add(self.box)
        self._build()

    # ---------- layout

    def _label(self, markup="", cls=None, xalign=0.0, ellipsize=False):
        label = Gtk.Label()
        label.set_markup(markup)
        label.set_xalign(xalign)
        if ellipsize:
            label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        if cls:
            label.get_style_context().add_class(cls)
        return label

    def _build(self):
        # Heading: the title, a glowing accent line, and a quiet subtitle.
        header = Gtk.Box(spacing=8)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        titles.pack_start(self._label(caps("Jedi Archives", 5), "sd-heading"), False, False, 0)
        accent = Gtk.DrawingArea()
        accent.set_size_request(-1, 2)
        accent.connect("draw", self._draw_accent)
        titles.pack_start(accent, False, False, 0)
        titles.pack_start(self._label("Your reading list, keeper of it Yoda is", "sd-subheading"),
                          False, False, 0)
        header.pack_start(titles, True, True, 0)
        close = Gtk.Button(label="✕")
        close.get_style_context().add_class("sd-close")
        close.set_valign(Gtk.Align.START)
        close.connect("clicked", lambda *_: self.hide())
        header.pack_end(close, False, False, 0)
        header.set_margin_bottom(8)
        self.box.pack_start(header, False, False, 0)

        # Jedi rank: the current rank in gold, and a ladder showing progress to the next.
        self.rank_card = Gtk.DrawingArea()
        self.rank_card.set_size_request(-1, 100)
        self.rank_card.set_margin_top(6)
        self.rank_card.set_margin_bottom(10)
        self.rank_card.connect("draw", self._draw_rank)
        self.box.pack_start(self.rank_card, False, False, 0)

        # This week: big count, goal bar and streak.
        week = Gtk.Box(spacing=14)
        week.set_margin_top(6)
        self.count_label = self._label(cls="sd-big")
        week.pack_start(self.count_label, False, False, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.set_valign(Gtk.Align.CENTER)
        self.week_caption = self._label(cls="sd-muted")
        self.week_caption.set_line_wrap(True)
        self.week_caption.set_max_width_chars(44)
        right.pack_start(self.week_caption, False, False, 0)
        self.bar = Gtk.DrawingArea()
        self.bar.set_size_request(-1, 8)
        self.bar.connect("draw", self._draw_bar)
        right.pack_start(self.bar, False, False, 0)
        # Weekly goal, adjustable right here.
        goal_row = Gtk.Box(spacing=6)
        self.goal_label = self._label(cls="sd-muted")
        goal_row.pack_start(self.goal_label, False, False, 0)
        for text, delta, tip in (("−", -1, "Lower the weekly goal"), ("+", 1, "Raise the weekly goal")):
            btn = Gtk.Button(label=text)
            btn.set_tooltip_text(tip)
            btn.get_style_context().add_class("sd-step")
            btn.connect("clicked", lambda _b, d=delta: self._change_goal(d))
            goal_row.pack_start(btn, False, False, 0)
        right.pack_start(goal_row, False, False, 0)
        week.pack_start(right, True, True, 0)
        self.box.pack_start(week, False, False, 0)

        # Last 8 weeks.
        self.history = Gtk.DrawingArea()
        self.history.set_size_request(-1, 54)
        self.history.set_margin_top(12)
        self.history.connect("draw", self._draw_history)
        self.box.pack_start(self.history, False, False, 0)

        sep = Gtk.Box()
        sep.get_style_context().add_class("sd-sep")
        self.box.pack_start(sep, False, False, 0)

        self.list_caption = self._label(cls="sd-caps")
        self.list_caption.set_margin_bottom(6)
        self.box.pack_start(self.list_caption, False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.set_max_content_height(MAX_LIST_HEIGHT)
        self.scroll.set_min_content_height(0)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.connect("row-activated", self._on_row_activated)
        self.scroll.add(self.listbox)
        self.scroll.set_no_show_all(True)  # visibility is managed in refresh()
        self.box.pack_start(self.scroll, False, False, 0)

        self.empty = self._label(cls="sd-muted")
        self.empty.set_line_wrap(True)
        self.empty.set_margin_top(4)
        self.empty.set_margin_bottom(8)
        self.empty.set_no_show_all(True)
        self.box.pack_start(self.empty, False, False, 0)

        # Footer: open folder, hint, and an undo toast after marking read.
        footer = Gtk.Box(spacing=10)
        footer.set_margin_top(12)
        folder = Gtk.Button(label="Open folder")
        folder.connect("clicked", lambda *_: self._open(self.lib.root))
        footer.pack_start(folder, False, False, 0)
        self.toast = Gtk.Box(spacing=8)
        self.toast_label = self._label(cls="sd-toast")
        undo = Gtk.Button(label="Undo")
        undo.connect("clicked", self._on_undo)
        self.toast.pack_start(self.toast_label, False, False, 0)
        self.toast.pack_start(undo, False, False, 0)
        self.toast.set_no_show_all(True)
        self.toast_label.show()
        undo.show()
        self.hint = self._label("Drop papers on Yoda to add them", "sd-muted", xalign=1.0)
        footer.pack_end(self.hint, True, True, 0)
        footer.pack_end(self.toast, False, False, 0)
        self.box.pack_start(footer, False, False, 0)

    def _row(self, paper):
        row = Gtk.ListBoxRow()
        row.paper = paper
        box = Gtk.Box(spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = self._label(html.escape(paper.title), "sd-paper", ellipsize=True)
        title.set_tooltip_text(paper.name)
        text.pack_start(title, False, False, 0)
        ext = os.path.splitext(paper.name)[1].lstrip(".").upper()
        meta = ago(paper.added) + (f"  ·  {ext}" if ext else "")
        text.pack_start(self._label(html.escape(meta), "sd-muted"), False, False, 0)
        box.pack_start(text, True, True, 0)
        check = Gtk.Button(label="✓")
        check.set_tooltip_text("Mark as read")
        check.get_style_context().add_class("sd-check")
        check.set_valign(Gtk.Align.CENTER)
        check.connect("clicked", lambda *_: self._mark_read(paper))
        box.pack_end(check, False, False, 0)
        row.add(box)
        return row

    def refresh(self):
        count, goal = self.lib.read_this_week(), self.lib.goal
        self.count, self.goal = count, goal
        self.count_label.set_markup(f"{count}<span alpha='45%'> / {goal}</span>")
        self.goal_label.set_text(f"Weekly goal: {goal} paper{'s' if goal != 1 else ''}")
        streak = self.lib.streak()
        if count >= goal:
            caption, cls = "Goal met this week. Strong, you are.", "sd-good"
        else:
            left = goal - count
            caption, cls = f"{left} more to reach this week's goal", "sd-muted"
        if streak:
            caption += f"  ·  {streak}-week streak"
        self.streak = streak
        ctx = self.week_caption.get_style_context()
        for c in ("sd-good", "sd-muted"):
            ctx.remove_class(c)
        ctx.add_class(cls)
        self.week_caption.set_text(caption)
        self.hist = self.lib.weekly_history(8)

        for child in self.listbox.get_children():
            self.listbox.remove(child)
        papers = self.lib.unread()
        self.list_caption.set_markup(caps(f"To read  ·  {len(papers)}"))
        for paper in papers:
            self.listbox.add(self._row(paper))
        self.listbox.show_all()
        self.scroll.show_all()
        # Grow with the list up to a limit, then scroll.
        list_h = min(len(papers) * 52, self.max_list)
        self.scroll.set_min_content_height(0)
        self.scroll.set_max_content_height(max(list_h, 1))
        self.scroll.set_min_content_height(list_h)
        folder = self.lib.todo.replace(os.path.expanduser("~"), "~")
        self.empty.set_markup(html.escape(f"Nothing waiting. Put papers in {folder}, or drop them on Yoda."))
        self.scroll.set_visible(bool(papers))
        self.empty.set_visible(not papers)
        self.bar.queue_draw()
        self.history.queue_draw()
        self.rank_card.queue_draw()
        if self.visible:
            GLib.idle_add(self._reanchor)

    def _reanchor(self):
        if not self.anchor:
            return False
        self.win.resize(1, 1)  # shrink to fit the new content
        _, h = self.box.get_preferred_height()
        top, bottom = self.bounds
        room = bottom - top - 16
        list_now = self.scroll.get_min_content_height() if self.scroll.get_visible() else 0
        if h > room and list_now > 104:
            # Too tall for this screen: give the list less height and let it scroll.
            # (Stops at two rows' worth, so this can't loop.)
            self.max_list = max(104, list_now - (h - room))
            self.refresh()
            _, h = self.box.get_preferred_height()
        for extra in (self.history, self.rank_card):  # still too tall (a small screen): leave these out
            if h <= room:
                break
            extra.hide()
            self.win.resize(1, 1)
            _, h = self.box.get_preferred_height()
        if h > room and self.scroll.get_visible() and self.max_list > 52:  # a tiny screen: one row's worth
            self.max_list = max(52, self.max_list - (h - room))
            self.refresh()
            self.win.resize(1, 1)
            _, h = self.box.get_preferred_height()
        w, _ = self.box.get_preferred_width()
        x, y = self.anchor
        y = max(top + 8, min(y, bottom - 8 - h))
        self.win.move(max(self.left, x - w), y)
        return False

    # ---------- drawing

    def _clear_bg(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        return False

    def _text(self, cr, text, font, x, y, rgba, spacing=0, align=0.0):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(font))
        if spacing:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_letter_spacing_new(int(spacing * Pango.SCALE)))
            layout.set_attributes(attrs)
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        cr.move_to(x - tw * align, y)
        cr.set_source_rgba(*rgba)
        PangoCairo.show_layout(cr, layout)
        return tw, th

    def _draw_rank(self, area, cr):
        w = area.get_allocated_width()
        r = self.lib.rank()
        rank = r.name
        self._text(cr, "JEDI RANK", f"{SANS} 8.5", 0, 0, (1, 1, 1, 0.45), spacing=3)
        if r.next:
            hint = f"{r.next} in {r.weeks_left} more week{'s' if r.weeks_left != 1 else ''}"
        elif r.master_streak:
            hint = f"Master streak: {r.master_streak} week{'s' if r.master_streak != 1 else ''}"
        else:
            hint = "Master streak starts next week you meet your goal"
        # Lifelines, as glowing kyber crystals, left of the hint.
        hint_w, _ = self._text(cr, hint, f"{SANS} 9", w, 1, (0, 0, 0, 0), align=1.0)
        lx = w - hint_w - 14
        if r.lifelines:
            label = f"{r.lifelines} lifeline{'s' if r.lifelines != 1 else ''}"
            lw, _ = self._text(cr, label, f"{SANS} 9", lx, 1, (0.62, 0.84, 1.0, 0.9), align=1.0)
            cx = lx - lw - 8
            for k in range(min(r.lifelines, 5)):
                self._crystal(cr, cx - k * 9, 8)

        self._text(cr, hint, f"{SANS} 9", w, 1, (1, 1, 1, 0.5), align=1.0)
        # The rank itself, large and gold, with a soft glow.
        font = f"{SANS} Light 20"
        for dx, dy, a in ((0, 1, 0.25), (0, 0, 1.0)):
            color = (0, 0, 0, a) if a < 1 else (*GOLD, 1.0)
            self._text(cr, rank.upper(), font, dx, 18 + dy, color, spacing=4)

        # Ladder: four evenly spaced ranks, filled up to your progress toward the next.
        y = 74
        x0, x1 = 6, w - 6
        step = (x1 - x0) / (len(RANKS) - 1)
        filled = x0 + step * (r.level + (r.fraction if r.next else 0))

        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(1, 1, 1, 0.10)
        cr.set_line_width(3)
        cr.move_to(x0, y)
        cr.line_to(x1, y)
        cr.stroke()
        if filled > x0:
            g = cairo.LinearGradient(x0, 0, filled, 0)
            g.add_color_stop_rgba(0, *GOLD, 0.35)
            g.add_color_stop_rgba(1, *GOLD, 0.95)
            cr.set_source(g)
            cr.move_to(x0, y)
            cr.line_to(filled, y)
            cr.stroke()
        for i, name in enumerate(RANKS):
            x = x0 + step * i
            reached = i <= r.level
            current = i == r.level
            if current:
                glow = cairo.RadialGradient(x, y, 0, x, y, 11)
                glow.add_color_stop_rgba(0, *GOLD, 0.55)
                glow.add_color_stop_rgba(1, *GOLD, 0)
                cr.set_source(glow)
                cr.arc(x, y, 11, 0, 2 * math.pi)
                cr.fill()
            cr.arc(x, y, 5 if current else 3.5, 0, 2 * math.pi)
            cr.set_source_rgba(*GOLD, 1.0) if reached else cr.set_source_rgba(0.25, 0.26, 0.30, 1)
            cr.fill()
            if not reached:
                cr.arc(x, y, 3.5, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.25)
                cr.set_line_width(1)
                cr.stroke()
            align = 0.0 if i == 0 else (1.0 if i == len(RANKS) - 1 else 0.5)
            label = name.replace("Jedi ", "")
            self._text(cr, label, f"{SANS} 8", x, y + 8,
                       (*GOLD, 0.9) if current else (1, 1, 1, 0.4 if reached else 0.3), align=align)
        return False

    def _crystal(self, cr, x, y):
        glow = cairo.RadialGradient(x, y, 0, x, y, 7)
        glow.add_color_stop_rgba(0, 0.62, 0.84, 1.0, 0.6)
        glow.add_color_stop_rgba(1, 0.62, 0.84, 1.0, 0)
        cr.set_source(glow)
        cr.arc(x, y, 7, 0, 2 * math.pi)
        cr.fill()
        cr.move_to(x, y - 5)
        cr.line_to(x + 3, y)
        cr.line_to(x, y + 5)
        cr.line_to(x - 3, y)
        cr.close_path()
        g = cairo.LinearGradient(x - 3, y - 5, x + 3, y + 5)
        g.add_color_stop_rgb(0, 0.85, 0.95, 1.0)
        g.add_color_stop_rgb(1, 0.35, 0.60, 0.95)
        cr.set_source(g)
        cr.fill()

    def _draw_accent(self, area, cr):
        w = min(area.get_allocated_width(), 190)
        g = cairo.LinearGradient(0, 0, w, 0)
        g.add_color_stop_rgba(0, *GREEN, 0.95)
        g.add_color_stop_rgba(1, *GREEN, 0)
        cr.set_source(g)
        cr.rectangle(0, 0, w, 2)
        cr.fill()
        return False

    def _draw_bar(self, area, cr):
        w, h = area.get_allocated_width(), area.get_allocated_height()
        r = h / 2

        def pill(width):
            cr.new_sub_path()
            cr.arc(r, r, r, math.pi / 2, 1.5 * math.pi)
            cr.arc(width - r, r, r, -math.pi / 2, math.pi / 2)
            cr.close_path()

        pill(w)
        cr.set_source_rgba(1, 1, 1, 0.08)
        cr.fill()
        frac = min(1.0, self.count / self.goal)
        if frac > 0:
            pill(max(h, w * frac))
            g = cairo.LinearGradient(0, 0, w, 0)
            g.add_color_stop_rgb(0, 0.36, 0.62, 0.30)
            g.add_color_stop_rgb(1, *GREEN)
            cr.set_source(g)
            cr.fill()
        # Ticks between goal segments.
        cr.set_source_rgba(0.05, 0.06, 0.08, 0.9)
        for i in range(1, self.goal):
            cr.rectangle(w * i / self.goal - 1, 0, 2, h)
        cr.fill()
        return False

    def _draw_history(self, area, cr):
        w, h = area.get_allocated_width(), area.get_allocated_height()
        hist = self.hist
        top = max(max(hist), self.goal, 1)
        label_h = 14
        bar_area = h - label_h
        n = len(hist)
        gap = 8
        bw = (w - gap * (n - 1)) / n
        # Goal line.
        gy = bar_area - bar_area * self.goal / top
        cr.set_source_rgba(1, 1, 1, 0.18)
        cr.set_line_width(1)
        cr.set_dash([3, 4])
        cr.move_to(0, gy + 0.5)
        cr.line_to(w, gy + 0.5)
        cr.stroke()
        cr.set_dash([])
        for i, count in enumerate(hist):
            x = i * (bw + gap)
            bh = max(2, bar_area * count / top) if count else 2
            current = i == n - 1
            met = count >= self.goal
            if current:
                cr.set_source_rgba(*GREEN, 0.95)
            elif met:
                cr.set_source_rgba(*GREEN, 0.5)
            else:
                cr.set_source_rgba(1, 1, 1, 0.22 if count else 0.10)
            r = min(3, bw / 2)
            y = bar_area - bh
            cr.new_sub_path()
            cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
            cr.arc(x + bw - r, y + r, r, -math.pi / 2, 0)
            cr.line_to(x + bw, bar_area)
            cr.line_to(x, bar_area)
            cr.close_path()
            cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.4)
        cr.select_font_face(SANS.split(",")[0], cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9)
        for text, x, align in (("8 WEEKS AGO", 0, 0), ("THIS WEEK", w, 1)):
            ext = cr.text_extents(text)
            cr.move_to(x - ext.x_advance * align, h - 2)
            cr.show_text(text)
        return False

    # ---------- actions

    def _open(self, path):
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        except GLib.Error:
            pass

    def _change_goal(self, delta):
        goal = max(1, min(30, self.lib.goal + delta))
        if goal == self.lib.goal:
            return
        self.lib.set_goal(goal)
        try:
            save_config_value("weekly_paper_goal", goal)
        except OSError:
            pass  # still applies for this session
        self.refresh()
        self.app.reading_list_changed()

    def _on_row_activated(self, _box, row):
        self._open(row.paper.path)

    def _mark_read(self, paper):
        now = time.time()
        if now < self.ignore_until:
            return  # the list just moved under the pointer; don't mark a second paper
        self.ignore_until = now + 0.6
        try:
            self.undo_token = self.lib.mark_read(paper)
        except OSError:
            return
        try:
            self.app.paper_read(paper.title)
        except Exception as e:  # never let Yoda's reaction stop the list from updating
            print(f"yoda-desk: {e}", file=sys.stderr)
        self.refresh()
        self.toast_label.set_text("Moved to Read.")
        self.toast.show()
        self.hint.hide()
        if self.undo_timer:
            GLib.source_remove(self.undo_timer)
        self.undo_timer = GLib.timeout_add_seconds(8, self._hide_toast)

    def _on_undo(self, *_):
        if self.undo_token:
            self.lib.undo(self.undo_token)
            self.lib.rank_events(silent=True)  # undoing isn't a fall in rank
            self.undo_token = None
            self.app.reading_list_changed()
            self.refresh()
        self._hide_toast()

    def _hide_toast(self):
        self.toast.hide()
        self.hint.show()
        self.undo_timer = None
        return False

    # ---------- show / hide

    @property
    def visible(self):
        return self.win.get_visible()

    def show(self, anchor_right, anchor_top, area_top, area_bottom, area_left=0):
        """Show the panel with its top-right corner at the given point, kept on screen."""
        self.anchor = (anchor_right, anchor_top)
        self.bounds = (area_top, area_bottom)
        self.left = area_left + 8
        self.max_list = MAX_LIST_HEIGHT
        self.refresh()
        self.win.show_all()
        self._reanchor()
        self.win.present()

    def hide(self):
        self.win.hide()

    def _maybe_hide(self):
        if self.visible and not self.win.is_active():
            self.hide()
        return False

    def _on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False


# ---------- the scribe droid's hologram: write a note, or read the ones you've kept

NOTE_WIDTH = 400
MARGIN = 14  # room around the box for its glow
FLICKER = [0.35, 0.1, 0.6, 0.3, 0.85, 0.7, 1.0]  # opacity, frame by frame, as it projects in

NOTE_CSS = f"""
.sd-note {{
  background-color: rgba(7, 16, 14, 0.975);
  border: 1px solid rgba(140, 242, 190, 0.42);
  border-radius: 18px;
  padding: 14px 16px 12px 16px;
  margin: {MARGIN}px;
  box-shadow: 0 0 {MARGIN - 2}px rgba(140, 242, 190, 0.22);
  font-family: {SANS};
}}
.sd-note-title {{ font-size: 8.5pt; color: rgba(140, 242, 190, 0.75); }}
.sd-note-hint {{ font-size: 8pt; color: rgba(255, 255, 255, 0.34); }}
.sd-note-placeholder {{ font-size: 11.5pt; font-style: italic; color: rgba(210, 240, 220, 0.30); }}
.sd-note-search-hint {{ font-size: 10.5pt; font-style: italic; color: rgba(210, 240, 220, 0.30); }}
.sd-note scrolledwindow, .sd-note viewport {{ background-color: transparent; border: none; }}
.sd-note textview, .sd-note textview text {{
  background-color: transparent;
  color: rgba(235, 250, 240, 0.95);
  font-size: 11.5pt;
  caret-color: #8cf2be;
}}
.sd-note selection, .sd-note textview text selection {{ background-color: rgba(140, 242, 190, 0.30); }}
.sd-note entry {{
  background-color: transparent; background-image: none; border: none; box-shadow: none; outline: none;
  border-bottom: 1px solid rgba(140, 242, 190, 0.18);
  border-radius: 0; padding: 2px 0 6px 0;
  color: rgba(240, 255, 245, 0.97);
  caret-color: #8cf2be;
}}
.sd-note entry:focus {{ border-bottom-color: rgba(140, 242, 190, 0.55); }}
.sd-note entry.sd-note-heading {{ font-size: 15pt; font-weight: 300; }}
.sd-note entry.sd-note-search {{ font-size: 10.5pt; }}
.sd-note button {{
  background-image: none; box-shadow: none; text-shadow: none;
  border-radius: 999px; font-size: 8.5pt; padding: 3px 12px; min-height: 0;
  color: rgba(255, 255, 255, 0.6);
  background-color: transparent;
  border: 1px solid transparent;
}}
.sd-note button:hover {{ color: rgba(255, 255, 255, 0.9); background-color: rgba(255, 255, 255, 0.06); }}
.sd-note button.sd-tab-on {{
  color: #b8f5d4;
  background-color: rgba(140, 242, 190, 0.12);
  border-color: rgba(140, 242, 190, 0.40);
}}
.sd-note list, .sd-note row {{ background-color: transparent; }}
.sd-note row {{ border-radius: 12px; padding: 7px 8px; outline: none; }}
.sd-note row:hover {{ background-color: rgba(140, 242, 190, 0.07); }}
.sd-note-day {{ font-size: 8pt; color: rgba(140, 242, 190, 0.6); }}
.sd-note-time {{ font-size: 8.5pt; color: rgba(140, 242, 190, 0.8); }}
.sd-note-head {{ font-size: 11pt; color: rgba(240, 255, 245, 0.95); }}
.sd-note-body {{ font-size: 9.5pt; color: rgba(225, 240, 230, 0.62); }}
.sd-note-empty {{ font-size: 10pt; font-style: italic; color: rgba(210, 240, 220, 0.4); }}
"""

PROMPTS = ["What is on your mind, hmm?", "A thought, record it you will.",
           "Write it down, you should.", "Hmm. Speak, and remember it the droid will."]
HEADINGS = ["A heading, give it you may", "Name this thought, hmm?", "A heading (if you wish)"]


def own_group(win):
    """Give a popup its own window group.

    The desktop widgets are utility windows, and window managers lift utility windows
    to the layer of the highest window in their group. Sharing a group with an
    on-top popup would bring every widget up over your apps while it is open."""
    gdk_win = win.get_window()
    if gdk_win is not None:
        gdk_win.set_group(gdk_win)


def day_name(day):
    today = dt.date.today()
    if day == today:
        return "Today"
    if day == today - dt.timedelta(days=1):
        return "Yesterday"
    return f"{day:%a} {day.day} {day:%b}" + ("" if day.year == today.year else f" {day.year}")


class NoteBox:
    def __init__(self, app, notes, on_save, on_place):
        self.app = app
        self.notes = notes
        self.on_save = on_save  # (heading, text, (x, y, w, h) of the text on screen)
        self.on_place = on_place  # (x, y, w, h) of the box on screen, or None when it closes
        self.anchor = None
        self.prompt = 0
        self.mode = "write"
        self.unfocused_since = None
        self.watch = None
        self.flicker = None

        provider = Gtk.CssProvider()
        provider.load_from_data(NOTE_CSS.encode())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.win = win = Gtk.Window(title="Note to Yoda")
        win.set_decorated(False)
        win.set_app_paintable(True)
        win.set_keep_above(True)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        win.set_resizable(False)
        visual = win.get_screen().get_rgba_visual()
        if visual:
            win.set_visual(visual)
        win.connect("draw", self._clear_bg)
        win.connect("key-press-event", self._on_window_key)
        win.connect("focus-out-event", lambda *_: GLib.timeout_add(150, self._maybe_hide))
        win.connect("delete-event", lambda *_: self.hide() or True)
        win.connect("realize", lambda w: own_group(w))
        win.connect("hide", lambda *_: self.on_place(None))

        self.box = box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.get_style_context().add_class("sd-note")
        box.set_size_request(NOTE_WIDTH, -1)
        box.connect_after("draw", self._draw_scanlines)
        win.add(box)

        # header: the droid's name, and Write / Read
        head = Gtk.Box(spacing=4)
        title = Gtk.Label()
        title.set_markup(caps("Scribe droid"))
        title.get_style_context().add_class("sd-note-title")
        head.pack_start(title, False, False, 0)
        self.read_tab = Gtk.Button()
        self.read_tab.connect("clicked", lambda *_: self.switch("read"))
        self.write_tab = Gtk.Button(label="Write")
        self.write_tab.connect("clicked", lambda *_: self.switch("write"))
        for tab in (self.read_tab, self.write_tab):
            tab.set_can_focus(False)
            head.pack_end(tab, False, False, 0)
        box.pack_start(head, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(140)
        self.stack.set_vhomogeneous(False)
        self.stack.set_interpolate_size(True)
        self.stack.add_named(self._build_write(), "write")
        self.stack.add_named(self._build_read(), "read")
        box.pack_start(self.stack, True, True, 0)

    # ---------- the two pages

    def _build_write(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.heading = Gtk.Entry()
        self.heading.get_style_context().add_class("sd-note-heading")
        self.heading.connect("activate", lambda *_: self.view.grab_focus())  # Enter moves on
        self.heading.connect("changed", lambda *_: self.app.note_typed())
        page.pack_start(self.heading, False, False, 0)

        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_accepts_tab(False)
        self.view.set_pixels_inside_wrap(2)
        self.view.connect("key-press-event", self._on_body_key)
        self.buffer = self.view.get_buffer()
        self.buffer.connect("changed", self._on_changed)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_min_content_height(72)
        self.scroll.set_max_content_height(200)
        self.scroll.set_propagate_natural_height(True)
        self.scroll.add(self.view)
        self.placeholder = Gtk.Label(xalign=0.0, yalign=0.0)
        self.placeholder.get_style_context().add_class("sd-note-placeholder")
        overlay = Gtk.Overlay()
        overlay.add(self.scroll)
        overlay.add_overlay(self.placeholder)
        overlay.set_overlay_pass_through(self.placeholder, True)
        page.pack_start(overlay, True, True, 0)

        hint = Gtk.Label(xalign=0.0)
        hint.set_text("Enter to keep  ·  Shift+Enter for a new line  ·  Esc to close")
        hint.get_style_context().add_class("sd-note-hint")
        page.pack_start(hint, False, False, 0)
        return page

    def _build_read(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.search = Gtk.Entry()
        self.search.get_style_context().add_class("sd-note-search")
        self.search.connect("changed", lambda *_: self._fill_list())
        # GTK hides an entry's placeholder while it has focus; this hint stays until you type.
        self.search_hint = Gtk.Label(label="Search your notes, you may", xalign=0.0)
        self.search_hint.get_style_context().add_class("sd-note-search-hint")
        self.search_hint.set_margin_bottom(4)
        overlay = Gtk.Overlay()
        overlay.add(self.search)
        overlay.add_overlay(self.search_hint)
        overlay.set_overlay_pass_through(self.search_hint, True)
        page.pack_start(overlay, False, False, 0)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.connect("row-activated", self._on_row)
        self.list_scroll = Gtk.ScrolledWindow()
        self.list_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_scroll.set_max_content_height(380)
        self.list_scroll.set_propagate_natural_height(True)
        self.list_scroll.add(self.listbox)
        page.pack_start(self.list_scroll, True, True, 0)
        foot = Gtk.Box(spacing=8)
        hint = Gtk.Label(xalign=0.0)
        hint.set_text("Click a note to open it")
        hint.get_style_context().add_class("sd-note-hint")
        folder = Gtk.Button(label="Open folder")
        folder.set_can_focus(False)
        folder.connect("clicked", lambda *_: self._open(self.notes.folder))
        foot.pack_start(hint, False, False, 0)
        foot.pack_end(folder, False, False, 0)
        page.pack_start(foot, False, False, 0)
        return page

    def _fill_list(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self.search_hint.set_visible(not self.search.get_text())
        query = self.search.get_text().strip().lower()
        notes = self.notes.all()
        self.notes.count = len(notes)
        found = [n for n in notes if not query or query in f"{n.heading}\n{n.body}".lower()]
        day = None
        for note in found[:300]:
            if note.day != day:
                day = note.day
                label = Gtk.Label(xalign=0.0)
                label.set_markup(caps(day_name(day), 2))
                label.get_style_context().add_class("sd-note-day")
                label.set_margin_top(4 if self.listbox.get_children() else 0)
                row = Gtk.ListBoxRow(activatable=False)
                row.add(label)
                self.listbox.add(row)
            row = Gtk.ListBoxRow()
            row.path = note.path
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            top = Gtk.Box(spacing=8)
            t = Gtk.Label(label=note.time)
            t.get_style_context().add_class("sd-note-time")
            top.pack_start(t, False, False, 0)
            if note.heading:
                h = Gtk.Label(label=note.heading, xalign=0.0)
                h.set_ellipsize(Pango.EllipsizeMode.END)
                h.get_style_context().add_class("sd-note-head")
                top.pack_start(h, True, True, 0)
            v.pack_start(top, False, False, 0)
            if note.body:
                b = Gtk.Label(label=note.body, xalign=0.0)
                b.set_line_wrap(True)
                b.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                b.set_ellipsize(Pango.EllipsizeMode.END)
                b.set_lines(4)
                b.set_max_width_chars(46)
                b.get_style_context().add_class("sd-note-body" if note.heading else "sd-note-head")
                v.pack_start(b, False, False, 0)
            row.add(v)
            self.listbox.add(row)
        if not found:
            label = Gtk.Label(label="Nothing found, there is. Hmm." if query
                              else "No notes yet. Write one, you should.", xalign=0.0)
            label.get_style_context().add_class("sd-note-empty")
            row = Gtk.ListBoxRow(activatable=False)
            row.add(label)
            self.listbox.add(row)
        self.listbox.show_all()
        self.read_tab.set_label(f"Read · {len(notes)}" if notes else "Read")
        if self.visible:
            GLib.idle_add(self._reanchor)

    def _on_row(self, _box, row):
        path = getattr(row, "path", None)
        if path:
            self._open(path)

    def _open(self, path):
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        except GLib.Error:
            return
        self.hide()

    def switch(self, mode):
        self.mode = mode
        for tab, on in ((self.write_tab, mode == "write"), (self.read_tab, mode == "read")):
            ctx = tab.get_style_context()
            (ctx.add_class if on else ctx.remove_class)("sd-tab-on")
        if mode == "read":
            self._fill_list()
        self.stack.set_visible_child_name(mode)
        # Writing starts in the note itself; the heading is there if you want one.
        (self.search if mode == "read" else self.view).grab_focus()
        if self.visible:
            GLib.idle_add(self._reanchor)

    # ---------- show / hide

    @property
    def visible(self):
        return self.win.get_visible()

    def show(self, right, bottom, area, mode="write", timestamp=None):
        """Open with the box's bottom-right corner near (right, bottom), kept inside the area."""
        self.anchor = (right, bottom, area)
        self.placeholder.set_text(PROMPTS[self.prompt % len(PROMPTS)])
        self.heading.set_placeholder_text(HEADINGS[self.prompt % len(HEADINGS)])
        self.prompt += 1
        self.win.get_child().show_all()
        self.search.set_text("")
        self._fill_list()
        self.switch(mode)
        self._on_changed()
        self.win.realize()
        self._reanchor()
        gdk_win = self.win.get_window()
        if timestamp is None and GdkX11 is not None and isinstance(gdk_win, GdkX11.X11Window):
            timestamp = GdkX11.x11_get_server_time(gdk_win)  # opened by a shortcut, not a click
        # Mapping with the timestamp, not showing first and asking for focus after, lets the
        # window manager focus it straight away.
        self.win.set_opacity(FLICKER[0])
        if timestamp:
            self.win.present_with_time(timestamp)
        else:
            self.win.present()
        self.flicker = 0
        GLib.timeout_add(35, self._flicker_in)
        self.unfocused_since = None
        if self.watch is None:
            self.watch = GLib.timeout_add(500, self._watch_focus)

    def _flicker_in(self):
        self.flicker += 1
        if self.flicker >= len(FLICKER) or not self.visible:
            self.win.set_opacity(1.0)
            return False
        self.win.set_opacity(FLICKER[self.flicker])
        return True

    def _reanchor(self):
        if not self.anchor:
            return False
        right, bottom, area = self.anchor
        self.win.resize(1, 1)
        w, _ = self.win.get_preferred_width()
        _, h = self.win.get_preferred_height()
        x = int(max(area.x + 4, min(right + MARGIN - w, area.x + area.width - w)))
        y = int(max(area.y + 4, min(bottom + MARGIN - h, area.y + area.height - h)))
        self.win.move(x, y)
        self.on_place((x + MARGIN, y + MARGIN, w - 2 * MARGIN, h - 2 * MARGIN))
        return False

    def hide(self):
        self.win.hide()

    def _maybe_hide(self):
        if self.visible and not self.win.is_active():
            self.hide()  # the draft is kept for next time
        return False

    def _watch_focus(self):
        """Close the box if it has sat unfocused for a while (focus-out alone can miss that:
        if the window manager never focused it, or focus went to a window that won't take it)."""
        if not self.visible:
            self.watch = None
            return False
        now = GLib.get_monotonic_time() / 1e6
        if self.win.is_active():
            self.unfocused_since = None
        elif self.unfocused_since is None:
            self.unfocused_since = now
        elif now - self.unfocused_since > 2.0:
            self.hide()
            self.watch = None
            return False
        return True

    # ---------- drawing

    def _clear_bg(self, _w, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        return False

    def _draw_scanlines(self, widget, cr):
        """Faint hologram scanlines over the box, and a glow along its top.

        (Drawn here rather than as a CSS gradient, which GTK loses when part of the box redraws.)"""
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        rounded_rect(cr, MARGIN + 1, MARGIN + 1, w - 2 * MARGIN - 2, h - 2 * MARGIN - 2, 17)
        cr.clip()
        g = cairo.LinearGradient(0, MARGIN, 0, MARGIN + (h - 2 * MARGIN) * 0.45)
        g.add_color_stop_rgba(0, 0.55, 0.95, 0.75, 0.07)
        g.add_color_stop_rgba(1, 0.55, 0.95, 0.75, 0.0)
        cr.set_source(g)
        cr.paint()
        for y in range(MARGIN + 2, h - MARGIN, 3):
            cr.rectangle(MARGIN, y, w - 2 * MARGIN, 1)
        cr.set_source_rgba(0.55, 0.95, 0.75, 0.03)
        cr.fill()
        return False

    # ---------- typing

    def _text(self):
        start, end = self.buffer.get_bounds()
        return self.buffer.get_text(start, end, False)

    def _on_changed(self, *_):
        self.placeholder.set_visible(not self._text())
        self.app.note_typed()
        if self.visible and self.anchor:
            GLib.idle_add(self._reanchor)  # grow upward as the note gets longer

    def _on_window_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def _on_body_key(self, _w, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not event.state & Gdk.ModifierType.SHIFT_MASK:
            text, heading = self._text().strip(), self.heading.get_text().strip()
            if text or heading:
                wx, wy = self.win.get_position()
                tx, ty = self.heading.translate_coordinates(self.win, 0, 0)
                alloc = self.scroll.get_allocation()
                _sx, sy = self.scroll.translate_coordinates(self.win, 0, 0)
                rect = (wx + tx, wy + ty, alloc.width, sy + alloc.height - ty)
                self.buffer.set_text("")
                self.heading.set_text("")
                self.hide()
                self.on_save(heading, text, rect)
            return True
        return False
