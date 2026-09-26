#!/usr/bin/env python3
"""WhatsApp chat export -> SQLite, with local voice-note transcription and image OCR.

Subcommands, in order:
  ingest      parse an export (.zip, folder or chat .txt) into a new DB, reusing transcripts/OCR from the old one
  transcribe  transcribe voice notes locally (mlx-whisper or faster-whisper)
  ocr         OCR images with tesseract
  index       copy transcripts onto messages and rebuild the FTS5 indexes
  status      progress counts, failures, and the state of the last run

Every command can be re-run. ingest builds the new DB in a temp file and swaps it in only when it
is complete, and refuses to drop messages the current DB has unless told to. transcribe and ocr
commit per item and skip what is done; failures are recorded per item and retried with
--retry-errors. A lock file stops two runs touching the same DB at once.
"""
import argparse, datetime, fcntl, faulthandler, glob, os, re, shutil, signal, sqlite3, subprocess, sys, tempfile, textwrap, time, traceback, zipfile
from collections import Counter
from pathlib import Path

AUDIO = {"opus", "m4a", "mp3", "wav", "ogg", "aac", "amr"}
IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "heic"}
VIDEO = {"mp4", "mov", "3gp", "mkv"}

# Zero-width and bidi marks (WhatsApp puts U+200E before the timestamp and before "<attached:"),
# and the non-breaking spaces newer exports put before AM/PM.
INVISIBLE = dict.fromkeys(map(ord, "​‎‏‪‫‬‭‮⁠﻿"), None)
SPACES = {0x202F: " ", 0x00A0: " "}

TS = r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?(?: ?([AaPp])\.? ?[Mm]\.?)?"
IOS = re.compile(r"^\[" + TS + r"\] (.*)$")          # [21/04/2025, 11:40:03] Name: text
ANDROID = re.compile(r"^" + TS + r" - (.*)$")         # 21/04/2025, 11:40 - Name: text
NEAR_HEADER = re.compile(r"^\[?\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}")
SENDER = re.compile(r"^([^:\n]+?):(?: (.*)|$)", re.S)  # "Name: text", or "Name:" with an empty body

# Attachment markers are worded in the phone's UI language ("<attached: X>", "<adjunto: X>",
# "X (file attached)", "X (archivo adjunto)"), so match their shape instead of the words, and
# accept a match only if it is clearly an attachment: the English word, a file that exists in
# the export, or (iOS) WhatsApp's numbered file name.
IOS_ATTACHED = re.compile(r"<([^<>:\n]+): ([^<>\n]+\.\w+)>")
IOS_MEDIA_NAME = re.compile(r"^\d{8}-")
ANDROID_ATTACHED = re.compile(r"^(.+\.\w+) \(([^()\n]+)\)$", re.M)
STICKER = re.compile(r"(-STICKER-|^STK-)|\.was$", re.I)   # .was = animated sticker

# File signatures, exactly as leptonica (tesseract's image library) checks them, for the formats
# WhatsApp sends; HEIC is converted with sips first. Anything leptonica doesn't recognise sends
# tesseract into its file-list mode, so a looser prefix here would be a hole (see image_type).
MAGIC = ((b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))
HEIF_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1")
# How WhatsApp names media: iOS 00000021-AUDIO-2025-06-20-19-20-28.opus, Android PTT-20250113-WA0001.opus.
WA_MEDIA_NAME = re.compile(r"^(0\d{7}-|(IMG|VID|AUD|PTT|STK|DOC)-\d{8}-WA\d+)")

# These are English-only; other UI languages leave such messages as kind 'text'.
OMITTED = re.compile(r"^<?(image|audio|video|sticker|document|gif|media) omitted>?$", re.I)
CALL = re.compile(r"^(missed |silenced )?(group )?(voice|video) call(\.|$)", re.I)
DELETED = re.compile(r"^(this message was deleted|you deleted this message)\.?$", re.I)
EDITED = "<This message was edited>"
E2E = "Messages and calls are end-to-end encrypted"

MAX_STREAK = 10  # this many new items failing in a row means the setup is broken, or the files are

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE messages(
  id INTEGER PRIMARY KEY, ts TEXT, date TEXT, time TEXT, year_month TEXT,
  sender TEXT, kind TEXT, text TEXT, media_file TEXT, char_len INT, edited INT DEFAULT 0,
  transcript TEXT, translation TEXT, ocr_text TEXT, ocr_error TEXT, image_desc TEXT);
CREATE TABLE audios(
  id INTEGER PRIMARY KEY, message_id INT, file TEXT, date TEXT, time TEXT, sender TEXT,
  transcript TEXT, translation TEXT, word_count INT, error TEXT, translation_error TEXT);
CREATE TABLE media(
  id INTEGER PRIMARY KEY, message_id INT, file TEXT, date TEXT, time TEXT, sender TEXT,
  type TEXT, path TEXT);
CREATE INDEX i_m_date ON messages(date);
CREATE INDEX i_m_send ON messages(sender);
CREATE INDEX i_m_kind ON messages(kind);
CREATE INDEX i_a_date ON audios(date);
CREATE VIRTUAL TABLE messages_fts USING fts5(text, content='messages', content_rowid='id');
CREATE VIRTUAL TABLE audios_fts USING fts5(transcript, content='audios', content_rowid='id');
CREATE VIRTUAL TABLE all_fts USING fts5(body, content='');
"""


def log(*a):
    print(*a, flush=True)


def stamp():
    return time.strftime("%F %T")


def die(msg):
    sys.exit(f"error: {msg}")


def connect(db):
    return sqlite3.connect(db, timeout=60)


def connect_ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def unique(path):
    """`path`, or `path-2`, `path-3`, ... whichever doesn't exist yet."""
    cand, n = path, 1
    while os.path.exists(cand):
        n += 1
        cand = f"{path}-{n}"
    return cand


def make_dir(path):
    """Create `path`, or `path-2`, `path-3`, ... atomically, and return the one created: two runs
    in the same second must never share (and then clean up) one directory."""
    cand, n = path, 1
    while True:
        try:
            os.mkdir(cand)
            return cand
        except FileExistsError:
            n += 1
            cand = f"{path}-{n}"


# ---------- locking ----------

def lock(db, what):
    """Take <db>.lock for the life of this process (and of a detached child), or die."""
    fd = os.open(db + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pid, other = holder(os.pread(fd, 100, 0))
        die(f"another wa2db run ({other}, pid {pid}) is using {db}; wait for it to finish or stop it first")
    mark_lock(fd, what)
    return fd


def mark_lock(fd, what):
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()} {what}".encode(), 0)


def holder(raw):
    """(pid, command) from a lock file's content."""
    pid, _, what = raw.decode(errors="replace").strip().partition(" ")
    return pid or "?", what or "a run"


def running(db):
    """(pid, command) of the run holding <db>.lock, or None."""
    if not os.path.exists(db + ".lock"):
        return None
    fd = os.open(db + ".lock", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return None
    except BlockingIOError:
        return holder(os.pread(fd, 100, 0))
    finally:
        os.close(fd)


# ---------- finding and parsing the export ----------

def walk_export(export_dir):
    """(name, path) for every regular file in the export, skipping symlinks (they could point
    anywhere on this machine) and the AppleDouble junk (__MACOSX/, ._*) that re-zipping in Finder
    adds."""
    for root, dirs, files in os.walk(export_dir):
        dirs[:] = [d for d in dirs if d != "__MACOSX"]
        for f in files:
            p = os.path.join(root, f)
            if not f.startswith("._") and not os.path.islink(p):
                yield f, p


def export_files(export_dir):
    """{name: path} for the export's files; the first path wins if a name repeats."""
    files = {}
    for f, p in walk_export(export_dir):
        files.setdefault(f, p)
    return files


def unpack(src, work):
    """(export dir, dir this call created or None). A zip is extracted into a new work/export-<stamp>."""
    if os.path.isdir(src):
        return os.path.realpath(src), None
    if not zipfile.is_zipfile(src):
        die(f"{src} is not a readable zip ({os.path.getsize(src)} bytes). A 0-byte or truncated zip "
            "usually means the WhatsApp export is still transferring; wait and retry.")
    s1 = os.path.getsize(src); time.sleep(2); s2 = os.path.getsize(src)
    if s1 != s2:
        die(f"{src} is still growing ({s1} -> {s2} bytes); wait for the transfer to finish")
    with zipfile.ZipFile(src) as z:
        bad = z.testzip()
        if bad:
            die(f"corrupt member in zip: {bad}")
        # A fresh directory each time: the current DB's media paths keep pointing at the old one.
        dest = make_dir(os.path.join(work, "export-" + time.strftime("%Y%m%d-%H%M%S")))
        try:
            z.extractall(dest)
        except BaseException:
            remove_extraction(dest)
            raise
    return os.path.realpath(dest), dest


def remove_extraction(path):
    """Remove an extraction this run created (the zip itself is untouched)."""
    try:
        shutil.rmtree(path)
    except OSError as e:
        log(f"warning: couldn't remove this run's extraction {path}: {e}")


def looks_like_chat(path):
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        head = [fh.readline().translate(INVISIBLE).translate(SPACES) for _ in range(5)]
    return any(IOS.match(l) or ANDROID.match(l) for l in head)


def find_chat_txt(export_dir):
    """The chat log's name depends on platform and UI language, so pick the .txt that parses.
    A chat export forwarded inside this chat is another such .txt; it's an attachment of the real
    log, so it is referenced by name from it."""
    root = os.path.realpath(export_dir)
    cands = [p for f, p in walk_export(export_dir) if f.lower().endswith(".txt") and looks_like_chat(p)]
    exact = [p for p in cands if os.path.basename(p) == "_chat.txt" and os.path.dirname(os.path.realpath(p)) == root]
    if len(exact) == 1:
        return exact[0]
    cands = [p for p in cands if os.path.dirname(os.path.realpath(p)) == root] or cands
    if len(cands) > 1:
        texts = {p: Path(p).read_text(encoding="utf-8-sig", errors="replace") for p in cands}
        attached = {q for p in cands for q in cands if q != p and os.path.basename(q) in texts[p]}
        cands = [p for p in cands if p not in attached] or cands
    if len(cands) != 1:
        names = [os.path.relpath(p, export_dir) for p in cands]
        die(f"expected one WhatsApp chat log (.txt) in {export_dir}, found: {names or 'none'}. "
            "Pass one of these names with --chat.")
    return cands[0]


def iso(groups, order):
    """(date, time) from header regex groups; ValueError if that date/time can't exist."""
    a, b, y, hh, mm, ss, ampm = groups
    d, mo = (a, b) if order == "dmy" else (b, a)
    h = int(hh)
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "p" else 0)
    dt = datetime.datetime(int(y) + (2000 if len(y) == 2 else 0), int(mo), int(d), h, int(mm), int(ss or 0))
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")


def open_quote(s):
    """True if `s` ends inside a quotation: its colon belongs to quoted text, not to a sender."""
    return s.count('"') % 2 == 1 or s.count("\u201c") > s.count("\u201d")


def parse(path, date_order="auto", fallback=None):
    """Return ([(date, time, sender, text)], diagnostics). A line that doesn't start with a
    timestamp header continues the previous message, and so does a "header" whose date can't
    exist (e.g. a pasted US-format date in a day-first chat). `fallback` = (order, how it was
    chosen) of the current DB, used when this export's day/month order is ambiguous."""
    try:
        with open(path, encoding="utf-8-sig") as fh:  # strict: mojibake must fail loudly
            lines = fh.read().split("\n")
    except UnicodeDecodeError as e:
        die(f"{path} is not UTF-8 ({e}); WhatsApp writes UTF-8 — was it re-saved by another app?")
    recs, near, dropped = [], [], 0
    for n, line in enumerate(lines, 1):
        line = line.rstrip("\r").translate(INVISIBLE).translate(SPACES)
        m = IOS.match(line) or ANDROID.match(line)
        if m:
            recs.append({"groups": m.groups()[:7], "lines": [m.group(8)], "n": n, "raw": line,
                         "android": m.re is ANDROID})
        elif recs:
            recs[-1]["lines"].append(line)
            if NEAR_HEADER.match(line):
                near.append((n, line))
        elif line.strip():
            dropped += 1
    if not recs:
        die(f"no message headers recognised in {path}; unknown export format")

    day_first = sum(int(r["groups"][0]) > 12 for r in recs)
    month_first = sum(int(r["groups"][1]) > 12 for r in recs)
    source = "given"
    if date_order == "auto":
        if day_first or month_first:
            date_order, source = ("mdy" if month_first > day_first else "dmy"), "detected"
            if day_first and month_first:
                log(f"warning: timestamps disagree on day/month order ({day_first} look day-first, "
                    f"{month_first} month-first); using {date_order}")
        elif fallback:
            date_order, source = fallback
            log(f"note: day/month order is ambiguous in this export; using {date_order}, as the current DB does"
                + (" (a guess there too: check a few dates and pass --date-order if they're wrong)"
                   if source == "guessed" else ""))
        else:
            date_order, source = "dmy", "guessed"
            log("warning: day/month order is ambiguous; assuming dd/mm. Pass --date-order mdy if wrong.")

    out, invalid = [], []
    for r in recs:
        try:
            date, time_ = iso(r["groups"], date_order)
        except ValueError:
            if out:
                invalid.append(r["n"])
                d, t, s, text = out[-1]
                out[-1] = (d, t, s, "\n".join([text, r["raw"], *r["lines"][1:]]).rstrip("\n"))
            else:
                dropped += sum(1 for l in [r["raw"], *r["lines"][1:]] if l.strip())
            continue
        body = "\n".join(r["lines"]).rstrip("\n")
        m = SENDER.match(body)
        # Android system lines have no "Name:" — but one can contain a quoted colon, as in
        # `Ana created group "Trip: Paris"`. A quote left open before the colon gives it away.
        if m and not (r["android"] and open_quote(m.group(1))):
            sender, text = m.group(1).strip(), m.group(2) or ""
        else:
            sender, text = None, body
        out.append((date, time_, sender, text))
    if not out:
        die(f"no message in {path} has a valid date; try --date-order")
    return out, {"dropped": dropped, "near_headers": near, "invalid_dates": invalid,
                 "date_order": date_order, "order_source": source}


def classify(date, time_, sender, text, files=()):
    """Message row for one parsed message. `files` = names present in the export."""
    edited = EDITED in text
    text = "\n".join(" ".join(l.split()) for l in text.replace(EDITED, "").split("\n")).strip()
    found = [m.group(2).strip() for m in IOS_ATTACHED.finditer(text)
             if m.group(1).lower() == "attached" or m.group(2).strip() in files
             or IOS_MEDIA_NAME.match(m.group(2).strip())]
    found += [m.group(1).strip() for m in ANDROID_ATTACHED.finditer(text)
              if m.group(2).lower() == "file attached" or m.group(1).strip() in files]
    media = found[0] if found else None
    kind = "text"
    if media:
        ext = media.rsplit(".", 1)[-1].lower()
        kind = ("sticker" if STICKER.search(media) else "audio" if ext in AUDIO else
                "image" if ext in IMAGE else "video" if ext in VIDEO else "file")
    elif OMITTED.match(text):
        w = OMITTED.match(text).group(1).lower()
        kind = {"image": "image", "gif": "image", "sticker": "sticker", "audio": "audio",
                "video": "video", "document": "file"}.get(w, "omitted")  # Android: untyped "<Media omitted>"
    elif sender is None or text.startswith(E2E):
        kind = "system"
    elif DELETED.match(text):
        kind = "deleted"
    elif CALL.match(text):
        kind = "call"
    return dict(ts=f"{date}T{time_}", date=date, time=time_, year_month=date[:7], sender=sender,
                kind=kind, text=text, media_file=media, char_len=len(text), edited=int(edited),
                attachments=found)


# ---------- ingest ----------

def merge(into, new):
    """Fill gaps in `into` from `new`, field by field; values already in `into` win."""
    for k, v in new.items():
        cur = into.get(k)
        into[k] = v if cur is None else tuple(c if c is not None else n for c, n in zip(cur, v))


class Unusable(Exception):
    """An earlier DB that can't be read for carry-over."""


def carried(path):
    """({file: (transcript, translation)}, {file: (ocr_text, image_desc)}) from an earlier DB.
    Errors are not carried: they describe the old export (e.g. a missing file), not the new one."""
    try:
        old = connect_ro(path)
        try:
            cols = lambda t: {r[1] for r in old.execute(f"pragma table_info({t})")}
            ac, mc = cols("audios"), cols("messages")
            if "file" not in ac or "media_file" not in mc:
                raise Unusable("it has no audios.file / messages.media_file columns")
            pick = lambda have, c: c if c in have else "null"
            audio, media = {}, {}
            for f, t, tr in old.execute(f"select file, {pick(ac, 'transcript')}, {pick(ac, 'translation')} "
                                        "from audios where file is not null"):
                merge(audio, {f: (t, tr)})
            for f, o, d in old.execute(f"select media_file, {pick(mc, 'ocr_text')}, {pick(mc, 'image_desc')} "
                                       "from messages where media_file is not null"):
                merge(media, {f: (o, d)})
        finally:
            old.close()
    except sqlite3.Error as e:
        raise Unusable(str(e)) from e
    return audio, media


def meta_value(db, key):
    try:
        con = connect_ro(db)
        try:
            row = con.execute("select value from meta where key = ?", (key,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def swap_day_month(ts):
    return f"{ts[:5]}{ts[8:10]}-{ts[5:7]}{ts[10:]}"


def dropped_messages(db, records, prev_order, new_order):
    """Describe messages in the current DB that this export lacks, or return None. Messages are
    matched by timestamp, so a contact renamed on the phone doesn't count. If the day/month order
    differs from the current DB's, that is either a correction (the DB misread the same text) or
    a real change of format between exports (e.g. the phone's region changed); both readings are
    tried and the better match wins."""
    con = connect_ro(db)
    try:
        stamps = [ts for ts, in con.execute("select ts from messages") if valid_ts(ts)]
    finally:
        con.close()
    new = Counter(f"{d}T{t}" for d, t, _, _ in records)
    missing = Counter(stamps) - new
    if prev_order and prev_order != new_order:
        swapped = Counter(swap_day_month(ts) for ts in stamps if valid_ts(swap_day_month(ts))) - new
        if sum(swapped.values()) < sum(missing.values()):
            log(f"note: reading dates as {new_order}; the current DB read the same text as {prev_order}, "
                "so its dates are corrected")
            missing = swapped
    if not missing:
        return None
    stamps = sorted(missing)
    return (f"{sum(missing.values())} of its messages aren't in this export ({stamps[0][:10]} -> {stamps[-1][:10]}; "
            f"e.g. at {', '.join(stamps[:3])}): a shorter or capped export, a different chat, or timestamps "
            "shifted by a phone timezone change?")


def valid_ts(ts):
    """False for timestamps that can't exist, e.g. 2025-13-04 stored by an older version."""
    try:
        datetime.datetime.fromisoformat(ts)
        return True
    except (TypeError, ValueError):
        return False


def build(tmp, export_dir, chat, records, files, keep_a, keep_m, diag):
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    con.executemany("insert into meta values(?,?)",
                    [("export_dir", export_dir), ("chat_file", chat), ("ingested_at", stamp()),
                     ("date_order", diag["date_order"]), ("date_order_source", diag["order_source"])])
    applied = {k: set() for k in ("transcript", "translation", "ocr_text", "image_desc")}
    st = dict(multi=0, linked=set(), applied=applied)
    for date, time_, sender, body in records:
        r = classify(date, time_, sender, body, files)
        f = r["media_file"]
        ocr, desc = keep_m.get(f, (None, None)) if f else (None, None)
        mid = con.execute(
            "insert into messages(ts,date,time,year_month,sender,kind,text,media_file,char_len,edited,ocr_text,image_desc)"
            " values(:ts,:date,:time,:year_month,:sender,:kind,:text,:media_file,:char_len,:edited,:ocr,:desc)",
            {**r, "ocr": ocr, "desc": desc}).lastrowid
        st["linked"].update(r["attachments"])
        st["multi"] += len(r["attachments"]) > 1
        if not f:
            continue
        if ocr is not None:
            applied["ocr_text"].add(f)
        if desc is not None:
            applied["image_desc"].add(f)
        path = files.get(f)
        con.execute("insert into media(message_id,file,date,time,sender,type,path) values(?,?,?,?,?,?,?)",
                    (mid, f, date, time_, sender, r["kind"], path))
        if r["kind"] == "audio":
            t, tr = keep_a.get(f, (None, None))
            if t is not None:
                applied["transcript"].add(f)
            if tr is not None:
                applied["translation"].add(f)
            con.execute("insert into audios(message_id,file,date,time,sender,transcript,translation,word_count,error)"
                        " values(?,?,?,?,?,?,?,?,?)",
                        (mid, f, date, time_, sender, t, tr, len(t.split()) if t else None,
                         None if path else "not in export"))
    con.commit()
    con.close()
    return st


def backup(db):
    """Keep the current DB as <db>.<stamp>.bak before it is replaced; return that path."""
    if not os.path.exists(db):
        return None
    bak = unique(f"{db}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
    try:
        os.link(db, bak)
    except OSError:  # no hard links here: copy under a temp name so a failed copy never looks like a backup
        part = bak + ".tmp"
        try:
            shutil.copy2(db, part)
            os.replace(part, bak)
        except BaseException:
            if os.path.exists(part):
                os.remove(part)
            raise
    log(f"previous DB kept as {bak}")
    return bak


def unapplied(src_a, src_m, applied):
    """{label: [files]} for carried-over values that the new DB didn't take."""
    found = (
        ("transcripts", [f for f, (t, _) in src_a.items() if t is not None and f not in applied["transcript"]]),
        ("translations", [f for f, (_, t) in src_a.items() if t is not None and f not in applied["translation"]]),
        ("OCR results", [f for f, (o, _) in src_m.items() if o is not None and f not in applied["ocr_text"]]),
        ("image descriptions", [f for f, (_, d) in src_m.items() if d is not None and f not in applied["image_desc"]]))
    return {label: fs for label, fs in found if fs}


def cmd_ingest(a):
    db = os.path.abspath(a.db)
    os.makedirs(os.path.dirname(db), exist_ok=True)
    if not os.path.exists(a.source):
        die(f"{a.source} does not exist")
    lock(db, "ingest")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # a kill becomes an exception, so cleanup below runs
    had_db = os.path.exists(db)
    problems = []  # everything --allow-drop would discard, refused together
    cur_a, cur_m = {}, {}
    if had_db:
        try:
            cur_a, cur_m = carried(db)
        except Unusable as e:
            problems.append(f"it can't be read for carry-over ({e}), so none of its transcripts or OCR are reused")
    old_a, old_m = {}, {}
    if a.carry_from:
        try:
            old_a, old_m = carried(a.carry_from)
        except Unusable as e:
            die(f"can't reuse {a.carry_from}: {e}")
    keep_a, keep_m = {}, {}
    for src, au, me in ((db, cur_a, cur_m), (a.carry_from, old_a, old_m)):  # the current DB first: its values win
        if au or me:
            log(f"reusing from {src}: {sum(t is not None for t, _ in au.values())} transcripts, "
                f"{sum(o is not None for o, _ in me.values())} OCR results")
        merge(keep_a, au)
        merge(keep_m, me)

    txt_source = os.path.isfile(a.source) and a.source.lower().endswith(".txt")
    if txt_source:  # just the chat log, e.g. a "without media" export
        export_dir, created = os.path.dirname(os.path.realpath(a.source)), None
    else:
        export_dir, created = unpack(a.source, os.path.dirname(db))
    tmp = None
    try:
        if a.chat:  # a name as listed inside the export first; a path of the user's otherwise
            inside = os.path.join(export_dir, a.chat)
            chat = inside if os.path.isfile(inside) else a.chat if os.path.isfile(a.chat) else None
            if not chat:
                die(f"--chat {a.chat}: no such file (give a name as listed inside the export, or a path)")
            chat = os.path.realpath(chat)
        else:
            chat = os.path.realpath(a.source) if txt_source else find_chat_txt(export_dir)
        prev_order = meta_value(db, "date_order") if had_db else None
        fallback = (prev_order, meta_value(db, "date_order_source") or "detected") if prev_order else None
        records, diag = parse(chat, a.date_order, fallback)  # parse everything before writing anything
        if txt_source:
            # Media may sit beside the log. Only files named the way WhatsApp names media count, so
            # chat text like "<attached: taxes.pdf>" can't pull in unrelated files from that folder.
            files = {f: os.path.join(export_dir, f) for f in os.listdir(export_dir)
                     if WA_MEDIA_NAME.match(f) and os.path.isfile(os.path.join(export_dir, f))
                     and not os.path.islink(os.path.join(export_dir, f))}
        else:
            files = export_files(export_dir)
        if had_db:
            try:
                gone = dropped_messages(db, records, prev_order, diag["date_order"])
                if gone:
                    problems.append(gone)
            except sqlite3.Error as e:
                problems.append(f"it can't be compared with this export ({e}), so dropped messages can't be ruled out")
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(db), prefix=os.path.basename(db) + ".", suffix=".partial")
        os.close(fd)
        st = build(tmp, export_dir, chat, records, files, keep_a, keep_m, diag)
        lost = unapplied(cur_a, cur_m, st["applied"])
        if lost:
            problems.append("its " + ", ".join(f"{len(fs)} {label}" for label, fs in lost.items())
                            + f" wouldn't carry over (e.g. {next(iter(lost.values()))[0]}): those files aren't in "
                            "this export as the same kind of attachment (an export without media?)")
        if problems:
            text = "replacing the current DB would lose data:\n" + "\n".join(f"  - {p}" for p in problems)
            if not a.allow_drop:
                die(f"{text}\nNothing was changed. Re-run with --allow-drop to replace it anyway (it is kept as a .bak).")
            log(f"warning: {text}\nReplacing anyway (--allow-drop); the current DB is kept as a .bak.")
        bak = backup(db)
        os.replace(tmp, db)  # atomic: the DB is either the old one or the complete new one
        tmp = None
    except BaseException:
        for p in (tmp, tmp and tmp + "-journal"):
            if p and os.path.exists(p):
                os.remove(p)  # only ever this run's half-built DB
        if created:
            remove_extraction(created)
        raise

    log(f"chat log: {chat}")
    where = " or ".join(p for p in (bak, a.carry_from) if p)
    for label, fs in unapplied(keep_a, keep_m, st["applied"]).items():
        log(f"warning: {len(fs)} earlier {label} weren't applied: their file isn't in this export as that kind "
            f"of attachment (e.g. {fs[0]}). They remain in {where}.")
    if keep_a or keep_m:
        log(f"reused {len(st['applied']['transcript'])} transcripts and {len(st['applied']['ocr_text'])} OCR results")
    # Line numbers (of the chat log above) only: chat text is private and untrusted, so it isn't echoed.
    if diag["dropped"]:
        log(f"warning: ignored {diag['dropped']} lines before the first message")
    if diag["invalid_dates"]:
        log(f"note: {len(diag['invalid_dates'])} header-like lines have impossible dates and were kept as text "
            f"of the previous message (line {', '.join(map(str, diag['invalid_dates'][:5]))})")
    if diag["near_headers"]:
        log(f"note: {len(diag['near_headers'])} continuation lines start with a date "
            f"(line {', '.join(str(n) for n, _ in diag['near_headers'][:5])}). Usually message text; if they "
            "are message headers, the format isn't recognised.")
    if st["multi"]:
        log(f"warning: {st['multi']} messages reference more than one attachment; only the first is linked")
    orphans = [f for f in files if f.rsplit(".", 1)[-1].lower() in AUDIO | IMAGE | VIDEO and f not in st["linked"]]
    if orphans and not txt_source:  # beside a bare .txt, other media may belong to other chats
        log(f"warning: {len(orphans)} media files in the export aren't referenced by any message "
            f"(e.g. {orphans[0]}). If that's most of them, the attachment markers weren't recognised.")
    con = connect(db)
    absent = [f for f, in con.execute("select file from media where path is null")]
    beside = [f for f in absent if txt_source and os.path.isfile(os.path.join(export_dir, f))]
    if beside:
        log(f"note: {len(beside)} referenced files beside the chat log weren't linked because their names don't "
            f"follow WhatsApp's media naming (e.g. {beside[0]}). To link them, ingest the export folder instead.")
    if len(absent) > len(beside):
        log(f"note: {len(absent) - len(beside)} attachments are referenced in the chat but absent from the export")
    status(con)


# ---------- runs: transcribe / ocr ----------

def describe(e):
    """Exception type plus the last non-empty line of its message. Tools like ffmpeg put a long
    version banner first and the actual cause last."""
    lines = [l.strip() for l in str(e).splitlines() if l.strip()]
    return f"{type(e).__name__}: {lines[-1] if lines else ''}"[:500]


def tracked(db, verb, body, fork=None):
    """Run body(con, last_run), recording its start and outcome in meta (key last_run:<verb>) and
    logging a timestamped line if it dies, so a crashed (possibly detached) run can't pass for
    one that never started, or for an earlier run. `fork` (detach) runs after the start is
    recorded, so a DB that can't be written fails in the launching shell, not only in a log."""
    state = {"con": connect(db)}
    key = f"last_run:{verb}"

    def last_run(value, required=False):
        con = state["con"]
        try:
            con.rollback()  # never block on (or commit) a half-done item
            con.execute("insert or replace into meta values(?, ?)", (key, f"{verb} {value}"))
            con.execute("delete from meta where key = 'last_run'")  # an older single-slot record
            con.commit()
        except sqlite3.Error as e:
            if required:
                die(f"can't record the run in {db} ({e}): is it read-only, the disk full, or the file held "
                    "by another program? Nothing was processed.")
            log(f"{stamp()} could not record run state: {e}")

    last_run(f"started {stamp()} pid {os.getpid()}", required=True)
    if fork:
        state["con"].close()
        fork()
        state["con"] = connect(db)
        last_run(f"started {stamp()} pid {os.getpid()}", required=True)
    try:
        body(state["con"], last_run)
    except SystemExit as e:
        if isinstance(e.code, str):  # die(): the message is printed as the process exits
            log(f"{stamp()} STOPPED")
            last_run(f"stopped {stamp()}: {e.code[:300]}")
        raise
    except KeyboardInterrupt:
        log(f"{stamp()} interrupted; finished items are saved")
        last_run(f"interrupted {stamp()}")
        raise
    except BaseException as e:
        log(f"{stamp()} ABORTED: {describe(e)} (traceback below)")
        last_run(f"aborted {stamp()}: {describe(e)}")
        raise


class Rejected(Exception):
    """A file refused before processing (empty, not an image, ...). That's a fact about the file,
    not the setup, so it doesn't count toward the failure streak, and retrying won't change it.
    The message is stable (so status can group them); `detail` goes to the log only."""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.detail = detail


def work_loop(con, last_run, todo, attempt, save, record_error, what, setup_hint):
    """Run `attempt(path)` per (id, name, path, retrying) item, saving results and recording
    failures. Returns (failed, of which had failed before, of which were rejected)."""
    def on_term(*_):
        log(f"{stamp()} stopped by SIGTERM; finished items are saved, rerun the same command to resume")
        last_run(f"stopped by SIGTERM {stamp()}")
        sys.exit(143)
    signal.signal(signal.SIGTERM, on_term)
    t0, failed, repeats, rejected, streak = time.time(), 0, 0, 0, []
    for i, (item_id, name, path, retrying) in enumerate(todo, 1):
        try:
            result = attempt(path)
        except Exception as e:  # one bad file mustn't end a multi-hour run; it's retried with --retry-errors
            err = describe(e)
            detail = f" ({e.detail})" if getattr(e, "detail", "") else ""
            log(f"{stamp()} FAIL {name}: {err}{detail}")  # before the DB write, so it's logged even if that fails
            full = str(e).strip()
            if "\n" in full:
                log(textwrap.indent(full[-2000:], "    "))
            record_error(item_id, err)
            con.commit()
            failed += 1
            if retrying:  # a known-bad file failing again says nothing about the setup
                repeats += 1
            elif isinstance(e, Rejected):
                rejected += 1
            else:
                streak.append(name)
            if len(streak) >= MAX_STREAK:
                die(f"{len(streak)} {what} in a row failed ({', '.join(streak[:3])}, ...). Either the setup is "
                    f"broken (see the FAIL lines; e.g. {setup_hint}) or these files are bad. Fix the setup and "
                    "rerun; if the same files fail again, a plain rerun (without --retry-errors) skips them.")
            continue
        streak = []
        save(item_id, result)
        con.commit()  # per item: a killed run loses at most the item in progress
        if i == 1 or i % 10 == 0 or i == len(todo):
            el = time.time() - t0
            log(f"{stamp()} {i}/{len(todo)}  {el/60:.1f}min  {el/i:.1f}s/item  eta={el/i*(len(todo)-i)/60:.0f}min")
    return failed, repeats, rejected


def missing_on_disk(todo):
    gone = [p for _, _, p, _ in todo if not os.path.exists(p)]
    if gone:
        die(f"{len(gone)} of {len(todo)} files are not on disk (e.g. {gone[0]}). Was the export folder "
            "moved or deleted? Re-ingest from where it is now. Nothing was marked.")


def check_backend(backend, lang):
    """Fail before forking: the backend must import in this interpreter, and --lang must be one
    of its language codes (Whisper only rejects it after loading the model)."""
    pkg = "mlx-whisper" if backend == "mlx" else "faster-whisper"
    if backend == "mlx" and not shutil.which("ffmpeg"):
        die("mlx-whisper decodes audio with ffmpeg, which is not on PATH (brew install ffmpeg)")
    probe = {"mlx": "from mlx_whisper.tokenizer import LANGUAGES as L, TO_LANGUAGE_CODE as N",
             "faster": "from faster_whisper.tokenizer import _LANGUAGE_CODES as L; N = ()"}[backend]
    code = probe + "\nimport sys\nl = sys.argv[1].lower()\nsys.exit(0 if not l or l in L or l in N else 3)"
    r = subprocess.run([sys.executable, "-c", code, lang or ""], capture_output=True, text=True)
    if r.returncode == 3:
        die(f"--lang {lang!r} isn't a Whisper language. Use a 2-letter code like es, en, pt "
            "(tesseract's spa/eng/por are a different scheme).")
    if r.returncode:
        err = (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
        die(f"{pkg} isn't usable ({err}). Run via: uv run --with {pkg} python3 {sys.argv[0]} transcribe ...")


def load_backend(name, model, lang, task):
    if name == "mlx":
        # mlx's multiprocessing helper warns at exit in a detached run; harmless, and it reads like
        # a crash. It warns from its own process, which inherits this environment.
        os.environ.setdefault("PYTHONWARNINGS", "ignore:resource_tracker:UserWarning")
        import mlx_whisper
        model = model or "mlx-community/whisper-large-v3-mlx"
        return lambda p: mlx_whisper.transcribe(p, path_or_hf_repo=model, language=lang,
                                                task=task, fp16=True)["text"]
    from faster_whisper import WhisperModel
    m = WhisperModel(model or "large-v3", compute_type="auto")
    return lambda p: "".join(s.text for s in m.transcribe(p, language=lang, task=task)[0])


def open_log(logfile):
    try:
        return os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError as e:
        die(f"can't open the log {logfile} ({e}); nothing was started")


def detach(out, lockfd, what):
    """Fork into a new session so the run survives the launching shell exiting; `out` is the
    already-open log. The child keeps the lock."""
    pid = os.fork()
    if pid:
        log(f"detached as pid {pid}; progress in the log")
        os._exit(0)
    os.setsid()
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0); os.dup2(out, 1); os.dup2(out, 2)
    os.environ["PYTHON_COLORS"] = "0"  # no ANSI codes in the log, even if the launcher set FORCE_COLOR
    mark_lock(lockfd, what)
    faulthandler.enable()  # native crashes (e.g. in MLX) still leave a traceback in the log


def cmd_transcribe(a):
    db = os.path.abspath(a.db)
    if a.translate and a.model and "turbo" in a.model:
        die("turbo models weren't trained to translate; they return the original language. Use large-v3.")
    check_backend(a.backend, a.lang)
    verb = "translate" if a.translate else "transcribe"
    lockfd = lock(db, verb)
    col, errcol = ("translation", "translation_error") if a.translate else ("transcript", "error")
    cond = f"a.{col} is null and a.{errcol} is null"
    if a.retry_empty:
        cond += f" or a.{col} = ''"
    if a.retry_errors:
        cond += f" or a.{errcol} is not null"
    con = connect(db)
    todo = con.execute(  # new clips first: known-bad retries mustn't block them
        f"select a.id, a.file, m.path, a.{errcol} is not null from audios a join media m on m.message_id = a.message_id"
        f" where m.path is not null and ({cond}) order by a.{errcol} is not null, a.id").fetchall()
    con.close()
    todo = todo[:a.limit] if a.limit else todo
    missing_on_disk(todo)
    log(f"{len(todo)} voice notes to {verb}")
    if not todo:
        return

    def body(con, last_run):
        log(f"{stamp()} start pid={os.getpid()} {verb} todo={len(todo)} backend={a.backend} "
            f"model={a.model or 'default'} lang={a.lang or 'auto'}")
        run = load_backend(a.backend, a.model, a.lang, verb)

        def save(aid, txt):
            txt = txt.strip()
            if a.translate:
                con.execute("update audios set translation=?, translation_error=null where id=?", (txt, aid))
            else:
                con.execute("update audios set transcript=?, word_count=?, error=null where id=?",
                            (txt, len(txt.split()), aid))

        finish(last_run, len(todo), work_loop(
            con, last_run, todo, run, save,
            lambda aid, err: con.execute(f"update audios set {errcol}=? where id=?", (err, aid)), "voice notes",
            "ffmpeg missing, a wrong --model or --lang, or no network for the first model download"))

    fork = None
    if a.detach:  # open the log first: a bad path fails here, before anything is recorded
        logfile = os.path.join(os.path.dirname(db), "transcribe.log")
        out = open_log(logfile)
        log(f"log: {logfile}")
        fork = lambda: detach(out, lockfd, verb)
    tracked(db, verb, body, fork)


def finish(last_run, total, result):
    failed, repeats, rejected = result
    extra = [f"{n} {what}" for n, what in ((repeats, "had failed before"), (rejected, "rejected")) if n]
    summary = f"{total - failed} ok, {failed} failed" + (f" ({', '.join(extra)})" if extra else "")
    last_run(f"finished {stamp()}: {summary}")
    if failed - repeats - rejected:
        advice = "; check the FAIL lines, then rerun with --retry-errors"
    elif rejected >= MAX_STREAK and rejected == total:
        advice = ("; every file was rejected, which looks systemic (placeholders instead of media, or a broken "
                  "transfer?) rather than bad files: check a few of them")
    elif failed:
        advice = "; only known-bad or rejected files failed, and retrying won't change them: leave them"
    else:
        advice = ""
    log(f"{stamp()} finished: {summary}{advice}. Run `index` next.")
    sys.exit(1 if failed else 0)


def image_type(path):
    """The image format the file's first bytes show, or None. Only such files may reach tesseract:
    it reads anything its image library doesn't recognise as a list of image paths and OCRs *those*
    files, so a crafted attachment could pull text out of other images on this machine. The
    signatures match what that library checks, and it rejects files under 12 bytes."""
    with open(path, "rb") as fh:
        head = fh.read(16)
    if len(head) < 12:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS:
        return "heic"
    return next((kind for sig, kind in MAGIC if head.startswith(sig)), None)


def rejection(path):
    """A Rejected for a file image_type refused."""
    size = os.path.getsize(path)
    if size == 0:
        return Rejected("empty file")
    with open(path, "rb") as fh:
        head = fh.read(8)
    return Rejected("not a recognised image; not given to tesseract",
                    f"starts with {head.hex(' ')}; {size} bytes: corrupt, another format, or deliberately disguised")


def tesseract(path, lang):
    # From an empty directory: if a file ever did reach tesseract's list mode, the list's first
    # entry (the signature bytes, never an absolute path) couldn't resolve to anything.
    # realpath: this tesseract can't open /tmp/... but can open the same file as /private/tmp/...
    with tempfile.TemporaryDirectory() as empty:
        r = subprocess.run(["tesseract", os.path.realpath(path), "-", "-l", lang, "--psm", "3"], cwd=empty,
                           capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode:
        useful = [l.strip() for l in r.stderr.splitlines() if l.strip() and l.strip() != "Error during processing."]
        raise RuntimeError("; ".join(useful[-2:]) or f"unreadable image (tesseract exit {r.returncode})")
    return " ".join(r.stdout.split())


def cmd_ocr(a):
    db = os.path.abspath(a.db)
    if not shutil.which("tesseract"):
        die("tesseract not found (brew install tesseract tesseract-lang)")
    r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode:
        die(f"tesseract is installed but doesn't run: {(r.stderr.strip().splitlines() or [f'exit {r.returncode}'])[-1]}")
    have = {l.strip() for l in r.stdout.splitlines()[1:] if l.strip()}  # first line is a header
    absent = [l for l in a.lang.split("+") if l not in have]
    if absent:
        die(f"tesseract language data missing for {absent} (brew install tesseract-lang). Codes are "
            f"3-letter (spa, eng, por), not Whisper's 2-letter ones. Installed: {', '.join(sorted(have))}")
    sips = shutil.which("sips")  # macOS: converts HEIC, which tesseract can't read
    lock(db, "ocr")
    con = connect(db)
    images = "from messages m join media md on md.message_id = m.id where m.kind = 'image' and md.path is not null"
    retry = " or (m.ocr_error is not null and m.ocr_error not like 'unsupported format%')" if a.retry_errors else ""
    touched = ("m.ocr_text is null and m.ocr_error is null" + retry
               + (" or m.ocr_text = ''" if a.redo_blank else "")
               + (" or m.ocr_error like 'unsupported format: heic%'" if sips else ""))
    # Check the files this run will touch before changing anything.
    missing_on_disk(con.execute(f"select m.id, m.media_file, md.path, 0 {images} and ({touched})").fetchall())
    if a.redo_blank:
        n = con.execute("update messages set ocr_text = null where kind = 'image' and ocr_text = ''"
                        " and id in (select message_id from media where path is not null)").rowcount
        log(f"re-queued {n} images with no text found")
    if sips:
        n = con.execute("update messages set ocr_error = null where kind = 'image'"
                        " and ocr_error like 'unsupported format: heic%'").rowcount
        if n:
            log(f"re-queued {n} HEIC images marked unsupported earlier: sips can convert them")
    else:
        n = con.execute("update messages set ocr_error = 'unsupported format: heic (needs sips to convert)'"
                        " where kind = 'image' and ocr_text is null and ocr_error is null"
                        " and lower(media_file) like '%.heic'"
                        " and id in (select message_id from media where path is not null)").rowcount
        if n:
            log(f"skipped {n} HEIC images: tesseract can't read them and sips isn't available to convert them")
    con.commit()
    todo = con.execute(  # new images first: known-bad retries mustn't block them
        f"select m.id, m.media_file, md.path, m.ocr_error is not null {images}"
        f" and (m.ocr_text is null and m.ocr_error is null{retry}) order by m.ocr_error is not null, m.id").fetchall()
    con.close()
    log(f"{len(todo)} images to OCR")
    if not todo:
        return

    def attempt(path):
        kind = image_type(path)
        if kind is None:
            raise rejection(path)
        if kind != "heic":
            return tesseract(path, a.lang)
        if not sips:
            raise Rejected("HEIC needs sips to convert it")
        with tempfile.TemporaryDirectory() as d:
            jpg = os.path.join(d, "image.jpg")
            r = subprocess.run([sips, "-s", "format", "jpeg", path, "--out", jpg],
                               capture_output=True, encoding="utf-8", errors="replace", timeout=120)
            if r.returncode or not os.path.exists(jpg):
                errs = [l.strip() for l in r.stderr.splitlines() if l.strip().startswith("Error")]
                raise RuntimeError(f"sips couldn't convert this HEIC: {'; '.join(errs) or f'exit {r.returncode}'}")
            return tesseract(jpg, a.lang)

    def body(con, last_run):
        def save(mid, txt):
            # Most photos have no text and tesseract emits short garbage for them; '' = checked, no text.
            con.execute("update messages set ocr_text=?, ocr_error=null where id=?",
                        (txt if len(txt) >= a.min_chars else "", mid))

        finish(last_run, len(todo), work_loop(
            con, last_run, todo, attempt, save,
            lambda mid, err: con.execute("update messages set ocr_error=? where id=?", (err, mid)), "images",
            "tesseract or its language data broken"))

    tracked(db, "ocr", body)


# ---------- index / status ----------

def cmd_index(a):
    db = os.path.abspath(a.db)
    lock(db, "index")
    con = connect(db)
    con.execute("update messages set transcript = (select transcript from audios where message_id = messages.id),"
                " translation = (select translation from audios where message_id = messages.id)"
                " where kind = 'audio'")
    con.execute("insert into messages_fts(messages_fts) values('rebuild')")
    con.execute("insert into audios_fts(audios_fts) values('rebuild')")
    con.execute("insert into all_fts(all_fts) values('delete-all')")  # contentless: plain DELETE is refused
    con.execute("insert into all_fts(rowid, body) select id, trim(coalesce(text,'')||' '||coalesce(transcript,'')"
                "||' '||coalesce(translation,'')||' '||coalesce(ocr_text,'')) from messages")
    con.commit()
    status(con)


def status(con):
    q = lambda sql: con.execute(sql).fetchone()[0]
    on_disk = "from audios a join media m on m.message_id = a.message_id where m.path is not null"
    log(f"messages {q('select count(*) from messages')}  "
        f"({q('select min(date) from messages')} -> {q('select max(date) from messages')})")
    for k, n in con.execute("select kind, count(*) from messages group by 1 order by 2 desc"):
        log(f"  {k:8} {n}")
    for s, n in con.execute("select sender, count(*) from messages where sender is not null group by 1 order by 2 desc"):
        log(f"  sender {s}: {n}")
    log("voice notes {}: transcribed {}, blank {}, pending {}, failed {}, not in export {}".format(
        q("select count(*) from audios"),
        q("select count(*) from audios where transcript != ''"),
        q("select count(*) from audios where transcript = '' and error is null"),
        q(f"select count(*) {on_disk} and a.transcript is null and a.error is null"),
        q(f"select count(*) {on_disk} and a.error is not null"),
        q("select count(*) from audios where error = 'not in export' and transcript is null")))
    if q("select count(*) from audios where translation is not null or translation_error is not null"):
        log("translations: done {}, pending {}, failed {}".format(
            q("select count(*) from audios where translation is not null"),
            q(f"select count(*) {on_disk} and a.translation is null and a.translation_error is null"),
            q("select count(*) from audios where translation_error is not null")))
    img = "from messages m join media md on md.message_id = m.id where m.kind = 'image'"
    log("images {}: with text {}, no text {}, pending {}, failed {}, unsupported {}, no file {}".format(
        q("select count(*) from messages where kind = 'image'"),
        q(f"select count(*) {img} and m.ocr_text != ''"),
        q(f"select count(*) {img} and m.ocr_text = ''"),
        q(f"select count(*) {img} and md.path is not null and m.ocr_text is null and m.ocr_error is null"),
        q(f"select count(*) {img} and m.ocr_error not like 'unsupported format%'"),
        q(f"select count(*) {img} and m.ocr_error like 'unsupported format%'"),
        q("select count(*) from messages m where kind = 'image' and ocr_text is null and ocr_error is null"
          " and not exists (select 1 from media md where md.message_id = m.id and md.path is not null)")))
    for step, err, n in con.execute(
            "select step, substr(e, 1, 100), count(*) from ("
            " select 'transcribe' step, error e from audios where error != 'not in export'"
            " union all select 'translate', translation_error from audios"
            " union all select 'ocr', ocr_error from messages where ocr_error not like 'unsupported format%')"
            " where e is not null group by 1, 2 order by 3 desc limit 8"):
        log(f"  {step} failure x{n}: {err}")
    for value, in con.execute("select value from meta where key like 'last_run:%' order by key"):
        log(f"last run: {value}")


def cmd_status(a):
    db = os.path.abspath(a.db)
    run = running(db)
    con = connect(db)
    records = [v for v, in con.execute("select value from meta where key like 'last_run:%'")]
    unfinished = [v for v in records if " started " in v and not (run and v.endswith(f"pid {run[0]}"))]
    if run:
        log(f"RUNNING: {run[1]} (pid {run[0]}) holds {db}.lock")
    for v in unfinished:
        log(f"WARNING: a run ended without finishing ({v}); the process was killed or crashed. "
            f"See {os.path.join(os.path.dirname(db), 'transcribe.log')} if it was detached.")
    if not run and not unfinished:
        log("no run in progress")
    current = con.execute("select value from meta where key = 'export_dir'").fetchone()
    stale = [p for p in sorted(glob.glob(os.path.join(os.path.dirname(db), "export-*")))
             if os.path.isdir(p) and (not current or os.path.realpath(p) != current[0])]
    if stale and not (run and run[1] == "ingest"):  # a running ingest's new extraction isn't in meta yet
        log(f"superseded extractions, not used by the current DB (.bak copies may still point into them): "
            f"{', '.join(stale)}")
    status(con)


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `status | head` should just stop
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ingest", help="parse an export into a new DB")
    s.add_argument("source", help="export .zip, unzipped folder, or bare chat .txt (a folder must stay where it "
                                  "is: the DB stores paths into it)")
    s.add_argument("--date-order", choices=["auto", "dmy", "mdy"], default="auto",
                   help="day/month order of the timestamps (default: detect)")
    s.add_argument("--carry-from", help="another DB to reuse transcripts/OCR from, in addition to --db if it exists")
    s.add_argument("--chat", help="the chat log to use if the export has several: a path, or a name as listed inside the export")
    s.add_argument("--allow-drop", action="store_true",
                   help="replace the DB even if it has messages this export lacks (they stay in the .bak)")
    s = sub.add_parser("transcribe", help="transcribe voice notes locally")
    s.add_argument("--backend", choices=["mlx", "faster"], default="mlx",
                   help="mlx = mlx-whisper (Apple Silicon); faster = faster-whisper (Linux/Intel/CUDA)")
    s.add_argument("--model", help="default: whisper-large-v3 for the chosen backend")
    s.add_argument("--lang", type=str.lower,
                   help="ISO 639-1 code, e.g. es. Recommended: auto-detect misfires on short clips")
    s.add_argument("--translate", action="store_true",
                   help="separate pass: Whisper's translate task (English only) into `translation`; doesn't fill `transcript`")
    s.add_argument("--retry-empty", action="store_true", help="also redo clips whose result was blank")
    s.add_argument("--retry-errors", action="store_true", help="also redo clips that failed before (after the new ones)")
    s.add_argument("--limit", type=int, help="process at most N clips (smoke test)")
    s.add_argument("--detach", action="store_true",
                   help="run in the background in its own session, logging to <db dir>/transcribe.log")
    s = sub.add_parser("ocr", help="OCR images with tesseract")
    s.add_argument("--lang", default="eng", help="tesseract languages, 3-letter codes joined by +, e.g. spa+eng")
    s.add_argument("--min-chars", type=int, default=12, help="shorter OCR output is stored as '' (no text)")
    s.add_argument("--retry-errors", action="store_true", help="also redo images that failed before (after the new ones)")
    s.add_argument("--redo-blank", action="store_true", help="also redo images where no text was found before")
    sub.add_parser("index", help="copy transcripts onto messages and rebuild full-text search")
    sub.add_parser("status", help="progress counts, failures, and the state of the last run")
    for sp in sub.choices.values():
        sp.add_argument("--db", required=True, help="path to the SQLite file")
    a = p.parse_args()
    if a.cmd != "ingest" and not os.path.exists(a.db):
        die(f"{a.db} does not exist; run ingest first")
    {"ingest": cmd_ingest, "transcribe": cmd_transcribe, "ocr": cmd_ocr, "index": cmd_index,
     "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
