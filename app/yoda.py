"""Yoda Desk: Yoda, keeper of your reading list, meditating in the corner of your screen.

He floats cross-legged in a soft glow of the Force, eyes closed, a few stones lifted beside him.
The papers waiting in your to-read folder orbit him as scrolls; glowing pips below show this
week's progress. Come near and he opens his eyes. Hover over him and he tells you how your week
is going; hover over a scroll to see its title, click it to read it. Click Yoda for the reading
list (the Jedi Archives); drop PDFs on him to add them. Mark a paper read and its scroll spirals
into him; meet your weekly goal, or rise a Jedi rank, and he celebrates.

A little scribe droid circles him and keeps your notes. Click it to write one, or drop text or a
link on Yoda; the words dissolve into light and fly into it.

Yoda himself is a set of pre-rendered images (assets/yoda); this file plays them, and draws
everything else live with Cairo. Run it with  python3 yoda.py  (or `yoda-desk install`).
"""
import datetime as dt
import json
import math
import os
import random
import signal
import sys
import time
from collections import deque

import cairo

import library
from library import (ADDED, BACKLOG, BEHIND, DONE, EMPTY, GOAL_MET, LAST_DAYS, LATE, LIFELINE_EARNED,
                     LIFELINE_SPENT, LONG_WAIT, MASTER_MET, MORNING, NOTED, ON_TRACK, OPENED, PROMOTED,
                     STREAK_LOST, WEEKEND, WISDOM, Library, Notes, load_config)
from popups import SANS, NoteBox, ReadingPopup, rounded_rect
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "yoda")
FPS = 30
GREEN = (0.56, 0.84, 0.42)
FORCE = (0.55, 0.95, 0.75)
RIM = (0.62, 0.84, 1.0)

# Geometry, in Yoda units (y down, origin at his centre); a unit is S window pixels.
WINDOW_W, WINDOW_H = 430, 184  # his window: room for the speech bubble to his left
CENTRE_X, CENTRE_Y = 318, 98  # where he sits in it
HEAD_Y = -58  # the middle of his head
FLOOR_Y = 50  # the bottom of his lap
AURA_BOX = (-80, -100, 160, 172)  # around the whole figure
HANDS = ((-35, 15), (33, 13))  # where his hands rest, on his knees
ORBIT_RX, ORBIT_RY, ORBIT_Y = 92, 18, 4
BUBBLE_IN, BUBBLE_OUT = 0.38, 0.28  # seconds for the speech bubble to pop up and to shrink away
BUBBLE_TYPE_DELAY, BUBBLE_CHARS_PER_SEC = 0.2, 13  # the line types itself out, as slowly as he speaks
# Pauses while speaking, in characters' worth of time: after commas, sentences and lines.
SPEECH_PAUSE = {",": 4, ";": 5, ":": 5, ".": 8, "!": 8, "?": 8, "\n": 10}
VOWELS = set("aeiouyAEIOUY")
MAX_ORBIT = 7
SCROLL_W, SCROLL_H = 22, 29
DROID_R = 10.0  # radius of the scribe droid
DROID_SPEED = 0.33  # radians a second round its orbit
NOTE_FLIGHT = 1.25  # seconds for a note's light to reach the droid
CELEBRATION = {"goal": 6.5, "rank": 9.0}  # seconds each celebration lasts
HOVER_TALK_EVERY = 20  # seconds between status lines when you hover over him
REQUEST_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "yoda-desk-request")
# He runs on X11, through XWayland on a Wayland session. There, "keep below" drops him into the
# compositor's bottom layer, under the desktop-icons window: GNOME's Desktop Icons NG can only
# emulate a desktop window on Wayland, so its full-screen window sits in the normal layer instead.
# It's transparent, so he still shows through it, but it swallows every click meant for him. So on
# Wayland he stays out of the bottom layer. He still ends up behind other windows, since he never
# takes focus and never raises himself.
KEEP_BELOW = not (os.environ.get("WAYLAND_DISPLAY")
                  or os.environ.get("XDG_SESSION_TYPE") == "wayland")


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def short(title, n=46):
    return title if len(title) <= n else title[: n - 1].rstrip() + "…"


def clear(cr):
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)


def make_window(title):
    """A borderless, transparent window that sits behind everything, on every workspace."""
    win = Gtk.Window(title=title)
    win.set_decorated(False)
    win.set_app_paintable(True)
    win.set_keep_below(KEEP_BELOW)
    win.stick()
    win.set_skip_taskbar_hint(True)
    win.set_skip_pager_hint(True)
    win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
    win.set_accept_focus(False)
    win.set_focus_on_map(False)
    visual = win.get_screen().get_rgba_visual()
    if visual:
        win.set_visual(visual)
    return win


class Images:
    """Yoda himself: his body, and his head in its states (waking, blinking, talking, glancing),
    rendered once in 3D and kept in assets/yoda. Scaled here, once, to the window's size, and to
    the screen's pixel density so he stays sharp on HiDPI screens."""

    def __init__(self, folder, scale, density=1):
        with open(os.path.join(folder, "yoda.json")) as f:
            meta = json.load(f)
        ppu = float(meta["px_per_unit"])
        k = scale / ppu  # window pixels per image pixel
        ox, oy = meta["origin"]
        size = meta["size"]
        self.body = self._scaled(os.path.join(folder, "body.png"), k, density)
        self.body_box = (ox, oy, size[0] / ppu, size[1] / ppu)
        cx, cy, _cw, _ch = meta["head_crop"]
        self.head_at = (ox + cx / ppu, oy + cy / ppu)
        self.head = {name: self._scaled(os.path.join(folder, path), k, density)
                     for name, path in meta["head"].items()}
        self.seq = meta["sequences"]
        self.neck = tuple(meta["neck"])
        self.ear = min(meta["ear_tips"])  # the left one, as you look at him

    @staticmethod
    def _scaled(path, k, density):
        src = cairo.ImageSurface.create_from_png(path)
        w = max(1, math.ceil(src.get_width() * k * density))
        h = max(1, math.ceil(src.get_height() * k * density))
        dst = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(dst)
        cr.scale(k * density, k * density)
        cr.set_source_surface(src, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        dst.set_device_scale(density, density)  # so it draws at its logical size, with every pixel
        return dst


class Yoda:
    def __init__(self, area, app, lib, scale, density=1, remind_minutes=45, notes=None):
        self.app = app
        self.notes = notes
        self.area = area
        self.lib = lib
        self.S = scale
        self.remind_every = remind_minutes * 60 if remind_minutes and remind_minutes > 0 else None
        self.width = int(WINDOW_W * scale)
        self.height = int(WINDOW_H * scale)
        # Yoda's centre, in window pixels.
        self.cx = CENTRE_X * scale
        self.cy = CENTRE_Y * scale
        self.win = make_window("Yoda")
        self.pos = (area.x + area.width - self.width, area.y + int(8 * scale))
        self.win.move(*self.pos)
        self.win.set_default_size(self.width, self.height)
        self.win.set_size_request(self.width, self.height)
        self.win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK
                            | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.win.connect("draw", self._on_draw)
        self.win.connect("button-press-event", self._on_press)
        self.win.connect("motion-notify-event", self._on_motion)
        self.win.connect("leave-notify-event", self._on_leave)
        self.win.connect("realize", lambda *_: self._set_input())
        # Drop files on Yoda to add them to the reading list, or text and links to keep as notes.
        self.win.drag_dest_set(Gtk.DestDefaults.ALL, [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
                               Gdk.DragAction.COPY)
        self.win.drag_dest_add_text_targets()
        self.win.connect("drag-data-received", self._on_drop)

        self.look = Images(ASSETS, scale, density)
        self.body = self.look.body
        self.body_box = self.look.body_box
        self.ear_tip = self.look.ear
        self.floor_y = FLOOR_Y
        self.aura_box = AURA_BOX
        self.hands = HANDS
        self.glance_side = "look_left"
        self.said = deque(maxlen=12)  # his last few lines, so he doesn't repeat himself
        self.pointer_u = None  # the pointer, in Yoda units, while it's over his window
        self.celebration = None  # (kind, start time) while he celebrates
        self.sparks = []  # (x, y, vx, vy, born) flying off in a celebration
        # stones lifted by the Force (x, y, size, phase), beside and below him
        self.stones = [(-74, 46, 6.2, 0.0), (80, 34, 4.6, 2.1), (-32, 66, 3.8, 4.0), (42, 64, 5.2, 5.2)]
        self.stone_push = [[0.0, 0.0] for _ in self.stones]
        self.unread = []
        self.read_count = 0
        self.rank, self.master_streak = "Youngling", 0
        self.bubble = None
        self.bubble_until = 0
        self.bubble_at = 0  # when the bubble popped up
        self.bubble_text_at = 0  # when the current line started typing
        self.eyes_open = 0.0  # 0 closed (meditating) .. 1 open; eased
        self.refresh()
        self.blink = 0.0
        self.glance_until = 0
        self.absorb_t = None  # a read scroll spiralling in
        self.orbit = 0.0
        self.orbit_speed = 0.22
        self.pointer_inside = False
        self.hover = None  # index of the orbiting scroll under the pointer
        self.slots = []  # (paper, x, y, depth) in window pixels, from the last frame
        now = time.time()
        self.next_remind = now + 25
        self.next_refresh = now + 60
        self.next_glance = now + random.uniform(15, 40)
        self.next_hover_talk = 0
        self.aura = self._render_aura()
        self.hover_droid = False
        self.note_box_open = False
        self.droid_pulse = 0.0  # a flash when a note arrives, or a key is pressed
        self.droid_a = random.uniform(0, 2 * math.pi)  # where it is on its orbit
        self.droid_speed = DROID_SPEED
        self.droid_at = None  # (x, y, depth, facing) in units, from the last frame
        self.note_motes = []  # (start x, y, bend x, y, delay) in Yoda units
        self.note_t = None
        self.note_ghost = None  # (heading, text, x, y, w) in window pixels, fading as it dissolves
        self.note_from_box = False
        self.present_rect = None  # the note box, in units, while the droid projects it
        self.present_spot = (-62, -46)  # where the droid hovers to project it
        self.droid_present = 0.0  # 0 on its orbit .. 1 projecting the note box
        self.win.show_all()

    # ---------- data

    def refresh(self):
        """Reload papers and stats; returns True if Yoda announced a rank change."""
        self.unread = self.lib.unread()
        self.read_count = self.lib.read_this_week()
        if self.notes:
            self.notes.recount()  # notes may be written from the command line too
        rank = self.lib.rank()
        self.rank, self.master_streak = rank.name, rank.master_streak
        # Real titles for papers named after arXiv IDs, fetched in the background.
        self.lib.lookup_titles(self.unread, lambda: GLib.idle_add(self._titles_arrived))
        events = self.lib.rank_events()
        if "lifeline_spent" in events:
            self.say(LIFELINE_SPENT, 9)
        elif "up" in events:
            self.say(PROMOTED.get(self.rank, "Grown in the Force, you have."), 9)
            self.celebrate("rank")
        elif "down" in events:
            self.say(STREAK_LOST, 8)
        elif "lifeline_earned" in events:
            self.say(LIFELINE_EARNED, 9)
        else:
            return False
        return True

    def _titles_arrived(self):
        self.app.reading_list_changed()
        return False

    def say(self, template, secs=7.0, title=None):
        """Speak a line; {title} is the given title or the oldest unread paper.

        Titles go in as format arguments, so braces in them (LaTeX in arXiv titles) are safe."""
        if title is None:
            title = short(self.unread[0].title) if self.unread else ""
        now = time.time()
        # If he's meditating, he opens his eyes first; the bubble and the words wait for him.
        wake = 0.0 if self.eyes_open > 0.95 else 0.5 * (1 - self.eyes_open)
        if self.bubble is None or now >= self.bubble_until - BUBBLE_OUT:
            self.bubble_at = now + wake  # pop a fresh bubble; otherwise just type the new line into it
        self.bubble = template.format(**self._facts(), title=title)
        self.bubble_text_at = now + wake
        self._speech = None  # the new line's timing, worked out when first needed
        self.bubble_until = now + wake + max(secs, self._typing_time() + 2.5)  # time to finish, and to read it

    def _facts(self):
        """What his lines can mention (see yoda_lines.py)."""
        def n(k, word):
            return f"{k} {word}{'' if k == 1 else 's'}"
        count, goal = self.read_count, self.lib.goal
        left = max(0, goal - count)
        oldest = self.unread[0] if self.unread else None
        waited = int((time.time() - oldest.added) // 86400) if oldest else 0
        days_left = 7 - (dt.date.today().weekday() - library.WEEK_START_DAY) % 7
        return dict(count=count, goal=goal, left=left, read_p=n(count, "paper"),
                    left_p=n(left, "more paper"), days=n(waited, "day"), days_left=n(days_left, "day"),
                    waiting=n(len(self.unread), "paper"), rank=self.rank, master=self.master_streak,
                    folder=self.lib.todo.replace(os.path.expanduser("~"), "~"),
                    waited=waited, days_left_n=days_left)

    def pick(self, lines):
        """A line from lines, avoiding the ones he said most recently."""
        fresh = [line for line in lines if line not in self.said] or list(lines)
        line = random.choice(fresh)
        self.said.append(line)
        return line

    def status(self, secs=7.0, hover=False):
        """Say something about how the week's reading is going, or, now and then, something wiser."""
        if self.refresh():
            return  # a rank announcement takes priority
        now = dt.datetime.now()
        f = self._facts()
        if now.hour >= 23 or now.hour < 5:
            if random.random() < 0.6:
                return self.say(self.pick(LATE), secs)
        elif now.hour < 10 and self.unread and random.random() < 0.3:
            return self.say(self.pick(MORNING), secs)
        if now.weekday() >= 5 and random.random() < 0.2:
            return self.say(self.pick(WEEKEND), secs)
        if hover and random.random() < 0.25:
            return self.say(self.pick(WISDOM), secs)
        if not self.unread:
            return self.say(self.pick(EMPTY), secs)
        if self.read_count >= self.lib.goal:
            lines = MASTER_MET if self.rank == "Jedi Master" and self.master_streak > 1 else GOAL_MET
            return self.say(self.pick(lines), secs)
        behind = self.read_count < self.lib.expected_by_now()
        lines = list(BEHIND if behind else ON_TRACK)
        # the particulars of this week weigh in, when they matter
        if behind and f["days_left_n"] <= 2:
            lines += LAST_DAYS * 2
        if f["waited"] >= 7:
            lines += LONG_WAIT
        if len(self.unread) >= 10:
            lines += BACKLOG
        self.say(self.pick(lines), secs)

    def remind(self):
        self.status()
        if self.remind_every:
            self.next_remind = time.time() + self.remind_every * random.uniform(0.85, 1.15)

    def paper_read(self, title):
        """Called by the reading list after a paper is marked read."""
        before = self.read_count
        announced = self.refresh()
        self.absorb_t = 0.0
        if announced:  # a promotion: it has its own words, and its own celebration
            pass
        elif before < self.lib.goal <= self.read_count:  # that paper made the week's goal
            self.say(self.pick(GOAL_MET), 8)
            self.celebrate("goal", delay=1.3)  # as the scroll reaches him
        else:
            self.say(self.pick(DONE), title=short(title, 36))
        if self.remind_every:
            self.next_remind = time.time() + self.remind_every

    # ---------- notes

    def keep_note(self, text, rect=None, heading="", from_box=False):
        """Save a note, and send its words as light into the scribe droid.

        rect is where the text was, in this window's pixels; None for a note from elsewhere."""
        try:
            if not self.notes.add(text, heading):
                return
        except OSError as e:
            self.say(f"Keep it, I cannot. Hmm.\n{e.strerror}.", 7)
            return
        self.note_from_box = from_box
        self._send_light(heading, text, rect)

    def keep_note_arrived(self):
        """A note was written from the command line: show it arriving."""
        self.note_from_box = False
        self._send_light("", "", None)

    def _send_light(self, heading, text, rect):
        s = self.S
        if rect is None:  # it arrives from the left, out of the space beside him
            rect = (self.cx - 200 * s, self.cy - 70 * s, 60 * s, 40 * s)
        x, y, w, h = rect
        self.note_ghost = (heading, text, x, y, w) if w > 150 else None
        tx, ty = self.present_spot if self.note_from_box else (self.droid_at or (60, -40))[:2]
        self.note_motes = []
        for _ in range(48):
            px = clamp(x + random.uniform(0, w), 2, self.width - 2)
            py = clamp(y + random.uniform(0, h), 2, self.height - 2)
            ux, uy = (px - self.cx) / s, (py - self.cy) / s
            mx, my = (ux + tx) / 2, (uy + ty) / 2
            if abs(mx) < 45 and -80 < my < 40:  # the way is through Yoda: arc over his head
                bend = (mx + random.uniform(-15, 15), random.uniform(-160, -140))
            else:  # a gentle swirl on the way
                bend = (mx + random.uniform(-25, 25), my + random.uniform(-35, 5))
            self.note_motes.append((ux, uy, *bend, random.uniform(0, 0.35)))
        self.note_t = 0.0

    def present_droid(self, rect):
        """The note box opened or moved (rect, in window pixels), or closed (None)."""
        if rect is None:
            self.present_rect = None
            return
        s = self.S
        x, y, w, h = ((rect[0] - self.cx) / s, (rect[1] - self.cy) / s, rect[2] / s, rect[3] / s)
        self.present_rect = (x, y, w, h)
        # below the box's right side, clear of his ear, so its beam fans up into the box
        self.present_spot = (clamp(x + w * 0.72, -self.cx / s + 12, -64), clamp(y + h + 24, -70, 30))

    def note_box_opened(self, opened):
        self.note_box_open = opened

    def note_typed(self):
        self.droid_pulse = max(self.droid_pulse, 0.35)

    # ---------- input

    def _set_input(self):
        if self.win.get_window():
            s = self.S
            x, y = int(self.cx - (ORBIT_RX + 20) * s), int(self.cy - 90 * s)
            self.win.input_shape_combine_region(cairo.Region(cairo.RectangleInt(
                x, max(0, y), int(2 * (ORBIT_RX + 20) * s), int(158 * s))))

    def _paper_at(self, x, y):
        """The orbiting scroll nearest the pointer, preferring ones in front."""
        best, best_key = None, None
        reach = 19 * self.S
        for i, (_paper, px, py, depth) in enumerate(self.slots):
            d = math.hypot(x - px, y - py)
            if d <= reach * (0.85 + 0.2 * depth):
                key = (-depth, d)
                if best_key is None or key < best_key:
                    best, best_key = i, key
        return best

    def _over_droid(self, x, y):
        """Is the pointer on the droid (and the droid not behind Yoda, where he'd be the one clicked)?"""
        if not self.notes or not self.droid_at:
            return False
        if self.droid_at[2] < -0.25 and self._over_yoda(x, y):
            return False
        dx, dy = self.droid_at[:2]
        return math.hypot((x - self.cx) / self.S - dx, (y - self.cy) / self.S - dy) <= DROID_R * 1.7

    def _over_yoda(self, x, y):
        dx, dy = (x - self.cx) / self.S, (y - self.cy) / self.S
        return (dx / 50) ** 2 + ((dy + 20) / 55) ** 2 <= 1  # head, ears and body

    def _on_motion(self, _w, event):
        self.pointer_inside = True
        self.pointer_u = ((event.x - self.cx) / self.S, (event.y - self.cy) / self.S)
        hover = self._paper_at(event.x, event.y)
        on_droid = self._over_droid(event.x, event.y)  # it's small and moving, so it wins
        hover = None if on_droid else hover
        if hover != self.hover or on_droid != self.hover_droid:
            self.hover, self.hover_droid = hover, on_droid
            self.win.queue_draw()
        now = time.time()
        if (hover is None and self._over_yoda(event.x, event.y) and now >= self.next_hover_talk
                and self.bubble is None):
            self.status(6, hover=True)
            self.next_hover_talk = now + HOVER_TALK_EVERY
        return False

    def _on_leave(self, *_):
        self.pointer_inside = False
        self.hover = None
        self.hover_droid = False
        return False

    def _on_press(self, _w, event):
        on_droid = event.button == 1 and self._over_droid(event.x, event.y)
        hit = self._paper_at(event.x, event.y) if event.button == 1 and not on_droid else None
        if on_droid:
            self.app.write_note(event.time)
        elif hit is not None:
            paper = self.slots[hit][0]
            try:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(paper.path).get_uri(), None)
            except GLib.Error:
                pass
            self.say(self.pick(OPENED), 5, title=short(paper.title, 36))
        elif event.button == 1:
            self.app.toggle_reading_list()
        elif event.button == 3:
            self.app.popup_menu(event)
        return True

    def _on_drop(self, _w, _ctx, x, y, data, _info, _time):
        uris = data.get_uris() or []
        files = [u for u in uris if Gio.File.new_for_uri(u).get_path()]
        if self.notes and not files:  # selected text, or links from a browser
            text = "\n".join(uris) if uris else (data.get_text() or "")
            if text.strip():
                self.keep_note(text, (x - 60, y - 20, 120, 40))
            return
        added = []
        for uri in files:
            path = Gio.File.new_for_uri(uri).get_path()
            if path and os.path.dirname(os.path.abspath(path)) != os.path.abspath(self.lib.todo):
                try:
                    added.append(self.lib.add(path))
                except OSError:
                    pass
        if added:
            self.refresh()
            if len(added) == 1:
                self.say(self.pick(ADDED), title=short(added[0].title, 36))
            else:
                self.say(f"{len(added)} papers, added they are.\nRead them, you must.")
            self.app.reading_list_changed()

    # ---------- animation

    def step(self, dt):
        now = time.time()
        if self.remind_every and now >= self.next_remind:
            self.remind()
        if now >= self.next_refresh:
            self.refresh()
            self.next_refresh = now + 60
        if now >= self.next_glance:
            self.glance_until = now + 2.2
            self.glance_side = random.choice(("look_left", "look_right"))
            self.next_glance = now + random.uniform(25, 60)
        reading = getattr(self.app, "reading", None)
        popup = reading is not None and reading.visible
        want_open = (self.bubble is not None or popup or self.absorb_t is not None
                     or self.note_box_open or self.note_t is not None
                     or now < self.glance_until or self.pointer_inside)
        k = min(1.0, dt * (5 if want_open else 2.5))
        self.eyes_open += ((1.0 if want_open else 0.0) - self.eyes_open) * k
        if self.eyes_open > 0.5 and random.random() < dt / 3.5:
            self.blink = 1.0
        self.blink = max(0.0, self.blink - dt * 8)
        target_speed = 0.0 if self.pointer_inside else 0.22
        self.orbit_speed += (target_speed - self.orbit_speed) * min(1.0, dt * 4)
        self.orbit = (self.orbit + dt * self.orbit_speed) % (2 * math.pi)
        if self.absorb_t is not None:
            self.absorb_t += dt
            if self.absorb_t > 1.9:
                self.absorb_t = None
        if self.note_t is not None:
            arrived = self.note_t < NOTE_FLIGHT + 0.35
            self.note_t += dt
            if arrived and self.note_t >= NOTE_FLIGHT + 0.35:
                self.droid_pulse = 1.0
                self.say(self.pick(NOTED), 5)
            if self.note_t > NOTE_FLIGHT + 0.6:
                self.note_t = self.note_ghost = None
        self.droid_pulse = max(0.0, self.droid_pulse - dt * 1.2)
        self._push_stones(dt)
        # It flies over to project the note box, and stays until the note's light reaches it.
        projecting = self.present_rect is not None or (
            self.note_from_box and self.note_t is not None and self.note_t < NOTE_FLIGHT + 0.9)
        self.droid_present += ((1.0 if projecting else 0.0) - self.droid_present) * min(1.0, dt * 4)
        if self.droid_present < 0.002:
            self.droid_present = 0.0
        # The droid pauses to listen while you write, and when you reach for it.
        still = self.note_box_open or self.pointer_inside
        self.droid_speed += ((0.0 if still else DROID_SPEED) - self.droid_speed) * min(1.0, dt * 3)
        self.droid_a = (self.droid_a + dt * self.droid_speed) % (2 * math.pi)
        if self.bubble and now > self.bubble_until:
            self.bubble = None

    @property
    def busy(self):
        return (self.absorb_t is not None or self.bubble is not None or self.pointer_inside
                or self.note_box_open or self.note_t is not None or self.droid_pulse > 0
                or 0.0 < self.droid_present < 0.99
                or time.time() < self.glance_until
                or self.celebration is not None
                or any(abs(p[0]) + abs(p[1]) > 0.3 for p in self.stone_push)
                or 0.02 < self.eyes_open < 0.98 or self.blink > 0)

    # ---------- per-frame drawing

    def _on_draw(self, _w, cr):
        clear(cr)
        now = time.time()
        s = self.S
        bob = math.sin(now * 0.9) * 3.0  # levitating
        breath = math.sin(now * 1.3)
        cr.save()
        cr.translate(self.cx, self.cy)
        cr.scale(s, s)
        self._draw_aura(cr, now, bob)
        self._draw_meditation(cr, now, bob)
        self._draw_celebration(cr, now, bob)
        papers = self._orbit_papers(bob)
        for i, p in enumerate(papers):
            if p[2] < 0:
                self._draw_scroll_in_orbit(cr, *p, hovered=i == self.hover)
        if self.notes:
            self.droid_at = self._droid_pos(now, bob)
            if self.droid_at[2] < 0:
                self._draw_scribe(cr, now, *self.droid_at)
        self._draw_force_glow(cr, now, bob)
        self.slots = [(self.unread[i], self.cx + x * s, self.cy + y * s, depth)
                      for i, (x, y, depth, _a) in enumerate(papers)]
        # Cached body, breathing slightly.
        cr.save()
        cr.translate(0, bob)
        bx, by, _bw, bh = self.body_box
        cr.translate(0, by + bh)
        cr.scale(1 + breath * 0.006, 1 - breath * 0.004)
        cr.translate(bx, -bh)
        cr.scale(1 / s, 1 / s)
        cr.set_source_surface(self.body, 0, 0)
        cr.paint()
        cr.restore()
        # His head, which turns a little as he talks, blinks, and glances aside.
        speaking = self.bubble is not None and now < self.bubble_text_at + self._typing_time() + 0.3
        tilt = math.sin(now * 1.2) * 0.018 if speaking else math.sin(now * 0.45) * 0.015
        cr.save()
        cr.translate(0, bob + breath * 0.35)
        cr.translate(*self.look.neck)
        cr.rotate(tilt)
        cr.translate(-self.look.neck[0], -self.look.neck[1])
        self._draw_rendered_head(cr, now, speaking)
        cr.restore()
        for i, p in enumerate(papers):
            if p[2] >= 0:
                self._draw_scroll_in_orbit(cr, *p, hovered=i == self.hover)
        self._draw_hand_light(cr, now, bob)
        self._draw_stones(cr, now, bob)

        if self.absorb_t is not None:
            self._draw_absorb(cr, bob)
        if self.notes:
            if self.droid_at[2] >= 0:
                self._draw_beam(cr, now)
                self._draw_scribe(cr, now, *self.droid_at)
            if self.note_t is not None:
                self._draw_note_flight(cr)
        self._draw_pips(cr)
        cr.restore()
        if self.note_ghost and self.note_t is not None:
            self._draw_note_ghost(cr)
        self._draw_rank_banner(cr, now)
        if self.hover_droid and self.droid_at:
            self._draw_droid_label(cr)
        if self.hover is not None and self.hover < len(self.slots):
            self._draw_hover_title(cr, *self.slots[self.hover])
        if self.bubble:
            self._draw_bubble(cr)
        return False

    AURA_RES = 1.5  # glow pixels per unit; it is blurry anyway

    def _render_aura(self):
        """A soft halo that hugs Yoda's outline: his silhouette, smeared outward."""
        x0, y0, w, h = self.aura_box
        k = self.AURA_RES
        sil = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(w * k), int(h * k))
        cr = cairo.Context(sil)
        cr.scale(k, k)
        cr.translate(-x0, -y0)
        cr.save()
        cr.translate(self.body_box[0], self.body_box[1])
        cr.scale(1 / self.S, 1 / self.S)
        cr.set_source_surface(self.body, 0, 0)
        cr.paint()
        cr.restore()
        cr.save()
        cr.translate(*self.look.head_at)
        cr.scale(1 / self.S, 1 / self.S)
        cr.set_source_surface(self.look.head[self.look.seq["wake"][-1]], 0, 0)
        cr.paint()
        cr.restore()
        glow = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(w * k), int(h * k))
        cr = cairo.Context(glow)
        for r in range(1, 19):
            fall = (1 - r / 19) ** 1.3
            for j in range(24):
                a = (j + 0.5 * (r % 2)) / 24 * 2 * math.pi
                cr.save()
                cr.translate(math.cos(a) * r, math.sin(a) * r * 0.9 - r * 0.25)  # it rises a little
                cr.set_source_rgba(*FORCE, 0.028 * fall)
                cr.mask_surface(sil, 0, 0)
                cr.restore()
        return glow

    def _draw_force_glow(self, cr, now, bob):
        x0, y0, w, h = self.aura_box
        pulse = 0.5 + 0.5 * math.sin(now * 0.8)
        flicker = 0.5 + 0.5 * math.sin(now * 2.3 + math.sin(now * 0.7) * 2)
        boost = 1.0 + (0.9 if self.absorb_t is not None else 0.0)
        cr.save()
        cr.translate(0, bob - 10)
        # swelling as he breathes in, settling as he breathes out
        strength = 1.3 * (0.8 + 0.35 * (0.5 + 0.5 * math.sin(now * 1.3)))
        for grow, alpha in ((1.0 + 0.02 * pulse, (0.24 + 0.10 * pulse) * strength), (1.06 + 0.03 * flicker, 0.09 * strength)):
            cr.save()
            cr.scale(grow, grow)
            cr.translate(x0, y0 + 10)
            cr.scale(1 / self.AURA_RES, 1 / self.AURA_RES)
            cr.set_source_surface(self.aura, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint_with_alpha(min(1.0, alpha * boost))
            cr.restore()
        cr.restore()

    def _draw_meditation(self, cr, now, bob):
        """While he meditates: slow ripples of the Force, one with each breath, rising off him."""
        calm = clamp(1.0 - self.eyes_open * 1.6, 0.0, 1.0)
        if calm <= 0.01:
            return
        for k in range(2):
            p = (now / 4.2 + k / 2) % 1.0
            r = 30 + 85 * p
            fade = math.sin(math.pi * min(1.0, p * 1.4)) * (1 - p)
            cr.save()
            cr.translate(0, -12 + bob - 10 * p)
            cr.scale(1, 0.72)
            cr.arc(0, 0, r, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgba(*FORCE, 0.42 * fade * calm)
            cr.set_line_width(1.1 + 1.4 * (1 - p))
            cr.stroke()

    def _draw_aura(self, cr, now, bob):
        pulse = 0.5 + 0.5 * math.sin(now * 0.8)
        boost = 1.0 + (0.8 if self.absorb_t is not None else 0.0)
        g = cairo.RadialGradient(0, -14 + bob, 4, 0, -14 + bob, 100)
        g.add_color_stop_rgba(0, *FORCE, (0.10 + 0.05 * pulse) * boost)
        g.add_color_stop_rgba(0.5, *FORCE, 0.04 * boost)
        g.add_color_stop_rgba(1, *FORCE, 0)
        cr.set_source(g)
        cr.arc(0, -14 + bob, 100, 0, 2 * math.pi)
        cr.fill()
        # A faint ring of light under him, like a disturbance in the Force.
        cr.save()
        cr.translate(0, self.floor_y + bob * 0.4)
        cr.scale(1, 0.18)
        g = cairo.RadialGradient(0, 0, 20, 0, 0, 52)
        g.add_color_stop_rgba(0, *FORCE, 0)
        g.add_color_stop_rgba(0.75, *FORCE, 0.16 + 0.06 * pulse)
        g.add_color_stop_rgba(1, *FORCE, 0)
        cr.set_source(g)
        cr.arc(0, 0, 52, 0, 2 * math.pi)
        cr.fill()
        cr.restore()



    # ---------- scrolls

    def _orbit_papers(self, bob):
        """(x, y, depth, angle) for each orbiting scroll."""
        n = min(len(self.unread), MAX_ORBIT)
        out = []
        for i in range(n):
            a = self.orbit + i * 2 * math.pi / max(n, 1)
            x = math.cos(a) * ORBIT_RX
            y = ORBIT_Y + math.sin(a) * ORBIT_RY + bob * 0.6
            out.append((x, y, math.sin(a), a))
        return out

    def _draw_scroll_in_orbit(self, cr, x, y, depth, a, hovered=False):
        scale = (0.82 + 0.22 * depth) * (1.25 if hovered else 1.0)
        alpha = 1.0 if hovered else 0.6 + 0.4 * (depth + 1) / 2
        cr.save()
        cr.translate(x, y)
        cr.rotate(math.cos(a) * 0.2 + 0.06)
        cr.scale(scale, scale)
        g = cairo.RadialGradient(0, 0, 0, 0, 0, 26)
        g.add_color_stop_rgba(0, *FORCE, (0.5 if hovered else 0.16) * alpha)
        g.add_color_stop_rgba(1, *FORCE, 0)
        cr.set_source(g)
        cr.arc(0, 0, 26, 0, 2 * math.pi)
        cr.fill()
        self._scroll(cr, alpha)
        cr.restore()

    def _scroll(self, cr, alpha):
        """A parchment scroll between two wooden rods."""
        w, h = SCROLL_W, SCROLL_H
        cr.move_to(-w / 2, -h / 2 + 2)
        cr.curve_to(-w / 2 + 1, -3, -w / 2 + 1, 3, -w / 2, h / 2 - 2)
        cr.line_to(w / 2, h / 2 - 2)
        cr.curve_to(w / 2 - 1, 3, w / 2 - 1, -3, w / 2, -h / 2 + 2)
        cr.close_path()
        g = cairo.LinearGradient(-w / 2, 0, w / 2, 0)
        g.add_color_stop_rgba(0, 0.80, 0.70, 0.50, alpha)
        g.add_color_stop_rgba(0.35, 0.96, 0.90, 0.76, alpha)
        g.add_color_stop_rgba(1, 0.78, 0.67, 0.47, alpha)
        cr.set_source(g)
        cr.fill()
        cr.set_source_rgba(0.35, 0.25, 0.15, 0.55 * alpha)
        for k in range(7):
            width = 14.5 - (5.5 if k == 6 else (k % 2) * 2)  # the last line is short
            cr.rectangle(-7.5, -8.5 + k * 2.6, width, 0.6)
        cr.fill()
        for y in (-h / 2 + 2, h / 2 - 2):
            rod = cairo.LinearGradient(0, y - 2.2, 0, y + 2.2)
            rod.add_color_stop_rgba(0, 0.64, 0.46, 0.27, alpha)
            rod.add_color_stop_rgba(0.5, 0.46, 0.31, 0.17, alpha)
            rod.add_color_stop_rgba(1, 0.28, 0.18, 0.09, alpha)
            rounded_rect(cr, -w / 2 - 1.4, y - 2.2, w + 2.8, 4.4, 2.2)
            cr.set_source(rod)
            cr.fill()
            for x in (-w / 2 - 3, w / 2 + 3):
                knob = cairo.RadialGradient(x - 0.5, y - 0.7, 0.2, x, y, 2.6)
                knob.add_color_stop_rgba(0, 0.74, 0.54, 0.31, alpha)
                knob.add_color_stop_rgba(1, 0.30, 0.19, 0.09, alpha)
                cr.set_source(knob)
                cr.arc(x, y, 2.6, 0, 2 * math.pi)
                cr.fill()

    def _draw_absorb(self, cr, bob):
        """A read scroll spirals from the ring into Yoda and flashes into light."""
        t = self.absorb_t
        head = (0, HEAD_Y + bob)
        if t < 1.3:
            p = t / 1.3
            e = p * p
            ang = 0.4 + p * 5.5
            r = (1 - e) * ORBIT_RX
            x = head[0] + math.cos(ang) * r
            y = head[1] + (ORBIT_Y - HEAD_Y) * (1 - e) + math.sin(ang) * r * 0.25
            cr.save()
            cr.translate(x, y)
            cr.rotate(p * 7)
            cr.scale(1 - 0.75 * e, 1 - 0.75 * e)
            g = cairo.RadialGradient(0, 0, 0, 0, 0, 24)
            g.add_color_stop_rgba(0, *FORCE, 0.6)
            g.add_color_stop_rgba(1, *FORCE, 0)
            cr.set_source(g)
            cr.arc(0, 0, 24, 0, 2 * math.pi)
            cr.fill()
            self._scroll(cr, 1 - 0.5 * e)
            cr.restore()
        else:
            p = (t - 1.3) / 0.6
            r = 8 + 44 * p
            g = cairo.RadialGradient(*head, 0, *head, r)
            g.add_color_stop_rgba(0, 1, 1, 1, 0.8 * (1 - p))
            g.add_color_stop_rgba(0.3, *FORCE, 0.5 * (1 - p))
            g.add_color_stop_rgba(1, *FORCE, 0)
            cr.set_source(g)
            cr.arc(*head, r, 0, 2 * math.pi)
            cr.fill()

    # ---------- celebrations

    def celebrate(self, kind="goal", delay=0.0):
        """The Force, at full strength: a shockwave and sparks, and the stones swirling round him.
        For a met goal, and (longer, with his new rank's name) for a promotion."""
        self.celebration = (kind if kind in CELEBRATION else "goal", time.time() + delay)
        self.sparks = []

    def _celebration(self, now):
        """(kind, seconds in, 0..1 how strongly it shows) or None."""
        if not self.celebration:
            return None
        kind, start = self.celebration
        t = now - start
        dur = CELEBRATION[kind]
        if t < 0:
            return None
        if t > dur:
            self.celebration = None
            return None
        return kind, t, clamp(min(t / 0.6, (dur - t) / 0.9), 0.0, 1.0)

    def _draw_celebration(self, cr, now, bob):
        c = self._celebration(now)
        if not c:
            return
        kind, t, k = c
        # the shockwave, and a second one for a promotion
        for delay in ((0.0, 0.5) if kind == "rank" else (0.0,)):
            p = (t - delay) / 1.1
            if 0 <= p < 1:
                cr.save()
                cr.translate(0, -18 + bob)
                cr.scale(1, 0.8)
                cr.arc(0, 0, 12 + 125 * (1 - (1 - p) ** 2), 0, 2 * math.pi)
                cr.restore()
                cr.set_source_rgba(*FORCE, 0.7 * (1 - p))
                cr.set_line_width(3.5 * (1 - p) + 0.5)
                cr.stroke()
        # sparks burst out from behind him
        if not self.sparks:
            rng = random.Random()
            for _ in range(70 if kind == "rank" else 50):
                a = rng.uniform(0, 2 * math.pi)
                v = rng.uniform(35, 95)
                self.sparks.append((0.0, -20.0, math.cos(a) * v, math.sin(a) * v * 0.8, 0.0))
        for x0, y0, vx, vy, born in self.sparks:
            age = t - born
            if not 0 <= age < 1.8:
                continue
            drag = (1 - math.exp(-age * 2.2)) / 2.2  # they slow as they fly
            x, y = x0 + vx * drag, y0 + vy * drag + bob - 6 * age
            a = (1 - age / 1.8) ** 1.5
            g = cairo.RadialGradient(x, y, 0, x, y, 3.5)
            g.add_color_stop_rgba(0, 0.9, 1.0, 0.92, 0.9 * a)
            g.add_color_stop_rgba(0.3, *FORCE, 0.5 * a)
            g.add_color_stop_rgba(1, *FORCE, 0)
            cr.set_source(g)
            cr.arc(x, y, 3.5, 0, 2 * math.pi)
            cr.fill()
        # the aura flares
        g = cairo.RadialGradient(0, -18 + bob, 5, 0, -18 + bob, 110)
        g.add_color_stop_rgba(0, *FORCE, 0.28 * k)
        g.add_color_stop_rgba(1, *FORCE, 0)
        cr.set_source(g)
        cr.arc(0, -18 + bob, 110, 0, 2 * math.pi)
        cr.fill()

    def _draw_rank_banner(self, cr, now):
        """His new rank, glowing beneath him as he's promoted."""
        c = self._celebration(now)
        if not c or c[0] != "rank":
            return
        _kind, t, k = c
        a = clamp((t - 1.0) / 0.8, 0.0, 1.0) * k
        if a <= 0:
            return
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"{SANS} Light 15"))
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_letter_spacing_new(int(6 * Pango.SCALE)))
        layout.set_attributes(attrs)
        layout.set_text(self.rank.upper(), -1)
        tw, th = layout.get_pixel_size()
        x = self.cx - tw / 2
        y = min(self.cy + (self.floor_y + 13) * self.S, self.height - th - 3)
        for dx, dy, glow in ((0, 0, 0.35), (1, 0, 0.2), (-1, 0, 0.2), (0, 1, 0.2), (0, -1, 0.2)):
            cr.move_to(x + dx * 1.5, y + dy * 1.5)
            cr.set_source_rgba(*FORCE, glow * a)
            PangoCairo.show_layout(cr, layout)
        cr.move_to(x, y)
        cr.set_source_rgba(0.92, 1.0, 0.94, 0.95 * a)
        PangoCairo.show_layout(cr, layout)

    # ---------- the rendered head

    def _draw_rendered_head(self, cr, now, speaking):
        """Show the one rendered frame for this moment: waking from meditation, blinking,
        talking or glancing aside. (Only ever one: blending two differently posed heads
        would show both.)"""
        seq = self.look.seq

        def step(frames, amount, rest=None):
            i = round(clamp(amount, 0.0, 1.0) * len(frames))
            return frames[i - 1] if i > 0 else rest

        awake = seq["wake"][-1]
        e = clamp(self.eyes_open, 0.0, 1.0)
        name = None
        talking = speaking and self.bubble and now >= self.bubble_text_at
        if e < (0.9 if talking else 0.995):
            name = seq["wake"][round(e * (len(seq["wake"]) - 1))]
        elif self.blink > 0:  # closing, then opening again
            name = step(seq["blink"], math.sin(math.pi * (1 - self.blink)))
        elif talking:
            amount = self._mouth(now)
            kind = getattr(self, "_mouth_kind", "open")
            frames = seq.get("talk_" + kind, seq["talk"]) if kind != "open" else seq["talk"]
            name = step(frames, amount)
        elif now < self.glance_until:
            left = self.glance_until - now
            g = min(2.2 - left, left) / 0.45  # turn aside, hold, and back
            g = clamp(g, 0.0, 1.0)
            name = step(seq[self.glance_side], g * g * (3 - 2 * g))
        cr.translate(*self.look.head_at)
        cr.scale(1 / self.S, 1 / self.S)
        cr.set_source_surface(self.look.head[name or awake], 0, 0)
        cr.paint()

    # ---------- the Force, made visible

    def _stone_positions(self, now, bob):
        """Where each lifted stone is: bobbing, higher while he meditates, swirling round him
        when he celebrates, and nudged away from your pointer."""
        calm = clamp(1.0 - self.eyes_open, 0.0, 1.0)
        c = self._celebration(now)
        swirl = c[2] if c else 0.0
        out = []
        for i, (x, y, r, phase) in enumerate(self.stones):
            t = now * 0.7 + phase
            lift = calm * (10 + 4 * i % 3)  # meditating, he lifts them higher
            sy = y - lift + math.sin(t) * (3.5 + 2.5 * calm) + bob * 0.3
            sx = x + math.sin(t * 0.5) * (1.5 + 5 * calm)
            if swirl:  # celebrating: all of them fly up and circle him
                a = phase * 1.6 + c[1] * 2.4
                ox, oy = math.cos(a) * (68 + 6 * i), -22 + math.sin(a) * 20 + bob
                sx, sy = sx + (ox - sx) * swirl, sy + (oy - sy) * swirl
            px, py = self.stone_push[i]
            out.append((sx + px, sy + py, r, phase, t))
        return out

    def _push_stones(self, dt):
        """The Force, answering you: stones near the pointer drift away from it, then float back."""
        now = time.time()
        bob = math.sin(now * 0.9) * 3.0
        for i, (sx, sy, r, _phase, _t) in enumerate(self._stone_positions(now, bob)):
            push = self.stone_push[i]
            tx = ty = 0.0
            if self.pointer_u:
                dx, dy = sx - push[0] - self.pointer_u[0], sy - push[1] - self.pointer_u[1]
                d = math.hypot(dx, dy) or 1.0
                if d < 34:
                    f = (34 - d) / 34 * 22
                    tx, ty = dx / d * f, dy / d * f
            k = min(1.0, dt * (6 if (tx or ty) else 1.6))  # quick to move away, slow to drift back
            push[0] += (tx - push[0]) * k
            push[1] += (ty - push[1]) * k

    def _draw_stones(self, cr, now, bob):
        """A few small stones, lifted by the Force, bobbing and turning slowly."""
        calm = clamp(1.0 - self.eyes_open, 0.0, 1.0)
        for i, (sx, sy, r, phase, t) in enumerate(self._stone_positions(now, bob)):
            ang = math.sin(t * 0.6) * 0.5 + phase + calm * now * 0.15
            cr.save()
            cr.translate(sx, sy)
            # the Force beneath it
            cr.save()
            cr.translate(0, r * 1.7)
            cr.scale(1, 0.35)
            g = cairo.RadialGradient(0, 0, 0, 0, 0, r * 1.8)
            g.add_color_stop_rgba(0, *FORCE, 0.4)
            g.add_color_stop_rgba(1, *FORCE, 0)
            cr.set_source(g)
            cr.arc(0, 0, r * 1.8, 0, 2 * math.pi)
            cr.fill()
            cr.restore()
            cr.rotate(ang)
            rng = random.Random(i * 7 + 3)
            pts = [(math.cos(k / 7 * 2 * math.pi) * r * rng.uniform(0.75, 1.1),
                    math.sin(k / 7 * 2 * math.pi) * r * rng.uniform(0.55, 0.8)) for k in range(7)]
            cr.move_to(*pts[0])
            for p in pts[1:]:
                cr.line_to(*p)
            cr.close_path()
            g = cairo.LinearGradient(-r, -r, r, r)
            g.add_color_stop_rgb(0, 0.72, 0.70, 0.64)
            g.add_color_stop_rgb(0.55, 0.48, 0.46, 0.41)
            g.add_color_stop_rgb(1, 0.24, 0.24, 0.22)
            cr.set_source(g)
            cr.fill_preserve()
            cr.set_source_rgba(*FORCE, 0.6)  # the Force's light along its edge
            cr.set_line_width(0.7)
            cr.stroke()
            cr.restore()

    def _draw_hand_light(self, cr, now, bob):
        """A soft light in his hands, swelling as he breathes in."""
        breath = 0.5 + 0.5 * math.sin(now * 1.3)
        calm = clamp(1.0 - self.eyes_open, 0.0, 1.0)
        for hx, hy in self.hands:
            r = 10 + 4 * breath
            g = cairo.RadialGradient(hx, hy + bob, 0, hx, hy + bob, r)
            g.add_color_stop_rgba(0, 0.88, 1.0, 0.93, (0.45 + 0.3 * breath) * (0.55 + 0.45 * calm))
            g.add_color_stop_rgba(0.35, *FORCE, (0.25 + 0.15 * breath) * (0.55 + 0.45 * calm))
            g.add_color_stop_rgba(1, *FORCE, 0)
            cr.set_source(g)
            cr.arc(hx, hy + bob, r, 0, 2 * math.pi)
            cr.fill()

    # ---------- the scribe droid

    def _droid_pos(self, now, bob):
        """(x, y, depth, facing): a diagonal orbit, top right to bottom left, round Yoda.

        It passes in front of his chest and behind his head; while the note box is open
        it flies to just below it instead."""
        a = self.droid_a
        along, across = math.cos(a) * 95, math.sin(a) * 22
        x = 0.8 * along + 0.6 * across  # along the diagonal (0.8, -0.6), and across it
        y = -17 - 0.6 * along + 0.8 * across  # low enough that its antenna stays in view
        depth = math.sin(a)
        facing = math.atan2(-math.sin(a) * 0.8, math.cos(a) * 0.25)  # the way it's flying
        k = self.droid_present
        if k > 0:
            e = k * k * (3 - 2 * k)
            sx, sy = self.present_spot
            x, y = x + (sx - x) * e, y + (sy - y) * e
            depth = depth + (1.0 - depth) * e
            if k > 0.5:
                facing = -0.8  # turned up toward the box
        y += bob * 0.5 + math.sin(now * 1.7) * 1.2
        y -= math.sin(math.pi * min(1.0, self.droid_pulse)) * 3  # a happy hop when a note arrives
        return x, y, depth, facing

    def _draw_beam(self, cr, now):
        """The droid's projection: a soft cone of light from its eye up to the note box."""
        k = self.droid_present
        if self.present_rect is None or k < 0.3:
            return
        x, y, w, h = self.present_rect
        dx, dy = self.droid_at[:2]
        ex, ey = dx - DROID_R * 0.5, dy - DROID_R * 0.3
        a = (k - 0.3) / 0.7 * (0.8 + 0.2 * math.sin(now * 9) * math.sin(now * 2.3))
        left, right = x + w * 0.3, x + w * 0.97
        cr.move_to(ex, ey)
        cr.line_to(right, y + h)
        cr.line_to(left, y + h)
        cr.close_path()
        g = cairo.LinearGradient(ex, ey, (left + right) / 2, y + h)
        g.add_color_stop_rgba(0, *FORCE, 0.30 * a)
        g.add_color_stop_rgba(1, *FORCE, 0.06 * a)
        cr.set_source(g)
        cr.fill()

    def _draw_scribe(self, cr, now, x, y, depth, facing):
        """A small floating sphere droid with one green eye, like a Jedi training remote."""
        r = DROID_R * (1 + 0.1 * depth)
        dim = 0.78 + 0.22 * (depth + 1) / 2  # a little darker behind him
        awake = 1.0 if (self.note_box_open or self.hover_droid) else 0.0
        cr.save()
        cr.translate(x, y)
        # the repulsor glow beneath it
        g = cairo.RadialGradient(0, r + 2, 0, 0, r + 2, r * 1.3)
        g.add_color_stop_rgba(0, *FORCE, 0.45 + 0.3 * self.droid_pulse)
        g.add_color_stop_rgba(1, *FORCE, 0)
        cr.set_source(g)
        cr.arc(0, r + 2, r * 1.3, 0, 2 * math.pi)
        cr.fill()
        if self.droid_pulse > 0:  # a ring of light as a note arrives
            p = 1 - self.droid_pulse
            cr.arc(0, 0, r * (1.2 + 1.6 * p), 0, 2 * math.pi)
            cr.set_source_rgba(*FORCE, 0.6 * self.droid_pulse)
            cr.set_line_width(0.8)
            cr.stroke()
        # the antenna, with a light that blinks when it's listening
        cr.move_to(r * 0.35, -r * 0.85)
        cr.line_to(r * 0.55, -r * 1.55)
        cr.set_source_rgb(0.35 * dim, 0.38 * dim, 0.42 * dim)
        cr.set_line_width(0.45)
        cr.stroke()
        blink = (0.5 + 0.5 * math.sin(now * 6)) if awake else 0.25
        cr.arc(r * 0.55, -r * 1.6, 0.6, 0, 2 * math.pi)
        cr.set_source_rgba(*FORCE, 0.4 + 0.6 * max(blink, self.droid_pulse))
        cr.fill()
        # the body: a brushed-metal sphere, lit from the upper left
        g = cairo.RadialGradient(-r * 0.35, -r * 0.4, r * 0.1, 0, 0, r)
        g.add_color_stop_rgb(0, 0.90 * dim, 0.92 * dim, 0.94 * dim)
        g.add_color_stop_rgb(0.7, 0.58 * dim, 0.62 * dim, 0.67 * dim)
        g.add_color_stop_rgb(1, 0.30 * dim, 0.33 * dim, 0.38 * dim)
        cr.arc(0, 0, r, 0, 2 * math.pi)
        cr.set_source(g)
        cr.fill()
        cr.save()
        cr.arc(0, 0, r, 0, 2 * math.pi)
        cr.clip()
        # a dark band round its middle, with vents that turn with it
        cr.save()
        cr.translate(0, r * 0.12)
        cr.scale(1, 0.3)
        cr.arc(0, 0, r, 0, math.pi)
        cr.restore()
        cr.set_source_rgba(0.18, 0.20, 0.24, 0.85 * dim)
        cr.set_line_width(r * 0.3)
        cr.stroke()
        for k in range(8):
            phi = k * math.pi / 4 + facing
            if math.cos(phi) <= 0.1:
                continue
            vx = math.sin(phi) * r * 0.93
            vy = r * 0.12 + math.cos(phi) * r * 0.3
            cr.rectangle(vx - 0.35 * math.cos(phi), vy - 0.5, 0.7 * math.cos(phi), 1.0)
            cr.set_source_rgba(0.55, 0.62, 0.68, 0.7 * dim)
            cr.fill()
        # its eye, on the side it faces, foreshortened as it turns away
        if math.cos(facing) > -0.15:
            turn = max(0.0, math.cos(facing))
            ex, ey = math.sin(facing) * r * 0.62, -r * 0.22
            cr.save()
            cr.translate(ex, ey)
            cr.scale(0.35 + 0.65 * turn, 1)
            cr.arc(0, 0, r * 0.32, 0, 2 * math.pi)
            cr.set_source_rgb(0.08, 0.10, 0.12)
            cr.fill()
            glow = 0.55 + 0.25 * awake + 0.4 * self.droid_pulse
            g = cairo.RadialGradient(0, 0, 0, 0, 0, r * 0.24)
            g.add_color_stop_rgba(0, 0.85, 1.0, 0.92, min(1.0, glow))
            g.add_color_stop_rgba(0.6, *FORCE, min(1.0, glow * 0.9))
            g.add_color_stop_rgba(1, *FORCE, 0.2)
            cr.arc(0, 0, r * 0.24, 0, 2 * math.pi)
            cr.set_source(g)
            cr.fill()
            cr.restore()
        cr.restore()
        # rim of starlight on the right, like Yoda
        cr.arc(0, 0, r - 0.3, -0.9, 0.6)
        cr.set_source_rgba(*RIM, 0.5 * dim)
        cr.set_line_width(0.5)
        cr.stroke()
        cr.restore()

    def _draw_note_flight(self, cr):
        """The note's words, as specks of light, curving into the droid wherever it is."""
        hx, hy = self.droid_at[:2]
        for x0, y0, bx, by, delay in self.note_motes:
            p = clamp((self.note_t - delay) / NOTE_FLIGHT, 0.0, 1.0)
            if p <= 0 or p >= 1:
                continue
            for k, trail in enumerate((0.0, 0.04, 0.08)):
                q = max(0.0, p - trail)
                e = q * q * (3 - 2 * q)
                u = 1 - e  # a quadratic curve from the text, bending round Yoda, into the droid
                x = u * u * x0 + 2 * u * e * bx + e * e * hx
                y = u * u * y0 + 2 * u * e * by + e * e * hy
                a = math.sin(math.pi * p) ** 0.6 * (0.9, 0.45, 0.2)[k]
                cr.set_source_rgba(*(FORCE if k else (0.88, 1.0, 0.94)), a)
                cr.arc(x, y, (1.1, 0.8, 0.6)[k], 0, 2 * math.pi)
                cr.fill()

    def _draw_note_ghost(self, cr):
        """The words just written, fading where they were as their light leaves."""
        heading, text, x, y, w = self.note_ghost
        p = clamp(self.note_t / 0.6, 0.0, 1.0)
        if p >= 1:
            return
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"{SANS} 11.5"))
        layout.set_width(int(w * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        markup = GLib.markup_escape_text(text)
        if heading:
            markup = f'<span size="130%" weight="light">{GLib.markup_escape_text(heading)}</span>\n' + markup
        layout.set_markup(markup, -1)
        cr.move_to(x, y - 6 * p)
        cr.set_source_rgba(0.80, 1.0, 0.90, 0.9 * (1 - p) ** 2)
        PangoCairo.show_layout(cr, layout)

    def _draw_droid_label(self, cr):
        n = self.notes.count
        text = f"Write a note  ·  {n} kept" if n else "Write a note"
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"{SANS} 10"))
        layout.set_text(text, -1)
        tw, th = layout.get_pixel_size()
        bw, bh = tw + 20, th + 10
        x = self.cx + self.droid_at[0] * self.S
        bx = clamp(x - bw / 2, 4, self.width - bw - 4)
        by = self.cy + (self.droid_at[1] - DROID_R * 2.4) * self.S - bh
        if by < 4:
            by = self.cy + (self.droid_at[1] + DROID_R * 2.4) * self.S
        rounded_rect(cr, bx, by, bw, bh, bh / 2)
        cr.set_source_rgba(0.04, 0.07, 0.06, 0.88)
        cr.fill_preserve()
        cr.set_source_rgba(*FORCE, 0.45)
        cr.set_line_width(1)
        cr.stroke()
        cr.move_to(bx + 10, by + 5)
        cr.set_source_rgba(0.93, 0.98, 0.92, 0.95)
        PangoCairo.show_layout(cr, layout)

    # ---------- pips, titles and speech

    def _draw_pips(self, cr):
        goal, count = self.lib.goal, self.read_count
        shown = min(goal, 10)
        gap = 7
        x0 = -(shown - 1) * gap / 2
        py = self.floor_y + 8
        for i in range(shown):
            x = x0 + i * gap
            if i < count:
                g = cairo.RadialGradient(x, py, 0, x, py, 5)
                g.add_color_stop_rgba(0, *GREEN, 0.7)
                g.add_color_stop_rgba(1, *GREEN, 0)
                cr.set_source(g)
                cr.arc(x, py, 5, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(0.85, 1.0, 0.8, 0.95)
            else:
                cr.set_source_rgba(1, 1, 1, 0.18)
            cr.arc(x, py, 1.6, 0, 2 * math.pi)
            cr.fill()

    def _draw_hover_title(self, cr, paper, x, y, _depth):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"{SANS} 10"))
        layout.set_width(int(240 * Pango.SCALE))
        layout.set_ellipsize(3)  # end
        layout.set_text(paper.title, -1)
        tw, th = layout.get_pixel_size()
        bw, bh = tw + 20, th + 10
        bx = clamp(x - bw / 2, 4, self.width - bw - 4)
        by = y - 24 * self.S - bh
        if by < 4:
            by = y + 20 * self.S
        rounded_rect(cr, bx, by, bw, bh, bh / 2)
        cr.set_source_rgba(0.04, 0.07, 0.06, 0.88)
        cr.fill_preserve()
        cr.set_source_rgba(*FORCE, 0.45)
        cr.set_line_width(1)
        cr.stroke()
        cr.move_to(bx + 10, by + 5)
        cr.set_source_rgba(0.93, 0.98, 0.92, 0.95)
        PangoCairo.show_layout(cr, layout)

    def _draw_bubble(self, cr):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f"{SANS} 12"))
        layout.set_line_spacing(1.35)
        # The bubble floats up and to the left of the head, clear of the ear, with a short
        # tail that points at him without touching him.
        gap = (0.3 - self.ear_tip[0]) * self.S  # the bubble's right edge sits just above the tip of his left ear
        pad_x, pad_y = 16, 12
        max_w = int(self.cx - gap - 2 * pad_x - 6)
        layout.set_width(int(clamp(max_w, 150, 300) * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD)
        layout.set_text(self.bubble, -1)
        tw, th = layout.get_pixel_size()
        bw, bh = tw + 2 * pad_x, th + 2 * pad_y
        now = time.time()
        bx = self.cx - gap - bw
        by = self.cy + (self.ear_tip[1] - 10) * self.S - bh + math.sin(now * 1.7) * 1.5 * self.S
        if by < 6:  # no room above: slide the bubble left as it comes down, so the tail stays clear of the ear
            bx -= (6 - by) * 1.2
            by = 6
        bx = max(6, bx)
        tail = clamp(6.5 * self.S, 8, 18)
        tip = (bx + bw + tail * 0.7, by + bh + tail * 0.55)  # toward the top of his ear, stopping short of it
        age = now - self.bubble_at
        if age < 0:
            return  # he's still opening his eyes
        left = self.bubble_until - now
        if age < BUBBLE_IN:  # pop out with a little overshoot
            t = age / BUBBLE_IN
            t -= 1
            size = 1 + (t * t * ((1.9 + 1) * t + 1.9))
            fade = min(1.0, age / 0.12)
        elif left < BUBBLE_OUT:  # shrink back toward Yoda
            t = max(0.0, left / BUBBLE_OUT)
            size = t * t
            fade = t
        else:
            size = fade = 1.0
        if size <= 0.01:
            return
        cr.save()
        cr.translate(*tip)
        cr.scale(size, size)
        cr.translate(-tip[0], -tip[1])
        ink = (0.05, 0.08, 0.07, 0.84 * fade)
        rounded_rect(cr, bx, by, bw, bh, 12)
        cr.set_source_rgba(*ink)
        cr.fill_preserve()
        cr.set_source_rgba(*FORCE, 0.35 * fade)
        cr.set_line_width(1)
        cr.stroke()
        # the tail: a small triangle from the bottom-right corner, its tip softly rounded
        cr.move_to(bx + bw - tail * 0.9, by + bh - 1)
        cr.line_to(*tip)
        cr.line_to(bx + bw - 1, by + bh - tail * 0.7)
        cr.close_path()
        cr.set_source_rgba(*ink)
        cr.fill_preserve()
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_width(2.5)
        cr.stroke()
        rounded_rect(cr, bx + 1, by + 1, bw - 2, bh - 2, 11)  # hide the tail's base inside the bubble
        cr.set_source_rgba(*ink)
        cr.fill()
        # The line types itself out while Yoda speaks.
        shown, _current = self._spoken(now)
        if shown < len(self.bubble):
            layout.set_text(self.bubble[:shown], -1)
        cr.move_to(bx + pad_x, by + pad_y)
        cr.set_source_rgba(0.93, 0.98, 0.92, 0.95 * fade)
        PangoCairo.show_layout(cr, layout)
        cr.restore()

    def bubble_anchor(self):
        """Where the speech bubble's bottom-right corner goes, in window pixels."""
        return self.cx + (0.3 + self.ear_tip[0]) * self.S, self.cy + (self.ear_tip[1] - 10) * self.S

    def _speech_times(self):
        """When each character of the current line is spoken, in characters' worth of time."""
        if getattr(self, "_speech", None) is None or self._speech[0] != self.bubble:
            starts, t = [], 0.0
            for c in self.bubble or "":
                starts.append(t)
                t += 1 + SPEECH_PAUSE.get(c, 0)
            self._speech = (self.bubble, starts, t)
        return self._speech

    def _spoken(self, now):
        """How many characters have been spoken, and which one is being said (or None in a pause)."""
        _text, starts, _total = self._speech_times()
        t = (now - self.bubble_text_at - BUBBLE_TYPE_DELAY) * BUBBLE_CHARS_PER_SEC
        if t < 0:
            return 0, None
        n = 0
        while n < len(starts) and starts[n] <= t:
            n += 1
        current = n - 1 if n and t - starts[n - 1] < 1 else None
        return n, current

    def _syllables(self):
        """(start, end, how open) for each syllable of the current line, in speech time.

        A syllable is a run of vowels with the consonants before it (and, at the end of a word,
        after it); the mouth opens and closes once for each, and rests between words."""
        text, starts, _total = self._speech_times()
        if getattr(self, "_syll", None) and self._syll[0] == text:
            return self._syll[1]
        out, i = [], 0
        while i < len(text):
            if not text[i].isalnum():
                i += 1
                continue
            j = i
            while j < len(text) and text[j].isalnum():
                j += 1
            word = text[i:j]
            cuts = [0]  # split the word before each vowel group after the first
            seen_vowel = False
            for k, c in enumerate(word):
                if c in VOWELS:
                    if seen_vowel and word[k - 1] not in VOWELS:
                        c0 = k - 1 if k - 1 > cuts[-1] and word[k - 1] not in VOWELS else k
                        cuts.append(c0)
                    seen_vowel = True
            cuts.append(len(word))
            for a, b in zip(cuts, cuts[1:]):
                part = word[a:b].lower()
                vowel = next((c for c in part if c in VOWELS), "")
                # how open, and the lips' shape: "oo" and "oh" purse them, "ee" stretches them
                kind = "round" if vowel in ("o", "u") else "wide" if vowel in ("e", "i", "y") else "open"
                amount = 1.0 if vowel in ("a", "o") else 0.8 if vowel else 0.45
                out.append((starts[i + a], starts[i + b - 1] + 1, amount, kind))
            i = j
        self._syll = (text, out)
        return out

    def _mouth_open(self, now):
        """How open his mouth is (0..1): once per syllable, shut between words and in the
        pauses, keeping time with the words as they type out."""
        if not self.bubble:
            return 0.0
        t = (now - self.bubble_text_at - BUBBLE_TYPE_DELAY) * BUBBLE_CHARS_PER_SEC
        for start, end, amount, kind in self._syllables():
            if start <= t < end:
                self._mouth_kind = kind
                return amount * math.sin(math.pi * (t - start) / (end - start)) ** 0.8
        return 0.0

    def _mouth(self, now):
        """The mouth, eased toward _mouth_open so it moves like a jaw rather than snapping."""
        target = self._mouth_open(now)
        last_t, last = getattr(self, "_mouth_state", (now, 0.0))
        k = 1 - math.exp(-max(0.0, now - last_t) * 14)
        value = last + (target - last) * k
        self._mouth_state = (now, value)
        return value

    def _typing_time(self):
        if not self.bubble:
            return BUBBLE_TYPE_DELAY
        return BUBBLE_TYPE_DELAY + self._speech_times()[2] / BUBBLE_CHARS_PER_SEC


# ---------- the app: Yoda's window, his two popups, and the commands that reach them

def pick_area(display, choice):
    """(work area, pixel density) of the configured monitor. YODA_DESK_AREA=x,y,w,h overrides
    the area, for trying other screen sizes."""
    monitor = None
    if isinstance(choice, int) and 0 <= choice < display.get_n_monitors():
        monitor = display.get_monitor(choice)
    monitor = monitor or display.get_primary_monitor() or display.get_monitor(0)
    override = os.environ.get("YODA_DESK_AREA")
    if override:
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = (int(v) for v in override.split(","))
        return rect, monitor.get_scale_factor() if monitor else 1
    if monitor is None:
        return None, 1
    return monitor.get_workarea(), monitor.get_scale_factor()


def yoda_scale(area, size=1.0):
    """Window pixels per Yoda unit: bigger on taller screens, never wider than the screen allows."""
    s = clamp(area.height / 1350, 1.0, 1.9) * size
    s = min(s, (area.width - 8) / WINDOW_W, (area.height * 0.6) / WINDOW_H)  # small or odd screens
    return max(0.5, s)


class App:
    def __init__(self):
        self.config = cfg = load_config()
        display = Gdk.Display.get_default()
        self.area, density = pick_area(display, cfg["monitor"])
        if self.area is None:  # no monitor right now (e.g. the lid is closed): try again soon
            print("yoda-desk: no monitor found, retrying in 10 s", file=sys.stderr)
            GLib.timeout_add_seconds(10, self._restart)
            return
        screen = Gdk.Screen.get_default()
        # The popups are dark; ask the theme for its dark variant so nothing in them is dark-on-dark.
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        if not screen.is_composited():
            print("yoda-desk: no compositor is running, so Yoda's window can't be see-through and "
                  "will show a dark box. Most desktops have one; on a bare window manager, run one "
                  "(e.g. picom).", file=sys.stderr)
        try:
            lib = Library(cfg["papers_dir"], cfg["weekly_paper_goal"], cfg["week_starts_on"])
        except OSError as e:
            sys.exit(f"yoda-desk: can't use papers folder {cfg['papers_dir']}: {e}")
        notes = Notes(cfg["notes_dir"])
        self.yoda = Yoda(self.area, self, lib, yoda_scale(self.area, cfg["size"]), density,
                         cfg["remind_minutes"], notes)
        self.reading = ReadingPopup(self, lib)
        self.note_box = box = NoteBox(self, notes, self._note_written, self._note_box_placed)
        box.win.connect("show", lambda *_: self.yoda.note_box_opened(True))
        box.win.connect("hide", lambda *_: self.yoda.note_box_opened(False))

        self.last = time.time()
        self.frame = 0
        self._relayout_pending = False
        GLib.timeout_add(1000 // FPS, self.tick)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self._on_request)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, self._restart)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._quit)
        screen.connect("monitors-changed", self._on_screen_changed)
        screen.connect("size-changed", self._on_screen_changed)

    # ---------- events

    def _on_request(self):
        """`yoda-desk list`, `note`, `notes` and `celebrate` write what they want to REQUEST_PATH, then
        send SIGUSR1."""
        try:
            with open(REQUEST_PATH) as f:
                what, when = (f.read().split() + ["", "0"])[:2]
            os.remove(REQUEST_PATH)
            fresh = time.time() - float(when) < 10
        except (OSError, ValueError):
            return True
        y = self.yoda
        if not fresh:
            pass
        elif what == "list":
            self.toggle_reading_list()
        elif what in ("note", "notes"):
            self.write_note(mode="write" if what == "note" else "read")
        elif what == "noted":  # a note written from the command line
            y.notes.recount()
            y.keep_note_arrived()
        elif what == "celebrate":
            y.say(y.pick(GOAL_MET), 8)
            y.celebrate("goal")
        elif what == "celebrate-rank":
            y.say(PROMOTED.get(y.rank, "Grown in the Force, you have."), 9)
            y.celebrate("rank")
        return True

    def _quit(self):
        Gtk.main_quit()
        return False

    def _on_screen_changed(self, *_):
        # Monitors often change in bursts (rotate, then resize); wait for it to settle.
        if not self._relayout_pending:
            self._relayout_pending = True
            GLib.timeout_add(2000, self._restart)

    def _restart(self):
        """Start over, with fresh screen geometry and config."""
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])

    def popup_menu(self, event):
        menu = Gtk.Menu()
        for label, cb in (("Reading list", lambda *_: self.toggle_reading_list()),
                          ("Write a note", lambda *_: self.write_note()),
                          ("Read notes", lambda *_: self.write_note(mode="read")),
                          ("Celebrate", lambda *_: self._celebrate()),
                          ("Quit Yoda", lambda *_: Gtk.main_quit())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", cb)
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        self._menu = menu  # keep a reference while it's open

    def _celebrate(self):
        self.yoda.say(self.yoda.pick(GOAL_MET), 8)
        self.yoda.celebrate("goal")

    # ---------- the popups

    def toggle_reading_list(self):
        if self.reading.visible:
            self.reading.hide()
            return
        a, y = self.area, self.yoda
        self.reading.show(a.x + a.width - 24, y.pos[1] + y.height, a.y, a.y + a.height, a.x)

    def write_note(self, timestamp=None, mode="write"):
        """Open the note box where Yoda's speech bubble goes, above and left of him."""
        box = self.note_box
        if box.visible:
            if box.mode == mode:  # a second click, or the shortcut again, closes it
                box.hide()
            else:
                box.switch(mode)
            return
        y = self.yoda
        y.bubble = None  # the box takes the bubble's place
        ax, ay = y.bubble_anchor()
        box.show(y.pos[0] + ax, y.pos[1] + ay, self.area, mode, timestamp)

    def note_typed(self):
        self.yoda.note_typed()

    def _to_yoda(self, rect):
        x, y, w, h = rect
        return (x - self.yoda.pos[0], y - self.yoda.pos[1], w, h)

    def _note_written(self, heading, text, rect):
        self.yoda.keep_note(text, self._to_yoda(rect), heading, from_box=True)

    def _note_box_placed(self, rect):
        self.yoda.present_droid(self._to_yoda(rect) if rect else None)

    def paper_read(self, title):
        self.yoda.paper_read(title)

    def reading_list_changed(self):
        self.yoda.refresh()
        if self.reading.visible:
            self.reading.refresh()

    # ---------- the clock

    def tick(self):
        now = time.time()
        dt = min(0.1, now - self.last)
        self.last = now
        self.frame += 1
        y = self.yoda
        if y.busy or self.frame % 2 == 0:  # at rest, 15 frames a second is plenty
            y.step(dt if y.busy else 2 * dt)
            y.win.queue_draw()
        return True


def main():
    if Gdk.Display.get_default() is None:
        # No desktop to draw on (e.g. started over SSH). Exit quietly so systemd doesn't loop;
        # the login autostart entry starts Yoda again with the next desktop session.
        print("yoda-desk: no display available", file=sys.stderr)
        sys.exit(0)
    App()
    Gtk.main()


if __name__ == "__main__":
    main()
