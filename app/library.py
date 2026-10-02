"""Yoda Desk's data: settings, the reading list and its Jedi ranks, notes, and Yoda's lines.

Nothing here touches the screen; yoda.py and popups.py draw it all. Also run from the command
line by `yoda-desk note`:  python3 library.py add-note [-t heading] <text>
"""
import datetime as dt
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "yoda-desk")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "yoda-desk")
LOG_PATH = os.path.join(STATE_DIR, "reading-log.json")
TITLES_PATH = os.path.join(STATE_DIR, "arxiv-titles.json")
STATE_PATH = os.path.join(STATE_DIR, "reading-state.json")

# ---------- settings

DEFAULTS = {
    "monitor": "primary",  # or a monitor number: 0, 1, ...
    "size": 1.0,  # Yoda's size, on top of what suits the screen (0.5 to 2.5)
    "papers_dir": "~/Papers",
    "notes_dir": "~/Papers/Notes",
    "weekly_paper_goal": 3,
    "week_starts_on": "monday",
    "remind_minutes": 45,  # how often he reminds you about your reading; 0 for never
}


def _coerce(key, value):
    """value if it suits the setting, else the default. Numbers may be given as strings."""
    default = DEFAULTS[key]
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number < 0 or number != number:  # negative or NaN
            return default
        return int(number) if isinstance(default, int) else number
    if key == "monitor":  # "primary" or a monitor number
        if isinstance(value, bool):
            return default
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            return int(value)
        return default
    return value if isinstance(value, str) and value.strip() else default


def load_config():
    config = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as e:
        print(f"yoda-desk: ignoring unreadable config {CONFIG_PATH}: {e}", file=sys.stderr)
        return config
    if not isinstance(user, dict):
        print(f"yoda-desk: ignoring config {CONFIG_PATH}: expected a JSON object", file=sys.stderr)
        return config
    for key, value in user.items():
        if key in DEFAULTS:
            config[key] = _coerce(key, value)
    if config["weekly_paper_goal"] < 1:
        config["weekly_paper_goal"] = DEFAULTS["weekly_paper_goal"]
    config["size"] = max(0.5, min(2.5, config["size"] or 1.0))
    return config


def save_config_value(key, value):
    """Set one setting in the config file, keeping everything else in it."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        return False  # don't overwrite a file we can't read
    if not isinstance(data, dict):
        return False  # nor one that holds something other than settings
    data[key] = value
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)
    return True


# ---------- the reading list
#
# Papers wait in a folder, and move aside as they're read, into a weekly log. Files named
# after an arXiv ID (e.g. 1706.03762v5.pdf) show their real title, looked up once from the
# arXiv API and cached. Weeks in a row that meet the reading goal earn a Jedi rank.

TODO_DIR, DONE_DIR = "To Read", "Read"

# New-style (2303.04137, 1706.03762v5) and old-style (hep-th/9901001, written hep-th_9901001) IDs.
ARXIV_NEW = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?!\d)")
ARXIV_OLD = re.compile(r"\b([a-z-]+(?:\.[A-Z]{2})?)[_/](\d{7})(?:v\d+)?\b")
ARXIV_API = "https://export.arxiv.org/api/query?id_list={ids}&max_results={n}"

# Jedi ranks. Each week that meets the goal counts toward the next rank; a missed
# week drops you one rank. At Jedi Master, weeks meeting the goal build a master streak.
# Every 12 weeks in a row meeting the goal earns a lifeline, which is spent
# automatically on a missed week so you keep your rank and progress.
RANKS = ["Youngling", "Padawan", "Jedi Knight", "Jedi Master"]
PROMOTE_AFTER = [2, 4, 6]  # weeks meeting the goal at each rank to reach the next
LIFELINE_EVERY = 12


class Rank:
    def __init__(self):
        self.level = 0
        self.progress = 0  # weeks banked toward the next rank
        self.master_streak = 0
        self.lifelines = 0
        self.run = 0  # weeks in a row meeting the goal, toward the next lifeline
        self.earned = 0  # lifelines earned and spent, ever
        self.spent = 0

    @property
    def weeks_to_lifeline(self):
        return LIFELINE_EVERY - self.run

    @property
    def name(self):
        return RANKS[self.level]

    @property
    def next(self):
        return RANKS[self.level + 1] if self.level + 1 < len(RANKS) else None

    @property
    def weeks_left(self):
        return PROMOTE_AFTER[self.level] - self.progress if self.next else 0

    @property
    def fraction(self):
        """Progress toward the next rank, 0..1 (1 at Master)."""
        return self.progress / PROMOTE_AFTER[self.level] if self.next else 1.0

    def week(self, met):
        """Apply one week's result."""
        if met:
            self.run += 1
            if self.run >= LIFELINE_EVERY:
                self.lifelines += 1
                self.earned += 1
                self.run = 0
            if self.next is None:
                self.master_streak += 1
                return
            self.progress += 1
            if self.progress >= PROMOTE_AFTER[self.level]:
                self.level += 1
                self.progress = 0
            return
        self.run = 0
        if self.lifelines:
            # A lifeline saves the week: rank, progress and master streak stay as they are.
            self.lifelines -= 1
            self.spent += 1
            return
        self.level = max(0, self.level - 1)
        self.progress = 0
        self.master_streak = 0


def arxiv_id(name):
    stem = os.path.splitext(name)[0]
    m = ARXIV_NEW.search(stem)
    if m:
        return m.group(1)
    m = ARXIV_OLD.search(stem)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def title_from_filename(name):
    stem = os.path.splitext(name)[0]
    if " " not in stem:
        stem = stem.replace("_", " ").replace("-", " ")
    return " ".join(stem.split()) or name


def unique_path(folder, name):
    """A path in folder for name that doesn't overwrite anything ("x (2).pdf")."""
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem} ({n}){ext}")
        n += 1
    return path


DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEK_START_DAY = 0  # Monday; set from the "week_starts_on" setting


def week_start(t=None):
    """Local midnight on the first day of the reading week containing t."""
    day = dt.date.fromtimestamp(time.time() if t is None else t)
    first = day - dt.timedelta(days=(day.weekday() - WEEK_START_DAY) % 7)
    return time.mktime(first.timetuple())


class Paper:
    def __init__(self, path, titles=None):
        self.path = path
        self.name = os.path.basename(path)
        self.arxiv = arxiv_id(self.name)
        known = (titles or {}).get(self.arxiv) if self.arxiv else None
        self.title = known or title_from_filename(self.name)
        self.added = os.path.getmtime(path)


class Library:
    def __init__(self, root, goal, week_starts_on="monday"):
        global WEEK_START_DAY
        day = str(week_starts_on).strip().lower()
        WEEK_START_DAY = DAYS.index(day) if day in DAYS else 0
        self.root = os.path.expanduser(root)
        self.todo = os.path.join(self.root, TODO_DIR)
        self.done = os.path.join(self.root, DONE_DIR)
        try:
            goal = max(1, int(goal))
        except (TypeError, ValueError):
            goal = 3
        os.makedirs(self.todo, exist_ok=True)
        os.makedirs(self.done, exist_ok=True)
        self.log = self._load()
        self.titles = _read_json(TITLES_PATH, {})
        self.state = _read_json(STATE_PATH, {})
        if not isinstance(self.state, dict):
            self.state = {}
        self.goal = goal
        self._record_goal(goal)
        self._tried = {}  # arXiv ID -> when it was last looked up without an answer
        self._lookup = None

    # ---------- log

    def _load(self):
        data = _read_json(LOG_PATH, [])
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("read_at"), (int, float))]

    def _save(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LOG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.log, f, indent=1)
        os.replace(tmp, LOG_PATH)

    # ---------- papers

    def unread(self):
        """Papers waiting to be read, oldest first."""
        try:
            names = [n for n in os.listdir(self.todo) if not n.startswith(".")]
        except OSError:
            return []
        papers = []
        for name in names:
            path = os.path.join(self.todo, name)
            if os.path.isfile(path):
                papers.append(Paper(path, self.titles))
        return sorted(papers, key=lambda p: p.added)

    def add(self, src):
        """Copy a file into the to-read folder."""
        dest = unique_path(self.todo, os.path.basename(src))
        shutil.copy2(src, dest)
        os.utime(dest)  # "added" is now, not when the file was made
        return Paper(dest, self.titles)

    def mark_read(self, paper):
        """Move a paper to the read folder and log it. Returns an undo token."""
        dest = unique_path(self.done, paper.name)
        shutil.move(paper.path, dest)
        entry = {"title": paper.title, "file": os.path.basename(dest), "read_at": time.time()}
        self.log.append(entry)
        try:
            self._save()
        except OSError as e:  # the file has moved; keep the entry for this session at least
            print(f"yoda-desk: couldn't save reading log: {e}", file=sys.stderr)
        return {"entry": entry, "from": paper.path, "to": dest}

    def undo(self, token):
        if os.path.exists(token["to"]):
            back = token["from"]
            if os.path.exists(back):
                back = unique_path(self.todo, os.path.basename(back))
            shutil.move(token["to"], back)
        if token["entry"] in self.log:
            self.log.remove(token["entry"])
            self._save()

    # ---------- arXiv titles

    def lookup_titles(self, papers, done):
        """Fetch real titles for arXiv-named papers in the background; calls done() if any arrive."""
        now = time.time()
        ids = sorted({p.arxiv for p in papers if p.arxiv and p.arxiv not in self.titles
                      and now - self._tried.get(p.arxiv, 0) > 600})
        if not ids or (self._lookup and self._lookup.is_alive()):
            return
        self._tried.update({i: now for i in ids})

        def query(batch):
            """{id: title} for a batch, or None if arXiv rejected an ID in it."""
            url = ARXIV_API.format(ids=",".join(batch), n=len(batch))
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "yoda-desk"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    root = ET.fromstring(resp.read())
            except Exception:
                return {}
            ns = {"a": "http://www.w3.org/2005/Atom"}
            out = {}
            for entry in root.findall("a:entry", ns):
                eid, title = entry.find("a:id", ns), entry.find("a:title", ns)
                if eid is None or not eid.text or "/api/errors" in eid.text:
                    return None
                if title is not None and title.text:
                    out[re.sub(r"v\d+$", "", eid.text.split("/abs/")[-1])] = " ".join(title.text.split())
            return out

        def fetch():
            found = {}
            for i in range(0, len(ids), 40):  # arXiv asks for modest batches, 3 s apart
                batch = ids[i:i + 40]
                result = query(batch)
                time.sleep(3)
                if result is None:  # one bad ID spoils the batch: ask one at a time
                    for one in batch:
                        found.update(query([one]) or {})
                        time.sleep(3)
                else:
                    found.update(result)
            if found:
                self.titles.update(found)
                _write_json(TITLES_PATH, self.titles)
                done()

        self._lookup = threading.Thread(target=fetch, daemon=True)
        self._lookup.start()

    # ---------- rank

    def rank(self):
        """Replay every week since the first paper was logged (this week counts once it's met)."""
        rank = Rank()
        times = [e.get("read_at", 0) for e in self.log if e.get("read_at")]
        if not times:
            return rank
        weeks = int(round((week_start() - week_start(min(times))) / (7 * 86400))) + 1
        history = self.weekly_windows(weeks)
        for ts, count in history[:-1]:
            rank.week(count >= self.goal_for(ts))
        ts, count = history[-1]
        if count >= self.goal_for(ts):
            rank.week(True)
        return rank

    def rank_events(self, silent=False):
        """What changed since last checked: any of 'up', 'down', 'lifeline_earned', 'lifeline_spent'.

        Only rises are announced across an undo: undoing a read is a correction, not a fall."""
        rank = self.rank()
        now = {"rank": rank.name, "earned": rank.earned, "spent": rank.spent}
        last = {k: self.state.get(k) for k in now}
        if last == now:
            return []
        self.state.update(now)
        try:
            _write_json(STATE_PATH, self.state)
        except OSError:
            pass
        if silent or last["rank"] not in RANKS:
            return []  # first run, or a correction such as an undo: nothing to announce
        events = []
        if RANKS.index(rank.name) > RANKS.index(last["rank"]):
            events.append("up")
        elif RANKS.index(rank.name) < RANKS.index(last["rank"]):
            events.append("down")
        if rank.earned > (last["earned"] or 0):
            events.append("lifeline_earned")
        if rank.spent > (last["spent"] or 0):
            events.append("lifeline_spent")
        return events

    # ---------- stats

    def read_this_week(self):
        start = week_start()
        return sum(1 for e in self.log if e.get("read_at", 0) >= start)

    def expected_by_now(self):
        """How many the goal implies by today, spread evenly over the week."""
        day_of_week = (dt.date.today().weekday() - WEEK_START_DAY) % 7  # 0 on the first day
        return self.goal * (day_of_week + 1) // 7

    # ---------- goals over time

    def _record_goal(self, goal):
        """Remember the goal in effect from this week on, so changing it never rewrites past weeks."""
        history = self.state.get("goals")
        if not isinstance(history, list) or not history:
            self.state["goals"] = [[0, goal]]  # the first goal applies to all earlier weeks
        elif history[-1][1] != goal:
            this_week = week_start()
            history = [h for h in history if h[0] < this_week]
            history.append([this_week, goal])
            self.state["goals"] = history
        else:
            return
        try:
            _write_json(STATE_PATH, self.state)
        except OSError:
            pass

    def set_goal(self, goal):
        self.goal = max(1, int(goal))
        self._record_goal(self.goal)

    def goal_for(self, week_ts):
        goal = self.goal
        for since, g in self.state.get("goals", []):
            if since <= week_ts + 3600:
                goal = g
        return goal

    # ---------- history

    def weekly_windows(self, weeks):
        """(week start, papers read) per week, oldest first, ending with this week."""
        start = week_start()
        out = []
        for i in range(weeks - 1, -1, -1):
            lo = week_start(start - i * 7 * 86400 + 3600)
            hi = week_start(lo + 8 * 86400)
            out.append((lo, sum(1 for e in self.log if lo <= e["read_at"] < hi)))
        return out

    def weekly_history(self, weeks=8):
        """Papers read per week, oldest first, ending with this week."""
        return [count for _, count in self.weekly_windows(weeks)]

    def streak(self):
        """Consecutive weeks meeting their goal (this week counts once it's met)."""
        weeks = self.weekly_windows(52)
        if weeks[-1][1] < self.goal_for(weeks[-1][0]):
            weeks = weeks[:-1]
        n = 0
        for ts, count in reversed(weeks):
            if count < self.goal_for(ts):
                break
            n += 1
        return n


# ---------- notes
#
# One Markdown file per note, named after its heading (or its first few words):
#
#     <notes_dir>/Multimodal grasps.md
#
#     ---
#     created: 2026-09-30 14:32
#     ---
#     # Multimodal grasps
#
#     Diffusion policies struggle with them. Try action chunking.
#
# Any other .md files in the folder show up in the list too.

CREATED = re.compile(r"^created:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", re.M)
UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')  # not allowed in file names on some systems
NAME_WORDS, NAME_CHARS = 6, 80


class Note:
    def __init__(self, when, heading, body, path):
        self.when, self.heading, self.body, self.path = when, heading, body, path
        self.day, self.time = when.date(), f"{when:%H:%M}"


def file_name(heading, body, when):
    """A file name from the heading, or the first words of the note."""
    name = heading or " ".join(body.lstrip("#> \t\n").split()[:NAME_WORDS])  # not a Markdown mark
    name = " ".join(UNSAFE.sub(" ", name).split()).strip(". ")[:NAME_CHARS].rstrip(". ")
    return name or f"Note {when:%Y-%m-%d %H.%M}"


def read_note(path):
    with open(path) as f:
        text = f.read()
    when = None
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            m = CREATED.search(text[4:end])
            if m:
                try:
                    when = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
                except ValueError:
                    pass
            text = text[end + 4:].lstrip("\n")
    if when is None:
        when = dt.datetime.fromtimestamp(os.path.getmtime(path))
    heading = ""
    if text.startswith("# "):
        first, _, text = text.partition("\n")
        heading = first[2:].strip()
    return Note(when, heading, text.strip(), path)


class Notes:
    def __init__(self, folder):
        self.folder = os.path.expanduser(folder or DEFAULTS["notes_dir"])
        self.count = 0
        self.recount()

    def add(self, text, heading="", when=None):
        """Save a note in a file of its own; returns its path, or None if there was nothing to keep."""
        heading = " ".join(heading.split())
        body = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
        if not body and not heading:
            return None
        when = when or dt.datetime.now()
        os.makedirs(self.folder, exist_ok=True)
        path = unique_path(self.folder, file_name(heading, body, when) + ".md")
        with open(path, "x") as f:
            f.write(f"---\ncreated: {when:%Y-%m-%d %H:%M}\n---\n")
            if heading:
                f.write(f"# {heading}\n\n")
            if body:
                f.write(f"{body}\n")
        self.count += 1
        return path

    def all(self):
        """Every note, newest first."""
        notes = []
        try:
            names = [n for n in os.listdir(self.folder) if n.endswith(".md") and not n.startswith(".")]
        except OSError:
            names = []
        for name in names:
            try:
                notes.append(read_note(os.path.join(self.folder, name)))
            except (OSError, UnicodeDecodeError):
                pass
        notes.sort(key=lambda n: n.when, reverse=True)
        return notes

    def recount(self):
        self.count = len(self.all())
        return self.count


# ---------- what Yoda says
#
# Everything Yoda says. Each thought on its own line; numbers always say what they count.

# Placeholders, filled in by Yoda.say():
#   {count} {goal} {left}   papers read this week, the weekly goal, and how many still to go
#   {read_p} {left_p}       the same as words: "1 paper", "3 more papers"
#   {title}                 a paper's title (the oldest waiting one, unless another is meant)
#   {days}                  how long that oldest paper has waited: "5 days"
#   {days_left}             what's left of the reading week: "2 days"
#   {waiting}               how many papers wait to be read: "12 papers"
#   {rank} {master}         his rank for you, and a Master's streak in weeks
#   {folder}                the to-read folder

BEHIND = [
    "Hmm. {count} of {goal} papers this week.\nRead more, you must.",
    "Behind on your reading, you are.\n{left_p} this week, hmm?",
    "Waiting, your papers are.\n'{title}', begin with.",
    "Knowledge, a Jedi's true power is.\nRead '{title}', you should.",
    "Do, or do not. There is no 'later'.\nRead '{title}'.",
    "Slipping, your week is.\n{left_p}, catch up you must.",
    "Hmm. Busy, you have been.\nBut read, a Jedi still does.",
    "A paper a day, the backlog keeps away.\n'{title}', start now.",
    "Train your model, you do.\nTrain your mind, you must also.",
    "Behind, you are. Hopeless, you are not.\n{left_p} this week, still possible.",
]
ON_TRACK = [
    "On track, you are. Good.\n'{title}', next read.",
    "{count} of {goal} papers this week.\nKeep reading, you will.",
    "Patience you have.\nBut '{title}', still waiting it is.",
    "Good pace, this is.\nSteady, like a well-tuned learning rate.",
    "Hmm. On schedule, your reading is.\nContinue, you should.",
    "{read_p} this week, read you have.\nThe Force flows well.",
    "Keeping up, you are.\n{left_p}, and done the week is.",
]
GOAL_MET = [
    "{read_p} this week, read you have.\nProud, I am.",
    "A true {rank}, you are.\nYour goal, met it is.",
    "Strong in the Force, your reading is.\nHmm.",
    "Your goal, met it is.\nMore, a true Jedi reads.",
    "Done, your week's reading is.\nRest your eyes, or read on. Your choice.",
    "Hmm! {count} of {goal} papers.\nConverged, your week has.",
    "Well read, you are this week.\nUse it wisely, you will.",
]
MASTER_MET = [
    "{master} weeks a Master, you have been.\nThe Force, strong it is.",
    "A Master's streak, {master} weeks long it grows.\nHmm.",
    "Week after week, a Master you remain.\n{master} weeks, now.",
]
EMPTY = [
    "Empty, your reading list is.\nAdd papers to {folder}, you must.",
    "Nothing to read, there is.\nFind new papers, you should.",
    "A clear list. Rare, this is.\nFill it again, you will. Hmm.",
]
DONE = [
    "Done, '{title}' is.\n{count} of {goal} papers this week.",
    "Read it, you have.\nWiser, you become.",
    "Good, good.\n{count} of {goal} papers this week, hmm.",
    "Finished, '{title}' is.\nWhat learned you? Write it down.",
    "Another paper, into the mind it goes.\n{left_p} this week.",
    "Hmm. Read, you have.\nImplement it, perhaps you will.",
]
ADDED = [
    "Added to your list, '{title}' is.",
    "'{title}'.\nRead it soon, you will.",
    "Hmm. '{title}'.\nPromising, this one looks.",
    "Into the archive, '{title}' goes.\n{waiting} wait now.",
]
OPENED = [
    "'{title}'.\nGood choice, this is.",
    "Read '{title}', you will.\nHmm, good.",
    "Begin, you have.\nFinish, you must.",
    "The abstract, only the beginning it is.\nRead it all, you should.",
    "Hmm. '{title}'.\nThe figures first, many do. Wise, that is.",
]
NOTED = [
    "Written in the Force, it is.",
    "Safe with the droid, your thought is.\nForget it, you will not.",
    "A wise thought, that is. Hmm.",
    "Kept, your thought is.\nRemember it, the droid will.",
    "Hmm. Noted.\nReturn to it, you will.",
    "A thought written is a thought kept.\nGood.",
]

# Now and then, in place of the week's news
LONG_WAIT = [
    "{days}, '{title}' has waited.\nPatient it is. Patient you should not be.",
    "'{title}'.\nWaiting {days}, this paper has. Hmm.",
    "Old, '{title}' grows.\n{days} on your list. Read it, or let it go.",
]
LAST_DAYS = [
    "{days_left} left in your week.\n{left_p} to go. Hurry, you must.",
    "Hmm. The week, nearly over it is.\n{left_p}, still.",
]
BACKLOG = [
    "{waiting} wait for you.\nA library, your list becomes.",
    "Many papers, you have saved.\nRead them, fewer you have. Hmm.",
    "{waiting} on your list.\nThe oldest, '{title}' is.",
]
MORNING = [
    "Morning, it is.\nA paper with your coffee, hmm?",
    "Fresh, the mind is in the morning.\n'{title}', start with.",
]
LATE = [
    "Late it is.\nRest, you must.",
    "Hmm. Past midnight, it is.\nTomorrow, read you will.",
    "Tired eyes, badly they read.\nSleep. The papers, wait they will.",
]
WEEKEND = [
    "The weekend, it is.\nRead for joy, today you may.",
    "Rest, the weekend is for.\nOne short paper, perhaps. Hmm.",
]
WISDOM = [
    "Overfit, your mind can too.\nRead widely, you must.",
    "Much to learn, you still have.\nGood. Always, this is so.",
    "Results you seek.\nBut the baseline, remember it you must.",
    "Fail, experiments will.\nLearn from them, the Jedi does.",
    "The loss, it goes down.\nBut generalise, does it? Hmm.",
    "Size matters not.\nA small model, well trained, strong it is.",
    "Try not. Ablate.\nOnly then, know you will.",
    "Clear your mind.\nThe bug, then you will see.",
    "In a dark place, the gradients vanish.\nNormalise, you must.",
    "Patience. Converge, all things do.\nIn time.",
    "Read the related work, you should.\nNew, few ideas are.",
    "Luminous beings are we.\nNot crude matrices of floats. Hmm.",
]

PROMOTED = {
    "Padawan": "Two weeks, your goal you have met.\nA Padawan, you now are.",
    "Jedi Knight": "Six weeks strong.\nA Jedi Knight, you have become. Hmm.",
    "Jedi Master": "Twelve weeks!\nA Jedi Master, you are.\nTaught you well, I have.",
}
STREAK_LOST = "A week missed, there was.\nBack to {rank}, you fall.\nRise again, you will."
LIFELINE_EARNED = "Twelve weeks without fail!\nA lifeline, you have earned.\nA missed week, it will forgive."
LIFELINE_SPENT = "Missed a week, you did.\nYour lifeline, spent it is.\nSafe, your rank remains. Hmm."


# ---------- the command line

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] != "add-note":
        sys.exit("usage: library.py add-note [-t heading] <text>   (text '-' reads it from stdin)")
    args, heading = args[1:], ""
    if args[:1] in (["-t"], ["--title"]) and len(args) > 1:
        heading, args = args[1], args[2:]
    text = sys.stdin.read() if args == ["-"] else " ".join(args)
    path = Notes(load_config()["notes_dir"]).add(text, heading)
    if not path:
        sys.exit("Nothing to write, there is.")
    print(f"Noted: {path}")
