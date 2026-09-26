#!/usr/bin/env python3
"""Tests for wa2db.py"""
import argparse
import fcntl
import hashlib
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wa2db
from wa2db import classify, parse

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wa2db.py")


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, SCRIPT, *map(str, args)], capture_output=True, text=True, cwd=cwd)


def make_export(root, lines, files=(), name="_chat.txt"):
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    for f in files:
        (root / f).write_bytes(b"not really media")
    return root


def chat_file(tmp_path, lines):
    p = tmp_path / "_chat.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def rows(db, sql, *params):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


IOS_CHAT = [
    "‎[21/04/2025, 11:40:03] Ana: ‎Messages and calls are end-to-end encrypted. Only people in this chat can read them.",
    "[21/04/2025, 11:41:00] Ana: hola",
    "[21/04/2025, 11:42:00] Bob: ‎<attached: 00000001-AUDIO-2025-04-21-11-42-00.opus>",
    "[21/04/2025, 11:43:00] Bob: ‎<attached: 00000002-AUDIO-2025-04-21-11-43-00.opus>",
    "[21/04/2025, 11:44:00] Ana: ‎<attached: 00000003-PHOTO-2025-04-21-11-44-00.jpg>",
]
IOS_FILES = ["00000001-AUDIO-2025-04-21-11-42-00.opus", "00000002-AUDIO-2025-04-21-11-43-00.opus",
             "00000003-PHOTO-2025-04-21-11-44-00.jpg"]


class TestParse:
    def test_ios_line(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ["‎[21/04/2025, 11:40:03] Ana: hola"]))
        assert recs == [("2025-04-21", "11:40:03", "Ana", "hola")]

    def test_android_12h_us_dates(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ["1/13/25, 9:05 PM - Ana: hi", "1/14/25, 12:10 AM - Bob: yo"]))
        assert recs == [("2025-01-13", "21:05:00", "Ana", "hi"), ("2025-01-14", "00:10:00", "Bob", "yo")]

    def test_continuation_lines_keep_line_breaks(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ["[21/04/2025, 11:40:03] Ana: one", "two", "[21/04/2025, 11:41:00] Bob: x"]))
        assert classify(*recs[0])["text"] == "one\ntwo"

    def test_empty_body_keeps_sender(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ["[21/04/2025, 11:40:03] Ana:"]))
        assert recs == [("2025-04-21", "11:40:03", "Ana", "")]

    def test_android_system_line_has_no_sender(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ["13/01/2025, 21:05 - Messages and calls are end-to-end encrypted."]))
        assert recs[0][2] is None
        assert classify(*recs[0])["kind"] == "system"

    def test_ambiguous_dates_default_to_day_first(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ["[01/02/2025, 10:00:00] Ana: x"]))
        assert recs[0][0] == "2025-02-01"

    def test_explicit_month_first(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ["[01/02/2025, 10:00:00] Ana: x"]), "mdy")
        assert recs[0][0] == "2025-01-02"

    def test_lines_before_first_header_are_counted(self, tmp_path):
        _, diag = parse(chat_file(tmp_path, ["junk", "[01/02/2025, 10:00:00] Ana: x"]))
        assert diag["dropped"] == 1

    def test_non_utf8_fails_loudly(self, tmp_path):
        p = tmp_path / "_chat.txt"
        p.write_bytes("[01/02/2025, 10:00:00] José: ¿qué tal?\n".encode("cp1252"))
        with pytest.raises(SystemExit):
            parse(p)


    def test_impossible_date_is_message_text(self, tmp_path):
        recs, diag = parse(chat_file(tmp_path, ["[21/04/2025, 10:00:00] Ana: look at this",
                                                "[04/13/2025, 10:00:00] Bob: pasted from elsewhere"]))
        assert len(recs) == 1 and "[04/13/2025, 10:00:00] Bob: pasted" in recs[0][3]
        assert len(diag["invalid_dates"]) == 1

    def test_mixed_order_follows_the_majority(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ["[21/04/2025, 10:00:00] Ana: a", "[22/04/2025, 10:00:00] Ana: b",
                                             "[04/13/2025, 10:00:00] Ana: c"]))
        assert [r[0] for r in recs] == ["2025-04-21", "2025-04-22"]

    def test_android_system_line_with_quoted_colon(self, tmp_path):
        recs, _ = parse(chat_file(tmp_path, ['13/01/2025, 21:05 - Ana created group "Trip: Paris"']))
        assert recs[0][2] is None and classify(*recs[0])["kind"] == "system"

    def test_date_like_continuation_lines_are_reported_with_an_example(self, tmp_path):
        _, diag = parse(chat_file(tmp_path, ["[21/04/2025, 10:00:00] Ana: reminders:", "12/05/2025 dentist"]))
        assert diag["near_headers"] == [(2, "12/05/2025 dentist")]


    @pytest.mark.parametrize("line", ['[21/04/2025, 11:41:00] Juan "Pato" Pérez: qué tal',
                                      '21/04/2025, 11:41 - Juan "Pato" Pérez: qué tal',
                                      '21/04/2025, 11:41 - Ana \u201cNana\u201d Ruiz: qué tal'])
    def test_contact_names_with_quotes_keep_their_sender(self, tmp_path, line):
        recs, _ = parse(chat_file(tmp_path, [line]))
        assert recs[0][2] in ('Juan "Pato" Pérez', 'Ana \u201cNana\u201d Ruiz') and recs[0][3] == "qué tal"

    def test_impossible_date_on_the_first_message_is_counted_as_dropped(self, tmp_path):
        recs, diag = parse(chat_file(tmp_path, ["[04/13/2025, 10:00:00] Bob: pasted", "more", "[21/04/2025, 10:00:00] Ana: hi"]))
        assert len(recs) == 1 and diag["dropped"] == 2 and diag["invalid_dates"] == []


class TestClassify:
    def c(self, text, files=()):
        return classify("2025-01-01", "10:00:00", "Ana", text, files)

    def test_ios_attachment(self):
        r = self.c("<attached: 00000007-AUDIO-2025-01-01-10-00-00.opus>")
        assert (r["kind"], r["media_file"]) == ("audio", "00000007-AUDIO-2025-01-01-10-00-00.opus")

    def test_ios_attachment_in_another_ui_language(self):
        assert self.c("<adjunto: 00000007-AUDIO-2025-01-01-10-00-00.opus>")["kind"] == "audio"

    def test_ios_attachment_with_caption(self):
        r = self.c("Informe.pdf • 3 pages <attached: 00001416-Informe.pdf>")
        assert (r["kind"], r["media_file"]) == ("file", "00001416-Informe.pdf")

    def test_android_filename_with_spaces(self):
        r = self.c("Presupuesto final.pdf (file attached)")
        assert r["media_file"] == "Presupuesto final.pdf"

    def test_android_other_language_needs_the_file_in_the_export(self):
        assert self.c("PTT-20250113-WA0001.opus (archivo adjunto)")["kind"] == "text"
        assert self.c("PTT-20250113-WA0001.opus (archivo adjunto)", {"PTT-20250113-WA0001.opus"})["kind"] == "audio"

    def test_stickers(self):
        assert self.c("<attached: 00000008-STICKER-2025-01-01-10-00-00.was>")["kind"] == "sticker"
        assert self.c("<attached: 00000009-STICKER-2025-01-01-10-00-00.webp>")["kind"] == "sticker"
        assert self.c("STK-20250113-WA0003.webp (file attached)")["kind"] == "sticker"

    def test_omitted_media(self):
        assert (self.c("image omitted")["kind"], self.c("image omitted")["media_file"]) == ("image", None)
        assert self.c("<Media omitted>")["kind"] == "omitted"

    @pytest.mark.parametrize("text", ["Voice call. 2 min", "Missed voice call. Tap to call back",
                                      "Silenced voice call. Focus mode", "Missed video call"])
    def test_calls(self, text):
        assert self.c(text)["kind"] == "call"

    @pytest.mark.parametrize("text", ["can we do a video call later?", "Video call tonight?"])
    def test_text_mentioning_calls(self, text):
        assert self.c(text)["kind"] == "text"

    def test_deleted_and_edited(self):
        assert self.c("This message was deleted")["kind"] == "deleted"
        r = self.c("hola <This message was edited>")
        assert (r["text"], r["edited"]) == ("hola", 1)

    def test_e2e_mention_in_normal_text_is_not_system(self):
        assert self.c("are these end-to-end encrypted?")["kind"] == "text"

    def test_counts_multiple_attachments(self):
        assert self.c("<attached: a-AUDIO.opus>\n<attached: b-AUDIO.opus>")["attachments"] == ["a-AUDIO.opus", "b-AUDIO.opus"]


    @pytest.mark.parametrize("text", ["<Note: see report.pdf>", "<edit: fixed in v2.1>", "<PS: bring the cable.jpg>"])
    def test_angle_brackets_in_ordinary_text_are_not_attachments(self, text):
        assert self.c(text)["kind"] == "text"

    def test_ios_attachment_name_in_the_export_is_accepted(self):
        assert self.c("<adjunto: foto.jpg>", {"foto.jpg"})["kind"] == "image"


class TestIngestAndIndex:
    def ingest(self, tmp_path, lines=IOS_CHAT, files=IOS_FILES, *extra):
        export = make_export(tmp_path / "export", lines, files)
        db = tmp_path / "out" / "chat.sqlite"
        r = cli("ingest", export, "--db", db, *extra)
        assert r.returncode == 0, r.stderr
        return export, db

    def test_ingest_counts(self, tmp_path):
        _, db = self.ingest(tmp_path)
        assert dict(rows(db, "select kind, count(*) from messages group by 1")) == {
            "system": 1, "text": 1, "audio": 2, "image": 1}
        assert rows(db, "select count(*) from audios where error is null") == [(2,)]

    def test_index_can_run_repeatedly(self, tmp_path):
        _, db = self.ingest(tmp_path)
        rows(db, "select 1")
        con = sqlite3.connect(db)
        con.execute("update audios set transcript = 'nos vemos en la estación' where id = 1")
        con.commit()
        con.close()
        for _ in range(2):
            r = cli("index", "--db", db)
            assert r.returncode == 0, r.stderr
        hits = rows(db, "select m.kind from all_fts f join messages m on m.id = f.rowid where all_fts match 'estación'")
        assert hits == [("audio",)]

    def test_reingest_keeps_transcripts_but_not_errors(self, tmp_path):
        # First export lacks the second voice note; the next one has it.
        export, db = self.ingest(tmp_path, IOS_CHAT, IOS_FILES[:1] + IOS_FILES[2:])
        assert rows(db, "select error from audios where id = 2") == [("not in export",)]
        con = sqlite3.connect(db)
        con.execute("update audios set transcript = 'hola hola' where id = 1")
        con.commit()
        con.close()
        (export / IOS_FILES[1]).write_bytes(b"now present")
        r = cli("ingest", export, "--db", db)
        assert r.returncode == 0, r.stderr
        assert rows(db, "select id, transcript, error from audios order by id") == [
            (1, "hola hola", None), (2, None, None)]
        assert len(list(db.parent.glob("chat.sqlite.*.bak"))) == 1

    def test_failed_ingest_leaves_db_untouched(self, tmp_path):
        _, db = self.ingest(tmp_path)
        before = hashlib.md5(db.read_bytes()).hexdigest()
        bad = make_export(tmp_path / "bad", ["not a whatsapp export"])
        r = cli("ingest", bad, "--db", db)
        assert r.returncode != 0
        assert hashlib.md5(db.read_bytes()).hexdigest() == before
        assert not list(db.parent.glob("*.bak")) and not list(db.parent.glob("*.partial"))

    def test_backups_get_unique_names(self, tmp_path):
        export, db = self.ingest(tmp_path)
        for _ in range(2):
            assert cli("ingest", export, "--db", db).returncode == 0
        assert len(list(db.parent.glob("chat.sqlite.*.bak*"))) == 2

    def test_carry_from_adds_to_existing_db(self, tmp_path):
        export, db = self.ingest(tmp_path)
        con = sqlite3.connect(db)
        con.execute("update audios set transcript = 'current' where id = 1")
        con.commit()
        con.close()
        old = tmp_path / "old.sqlite"  # older schema: no translation/error/ocr_error columns
        con = sqlite3.connect(old)
        con.executescript("create table audios(id integer primary key, file text, transcript text);"
                          "create table messages(id integer primary key, media_file text, ocr_text text);")
        con.executemany("insert into audios(file, transcript) values(?, ?)",
                        [(IOS_FILES[0], "stale"), (IOS_FILES[1], "from old db")])
        con.execute("insert into messages(media_file, ocr_text) values(?, 'SALE 50%')", (IOS_FILES[2],))
        con.commit()
        con.close()
        r = cli("ingest", export, "--db", db, "--carry-from", old)
        assert r.returncode == 0, r.stderr
        assert rows(db, "select transcript from audios order by id") == [("current",), ("from old db",)]
        assert rows(db, "select ocr_text from messages where kind = 'image'") == [("SALE 50%",)]

    def test_missing_source_is_not_mistaken_for_a_transfer(self, tmp_path):
        r = cli("ingest", tmp_path / "nope.zip", "--db", tmp_path / "chat.sqlite")
        assert r.returncode != 0 and "does not exist" in r.stderr

    def test_zip_source(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES)
        z = shutil.make_archive(str(tmp_path / "WhatsApp Chat - Ana"), "zip", export)
        db = tmp_path / "out" / "chat.sqlite"
        r = cli("ingest", z, "--db", db)
        assert r.returncode == 0, r.stderr
        assert rows(db, "select count(*) from media where path is not null") == [(3,)]

    def test_lock_blocks_a_second_run(self, tmp_path):
        _, db = self.ingest(tmp_path)
        with open(f"{db}.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write("4242")
            fh.flush()
            r = cli("index", "--db", db)
            assert r.returncode != 0 and "4242" in r.stderr
            out = cli("status", "--db", db).stdout
            assert "RUNNING" in out and "pid 4242" in out


def transcribe_args(db, **kw):
    base = dict(db=str(db), backend="mlx", model=None, lang="es", translate=False,
                retry_empty=False, retry_errors=False, limit=None, detach=False)
    return argparse.Namespace(**{**base, **kw})


class TranscribeHarness:
    """A DB with 12 voice notes, and an in-process transcribe run with a fake backend."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        n = 12
        lines = [f"[21/04/2025, 11:{i:02d}:00] Bob: <attached: {i:08d}-AUDIO.opus>" for i in range(n)]
        export = make_export(tmp_path / "export", lines, [f"{i:08d}-AUDIO.opus" for i in range(n)])
        db = tmp_path / "chat.sqlite"
        assert cli("ingest", export, "--db", db).returncode == 0
        monkeypatch.setattr(wa2db, "check_backend", lambda *a: None)
        return db

    def run(self, monkeypatch, db, fn, **kw):
        monkeypatch.setattr(wa2db, "load_backend", lambda *a: fn)
        with pytest.raises(SystemExit) as e:
            wa2db.cmd_transcribe(transcribe_args(db, **kw))
        # Each in-process run leaves its lock fd open; drop it so the next run can lock.
        os.rename(f"{db}.lock", f"{db}.lock.{os.urandom(4).hex()}")
        return e.value.code


class TestTranscribe(TranscribeHarness):
    def test_one_bad_clip_is_recorded_and_the_run_continues(self, db, monkeypatch):
        def fake(path):
            if path.endswith("00000003-AUDIO.opus"):
                raise RuntimeError("corrupt opus")
            return " hola "
        assert self.run(monkeypatch, db, fake) == 1
        assert rows(db, "select count(*) from audios where transcript = 'hola'") == [(11,)]
        assert rows(db, "select error from audios where error is not null") == [("RuntimeError: corrupt opus",)]
        assert self.run(monkeypatch, db, lambda p: "ya está", retry_errors=True) == 0
        assert rows(db, "select count(*) from audios where error is not null") == [(0,)]

    def test_repeated_failures_stop_the_run(self, db, monkeypatch):
        def broken(path):
            raise FileNotFoundError("ffmpeg")
        code = self.run(monkeypatch, db, broken)
        assert "in a row" in str(code)
        assert rows(db, "select count(*) from audios where error is not null") == [(wa2db.MAX_STREAK,)]

    def test_moved_export_dies_without_marking_anything(self, db, monkeypatch, tmp_path):
        os.rename(tmp_path / "export", tmp_path / "moved")
        code = self.run(monkeypatch, db, lambda p: "x")
        assert "not on disk" in str(code)
        assert rows(db, "select count(*) from audios where error is not null") == [(0,)]

    def test_translate_has_its_own_error_column(self, db, monkeypatch):
        def fail(path):
            raise RuntimeError("nope")
        self.run(monkeypatch, db, fail, translate=True, limit=1)
        assert rows(db, "select error, translation_error from audios where id = 1") == [(None, "RuntimeError: nope")]

    def test_turbo_cannot_translate(self, db):
        with pytest.raises(SystemExit):
            wa2db.cmd_transcribe(transcribe_args(db, translate=True, model="mlx-community/whisper-large-v3-turbo"))


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
class TestOcr:
    def test_unreadable_image_is_a_failure_not_empty_text(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES)
        db = tmp_path / "chat.sqlite"
        assert cli("ingest", export, "--db", db).returncode == 0
        r = cli("ocr", "--db", db)
        assert r.returncode == 1
        [(text, err)] = rows(db, "select ocr_text, ocr_error from messages where kind = 'image'")
        assert text is None and err

    def test_missing_language_data_is_refused(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES)
        db = tmp_path / "chat.sqlite"
        assert cli("ingest", export, "--db", db).returncode == 0
        r = cli("ocr", "--db", db, "--lang", "zzz")
        assert r.returncode != 0 and "language data missing" in r.stderr


def ingested(tmp_path, lines=IOS_CHAT, files=IOS_FILES, name="_chat.txt"):
    export = make_export(tmp_path / "export", lines, files, name)
    db = tmp_path / "out" / "chat.sqlite"
    r = cli("ingest", export, "--db", db)
    assert r.returncode == 0, r.stderr
    return export, db, r


def md5(path):
    return hashlib.md5(path.read_bytes()).hexdigest()


class TestIngestSafety:
    def test_shorter_export_is_refused_unless_allowed(self, tmp_path):
        _, db, _ = ingested(tmp_path)
        before = md5(db)
        short = make_export(tmp_path / "short", IOS_CHAT[3:], IOS_FILES[1:])
        r = cli("ingest", short, "--db", db)
        assert r.returncode != 0 and "of its messages aren't in this export" in r.stderr
        assert md5(db) == before and not list(db.parent.glob("*.bak"))
        r = cli("ingest", short, "--db", db, "--allow-drop")
        assert r.returncode == 0, r.stderr
        assert rows(db, "select count(*) from messages") == [(2,)]
        assert len(list(db.parent.glob("*.bak"))) == 1

    def test_failed_build_leaves_db_and_no_partial_files(self, tmp_path, monkeypatch):
        export, db, _ = ingested(tmp_path)
        before = md5(db)
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("boom mid-build")
            return classify(*a, **kw)
        monkeypatch.setattr(wa2db, "classify", flaky)
        monkeypatch.setattr(signal, "signal", lambda *a: None)
        args = argparse.Namespace(db=str(db), source=str(export), date_order="auto", carry_from=None,
                                  chat=None, allow_drop=False)
        with pytest.raises(RuntimeError):
            wa2db.cmd_ingest(args)
        assert md5(db) == before
        assert not [p for p in db.parent.iterdir() if p.suffix in (".partial", ".bak") or "journal" in p.name]

    def test_failed_zip_ingest_removes_its_extraction(self, tmp_path):
        bad = make_export(tmp_path / "bad", ["not a whatsapp export"])
        z = shutil.make_archive(str(tmp_path / "bad"), "zip", bad)
        out = tmp_path / "out"
        r = cli("ingest", z, "--db", out / "chat.sqlite")
        assert r.returncode != 0
        assert not list(out.glob("export-*"))

    def test_lost_transcript_is_reported(self, tmp_path):
        export, db, _ = ingested(tmp_path)
        con = sqlite3.connect(db)
        con.execute("update audios set transcript = 'hola' where id = 1")
        con.commit()
        con.close()
        lines = IOS_CHAT[:2] + ["[21/04/2025, 11:42:00] Bob: audio omitted"] + IOS_CHAT[3:]
        (export / "_chat.txt").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        r = cli("ingest", export, "--db", db)
        assert r.returncode != 0 and "1 transcripts wouldn't carry over" in r.stderr
        r = cli("ingest", export, "--db", db, "--allow-drop")
        assert r.returncode == 0, r.stderr
        assert "1 earlier transcripts weren't applied" in r.stdout and ".bak" in r.stdout

    def test_macos_zip_junk_is_ignored(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES)
        (export / "__MACOSX").mkdir()
        for f in IOS_FILES:
            (export / "__MACOSX" / f"._{f}").write_bytes(b"x")
            (export / f"._{f}").write_bytes(b"x")
        r = cli("ingest", export, "--db", tmp_path / "chat.sqlite")
        assert r.returncode == 0, r.stderr
        assert "aren't referenced" not in r.stdout

    def test_bare_chat_txt_source(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT)
        db = tmp_path / "out" / "chat.sqlite"
        r = cli("ingest", export / "_chat.txt", "--db", db)
        assert r.returncode == 0, r.stderr
        assert rows(db, "select count(*) from audios where error = 'not in export'") == [(2,)]


class TestChatLog:
    def test_android_named_log(self, tmp_path):
        _, db, _ = ingested(tmp_path, name="WhatsApp Chat with Ana.txt")
        assert rows(db, "select count(*) from messages") == [(5,)]

    def test_forwarded_chat_inside_an_ios_export(self, tmp_path):
        fwd = "00000004-WhatsApp Chat - Bob.txt"
        export = make_export(tmp_path / "export", IOS_CHAT + [f"[21/04/2025, 11:45:00] Ana: <attached: {fwd}>"], IOS_FILES)
        (export / fwd).write_text("[01/01/2024, 10:00:00] Bob: other chat\n", encoding="utf-8")
        r = cli("ingest", export, "--db", tmp_path / "chat.sqlite")
        assert r.returncode == 0, r.stderr
        assert rows(tmp_path / "chat.sqlite", "select value from meta where key = 'chat_file'")[0][0].endswith("/_chat.txt")

    def test_forwarded_chat_inside_an_android_export(self, tmp_path):
        lines = ["13/01/2025, 21:05 - Ana: hi", "13/01/2025, 21:06 - Ana: WhatsApp Chat with Bob.txt (file attached)"]
        export = make_export(tmp_path / "export", lines, name="WhatsApp Chat with Ana.txt")
        (export / "WhatsApp Chat with Bob.txt").write_text("01/01/2024, 10:00 - Bob: other chat\n", encoding="utf-8")
        r = cli("ingest", export, "--db", tmp_path / "chat.sqlite")
        assert r.returncode == 0, r.stderr
        assert rows(tmp_path / "chat.sqlite", "select count(*) from messages") == [(2,)]

    def test_ambiguous_logs_need_chat_flag(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, name="WhatsApp Chat with Ana.txt")
        (export / "WhatsApp Chat with Bob.txt").write_text("[01/01/2024, 10:00:00] Bob: other\n", encoding="utf-8")
        r = cli("ingest", export, "--db", tmp_path / "chat.sqlite")
        assert r.returncode != 0 and "--chat" in r.stderr
        r = cli("ingest", export, "--db", tmp_path / "chat.sqlite", "--chat", "WhatsApp Chat with Ana.txt")
        assert r.returncode == 0, r.stderr


class TestLocks:
    @pytest.mark.parametrize("cmd", [["ingest", "{export}"], ["ocr"], ["index"]])
    def test_locked_db_is_refused(self, tmp_path, cmd):
        if cmd == ["ocr"] and not shutil.which("tesseract"):
            pytest.skip("tesseract not installed")
        export, db, _ = ingested(tmp_path)
        with open(f"{db}.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write("4242")
            fh.flush()
            r = cli(*[c.format(export=export) for c in cmd], "--db", db)
        assert r.returncode != 0 and "pid 4242" in r.stderr

    def test_transcribe_respects_the_lock(self, tmp_path, monkeypatch):
        _, db, _ = ingested(tmp_path)
        monkeypatch.setattr(wa2db, "check_backend", lambda *a: None)
        with open(f"{db}.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write("4242")
            fh.flush()
            with pytest.raises(SystemExit) as e:
                wa2db.cmd_transcribe(transcribe_args(db))
        assert "pid 4242" in str(e.value.code)


class TestTranscribeRetries(TranscribeHarness):
    def test_errors_keep_the_cause_not_the_banner(self):
        e = RuntimeError("Failed to load audio: ffmpeg version 9.0.1 Copyright\n  configuration: --prefix=/x\n"
                         "x.opus: Invalid data found when processing input\n")
        assert wa2db.describe(e) == "RuntimeError: x.opus: Invalid data found when processing input"

    def test_a_success_resets_the_failure_streak(self, db, monkeypatch):
        # 11 failures in total, but never 10 in a row: clip 9 succeeds between them.
        def all_but_nine(path):
            if int(os.path.basename(path)[:8]) != 9:
                raise RuntimeError("bad clip")
            return "ok"
        assert self.run(monkeypatch, db, all_but_nine) == 1
        assert rows(db, "select count(*) from audios where error is not null") == [(11,)]
        assert rows(db, "select value from meta where key = 'last_run:transcribe'")[0][0].startswith("transcribe finished")

    def test_known_bad_clips_are_retried_last_and_dont_stop_the_run(self, db, monkeypatch, capsys):
        bad = {f"{i:08d}-AUDIO.opus" for i in range(10)}

        def fails_on_bad(path):
            if os.path.basename(path) in bad:
                raise RuntimeError("corrupt")
            return "ok"
        assert "in a row" in str(self.run(monkeypatch, db, fails_on_bad))
        seen = []

        def spy(path):
            seen.append(os.path.basename(path))
            return fails_on_bad(path)
        assert self.run(monkeypatch, db, spy, retry_errors=True) == 1
        assert seen[:2] == ["00000010-AUDIO.opus", "00000011-AUDIO.opus"] and len(seen) == 12
        assert "leave them" in capsys.readouterr().out
        assert rows(db, "select count(*) from audios where transcript = 'ok'") == [(2,)]

    def test_clips_not_in_export_are_never_attempted(self, tmp_path, monkeypatch):
        export = make_export(tmp_path / "ex2", IOS_CHAT, IOS_FILES[:1] + IOS_FILES[2:])
        db = tmp_path / "db2.sqlite"
        assert cli("ingest", export, "--db", db).returncode == 0
        monkeypatch.setattr(wa2db, "check_backend", lambda *a: None)
        seen = []
        assert self.run(monkeypatch, db, lambda p: seen.append(p) or "ok", retry_errors=True, retry_empty=True) == 0
        assert [os.path.basename(p) for p in seen] == [IOS_FILES[0]]


@pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
class TestOcrMore:
    def db(self, tmp_path, image):
        lines = IOS_CHAT[:2] + [f"[21/04/2025, 11:44:00] Ana: <attached: {image}>"]
        _, db, _ = ingested(tmp_path, lines, [image])
        return db

    def test_heic_is_marked_unsupported_without_sips(self, tmp_path):
        db = self.db(tmp_path, "00000003-PHOTO.heic")
        no_sips = {**os.environ, "PATH": os.path.dirname(shutil.which("tesseract"))}
        r = subprocess.run([sys.executable, SCRIPT, "ocr", "--db", str(db)], capture_output=True, text=True, env=no_sips)
        assert r.returncode == 0, r.stderr
        assert rows(db, "select ocr_error from messages where kind = 'image'") == [
            ("unsupported format: heic (needs sips to convert)",)]
        r = subprocess.run([sys.executable, SCRIPT, "ocr", "--db", str(db), "--retry-errors"],
                           capture_output=True, text=True, env=no_sips)
        assert "0 images to OCR" in r.stdout
        assert "unsupported 1" in cli("status", "--db", db).stdout

    def test_failure_message_is_not_the_generic_line(self, tmp_path):
        db = self.db(tmp_path, "00000003-PHOTO.jpg")
        assert cli("ocr", "--db", db).returncode == 1
        [(err,)] = rows(db, "select ocr_error from messages where kind = 'image'")
        assert err != "RuntimeError: Error during processing."

    def test_redo_blank(self, tmp_path):
        db = self.db(tmp_path, "00000003-PHOTO.jpg")
        con = sqlite3.connect(db)
        con.execute("update messages set ocr_text = '' where kind = 'image'")
        con.commit()
        con.close()
        assert "0 images to OCR" in cli("ocr", "--db", db).stdout
        assert "1 images to OCR" in cli("ocr", "--db", db, "--redo-blank").stdout


FAKE_MLX = '''
import os, time
class Crash(BaseException):
    pass
def transcribe(path, **kw):
    mode = os.environ.get("FAKE_MLX", "ok")
    if mode == "crash":
        raise Crash("simulated native crash")
    if mode == "hang":
        time.sleep(120)
    return {"text": "hola"}
'''


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="the mlx backend check needs ffmpeg on PATH")
class TestDetach:
    @pytest.fixture
    def env(self, tmp_path):
        pkg = tmp_path / "fake" / "mlx_whisper"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(FAKE_MLX)
        (pkg / "tokenizer.py").write_text("LANGUAGES = {'es': 'spanish'}\nTO_LANGUAGE_CODE = {'spanish': 'es'}\n")
        return {**os.environ, "PYTHONPATH": str(tmp_path / "fake"), "FORCE_COLOR": "3"}

    def launch(self, db, env, mode, *extra):
        return subprocess.run([sys.executable, SCRIPT, "transcribe", "--db", str(db), "--lang", "es", *extra],
                              capture_output=True, text=True, env={**env, "FAKE_MLX": mode}, timeout=60)

    def wait_for(self, cond, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.2)
        raise AssertionError("timed out")

    def test_wrong_language_code_is_refused_before_forking(self, tmp_path, env):
        _, db, _ = ingested(tmp_path)
        r = self.launch(db, env, "ok", "--detach")
        assert r.returncode == 0, r.stderr
        self.wait_for(lambda: not wa2db.running(str(db)))
        r = subprocess.run([sys.executable, SCRIPT, "transcribe", "--db", str(db), "--lang", "spa", "--detach"],
                           capture_output=True, text=True, env=env)
        assert r.returncode != 0 and "isn't a Whisper language" in r.stderr

    def test_crash_in_detached_run_is_visible(self, tmp_path, env):
        _, db, _ = ingested(tmp_path)
        r = self.launch(db, env, "crash", "--detach")
        assert r.returncode == 0 and "detached as pid" in r.stdout
        logfile = db.parent / "transcribe.log"
        self.wait_for(lambda: "ABORTED" in logfile.read_text() and not wa2db.running(str(db)))
        assert "\x1b[" not in logfile.read_text()
        assert "aborted" in cli("status", "--db", db).stdout

    def test_uppercase_language_code_is_normalised(self, tmp_path, env):
        _, db, _ = ingested(tmp_path)
        r = subprocess.run([sys.executable, SCRIPT, "transcribe", "--db", str(db), "--lang", "ES"],
                           capture_output=True, text=True, env={**env, "FAKE_MLX": "ok"})
        assert "lang=es" in r.stdout

    def test_sigterm_is_recorded(self, tmp_path, env):
        _, db, _ = ingested(tmp_path)
        assert self.launch(db, env, "hang", "--detach").returncode == 0
        self.wait_for(lambda: "start pid" in (db.parent / "transcribe.log").read_text())
        os.kill(int(wa2db.running(str(db))[0]), signal.SIGTERM)
        self.wait_for(lambda: not wa2db.running(str(db)))
        out = cli("status", "--db", db).stdout
        assert "stopped by SIGTERM" in out and "ended without finishing" not in out

    def test_unwritable_db_fails_in_the_launching_shell(self, tmp_path, env):
        _, db, _ = ingested(tmp_path)
        os.chmod(db, 0o444)
        try:
            r = self.launch(db, env, "ok", "--detach")
        finally:
            os.chmod(db, 0o644)
        assert r.returncode != 0 and "can't record the run" in r.stderr and "detached" not in r.stdout

    def test_unopenable_log_fails_before_anything_is_recorded(self, tmp_path, env):
        _, db, _ = ingested(tmp_path)
        (db.parent / "transcribe.log").mkdir()
        r = self.launch(db, env, "ok", "--detach")
        assert r.returncode != 0 and "detached" not in r.stdout
        out = cli("status", "--db", db).stdout
        assert "ended without finishing" not in out and "last run" not in out

    def test_killed_detached_run_is_reported(self, tmp_path, env):
        _, db, _ = ingested(tmp_path)
        assert self.launch(db, env, "hang", "--detach").returncode == 0
        self.wait_for(lambda: wa2db.running(str(db)))
        self.wait_for(lambda: "start pid" in (db.parent / "transcribe.log").read_text())
        os.kill(int(wa2db.running(str(db))[0]), signal.SIGKILL)
        self.wait_for(lambda: not wa2db.running(str(db)))
        assert "ended without finishing" in cli("status", "--db", db).stdout


class TestIngestSafetyMore:
    def test_contact_rename_is_not_a_drop(self, tmp_path):
        export, db, _ = ingested(tmp_path)
        renamed = [l.replace("] Ana:", "] Ana \u2764\ufe0f:") for l in IOS_CHAT]
        (export / "_chat.txt").write_text("\r\n".join(renamed) + "\r\n", encoding="utf-8")
        r = cli("ingest", export, "--db", db)
        assert r.returncode == 0, r.stderr

    def test_uncomparable_db_is_not_replaced(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES)
        db = tmp_path / "chat.sqlite"
        con = sqlite3.connect(db)  # an older ad-hoc DB: carry-over works, but no ts to compare
        con.executescript("create table audios(file text, transcript text); create table messages(media_file text);")
        con.close()
        before = md5(db)
        r = cli("ingest", export, "--db", db)
        assert r.returncode != 0 and "can't be compared" in r.stderr and md5(db) == before
        assert cli("ingest", export, "--db", db, "--allow-drop").returncode == 0

    def test_losing_transcripts_needs_allow_drop(self, tmp_path):
        export, db, _ = ingested(tmp_path)
        con = sqlite3.connect(db)
        con.execute("update audios set transcript = 'hola' where id = 1")
        con.commit()
        con.close()
        # An export without media names no files: "audio omitted" instead of "<attached: ...>".
        omitted = [l.split("\u200e<attached")[0] + "audio omitted" if "AUDIO" in l else l for l in IOS_CHAT]
        textonly = make_export(tmp_path / "textonly", omitted)
        r = cli("ingest", textonly / "_chat.txt", "--db", db)
        assert r.returncode != 0 and "1 transcripts wouldn't carry over" in r.stderr
        assert rows(db, "select transcript from audios where id = 1") == [("hola",)]
        assert cli("ingest", textonly / "_chat.txt", "--db", db, "--allow-drop").returncode == 0

    def test_chat_txt_finds_media_beside_it(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES)
        db = tmp_path / "out" / "chat.sqlite"
        assert cli("ingest", export / "_chat.txt", "--db", db).returncode == 0
        assert rows(db, "select count(*) from media where path is not null") == [(3,)]

    def test_chat_flag_after_an_ambiguous_zip(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, name="WhatsApp Chat with Ana.txt")
        (export / "WhatsApp Chat with Bob.txt").write_text("[01/01/2024, 10:00:00] Bob: other\n", encoding="utf-8")
        z = shutil.make_archive(str(tmp_path / "chats"), "zip", export)
        db = tmp_path / "out" / "chat.sqlite"
        r = cli("ingest", z, "--db", db)
        assert r.returncode != 0 and "'WhatsApp Chat with Ana.txt'" in r.stderr
        assert not list(db.parent.glob("export-*"))
        r = cli("ingest", z, "--db", db, "--chat", "WhatsApp Chat with Ana.txt")
        assert r.returncode == 0, r.stderr

    def test_backup_falls_back_to_a_copy_without_hard_links(self, tmp_path, monkeypatch):
        export, db, _ = ingested(tmp_path)
        before = md5(db)

        def no_links(*a):
            raise OSError("hard links not supported")
        monkeypatch.setattr(os, "link", no_links)
        monkeypatch.setattr(signal, "signal", lambda *a: None)
        wa2db.cmd_ingest(argparse.Namespace(db=str(db), source=str(export), date_order="auto", carry_from=None,
                                            chat=None, allow_drop=False))
        [bak] = db.parent.glob("*.bak")
        assert md5(bak) == before and not list(db.parent.glob("*.tmp"))

    def test_extraction_dirs_are_never_shared(self, tmp_path):
        assert wa2db.make_dir(str(tmp_path / "x")) != wa2db.make_dir(str(tmp_path / "x"))


def png(path):
    """Write a real 1x1 white PNG."""
    import struct, zlib
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"\x00\xff")) + chunk(b"IEND", b""))


class TestOcrSafetyChecks:
    pytestmark = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")

    def db(self, tmp_path, image):
        lines = IOS_CHAT[:2] + [f"[21/04/2025, 11:44:00] Ana: <attached: {image}>"]
        _, db, _ = ingested(tmp_path, lines, [image])
        return db

    def test_a_path_list_disguised_as_an_image_never_reaches_tesseract(self, tmp_path):
        secret = tmp_path / "secret.png"
        png(secret)
        db = self.db(tmp_path, "00000003-PHOTO.png")
        (tmp_path / "export" / "00000003-PHOTO.png").write_text(f"{secret}\n")
        assert cli("ocr", "--db", db).returncode == 1
        [(text, err)] = rows(db, "select ocr_text, ocr_error from messages where kind = 'image'")
        assert text is None and err.startswith("Rejected: not a recognised image")

    def test_a_real_image_is_ocrd(self, tmp_path):
        db = self.db(tmp_path, "00000003-PHOTO.png")
        png(tmp_path / "export" / "00000003-PHOTO.png")
        assert cli("ocr", "--db", db).returncode == 0
        assert rows(db, "select ocr_text, ocr_error from messages where kind = 'image'") == [("", None)]

    @pytest.mark.skipif(not shutil.which("sips"), reason="sips (macOS) not available")
    def test_heic_is_converted_with_sips(self, tmp_path):
        db = self.db(tmp_path, "00000003-PHOTO.heic")
        png(tmp_path / "src.png")
        heic = tmp_path / "export" / "00000003-PHOTO.heic"
        subprocess.run(["sips", "-s", "format", "heic", str(tmp_path / "src.png"), "--out", str(heic)],
                       capture_output=True, check=True)
        assert cli("ocr", "--db", db).returncode == 0
        assert rows(db, "select ocr_text, ocr_error from messages where kind = 'image'") == [("", None)]

    def test_redo_blank_changes_nothing_when_the_export_moved(self, tmp_path):
        db = self.db(tmp_path, "00000003-PHOTO.png")
        con = sqlite3.connect(db)
        con.execute("update messages set ocr_text = '' where kind = 'image'")
        con.commit()
        con.close()
        os.rename(tmp_path / "export", tmp_path / "moved")
        r = cli("ocr", "--db", db, "--redo-blank")
        assert r.returncode != 0 and "not on disk" in r.stderr
        assert rows(db, "select ocr_text from messages where kind = 'image'") == [("",)]



class TestRoundFour:
    """Regressions from the fourth review round."""

    def ocr_db(self, tmp_path, image):
        lines = IOS_CHAT[:2] + [f"[21/04/2025, 11:44:00] Ana: <attached: {image}>"]
        _, db, _ = ingested(tmp_path, lines, [image])
        return db, tmp_path / "export" / image

    @pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
    @pytest.mark.parametrize("prefix", [b"\x89PNG", b"GIF8", b"\x89PNG\r\n\x1a\n", b"GIF89a", b"\xff\xd8\xff"])
    def test_disguised_path_lists_are_never_read_as_lists(self, tmp_path, prefix):
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        png(cwd / "a.png")  # what list mode would open, relative to the current directory
        secret = tmp_path / "secret.png"
        png(secret)
        db, image = self.ocr_db(tmp_path, "00000003-PHOTO.png")
        image.write_bytes(prefix + b"/x/a.png\n" + str(secret).encode() + b"\n")
        r = cli("ocr", "--db", db, cwd=cwd)
        [(text, err)] = rows(db, "select ocr_text, ocr_error from messages where kind = 'image'")
        assert text is None and err, (r.stdout, r.stderr)

    @pytest.mark.parametrize("head", [b"\x89PNG/x/a.png\n", b"GIF8/x/a.png\n", b"II*\x00/x/a.png\n",
                                      b"BM/x/a.png\n", b"\xff\xd8\xff", b""])
    def test_layer1_only_exact_signatures_count_as_images(self, tmp_path, head):
        f = tmp_path / "x.png"
        f.write_bytes(head)
        assert wa2db.image_type(str(f)) is None

    def test_layer1_real_signatures_are_recognised(self, tmp_path):
        f = tmp_path / "x.png"
        png(f)
        assert wa2db.image_type(str(f)) == "png"

    @pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
    def test_layer2_tesseract_runs_where_list_entries_cant_resolve(self, tmp_path, monkeypatch):
        png(tmp_path / "a.png")
        (tmp_path / "list.txt").write_text("a.png\n")
        monkeypatch.chdir(tmp_path)  # list mode here would find a.png
        with pytest.raises(RuntimeError):
            wa2db.tesseract(str(tmp_path / "list.txt"), "eng")

    def test_bare_txt_only_links_whatsapp_named_files_beside_it(self, tmp_path):
        folder = tmp_path / "Downloads"
        make_export(folder, IOS_CHAT + ["[21/04/2025, 11:45:00] Eve: look <attached: secret.png>"], IOS_FILES)
        png(folder / "secret.png")
        db = tmp_path / "out" / "chat.sqlite"
        assert cli("ingest", folder / "_chat.txt", "--db", db).returncode == 0
        assert rows(db, "select file, path is not null from media order by id") == [
            (IOS_FILES[0], 1), (IOS_FILES[1], 1), (IOS_FILES[2], 1), ("secret.png", 0)]

    def test_one_refusal_lists_everything_allow_drop_would_discard(self, tmp_path):
        export, db, _ = ingested(tmp_path)
        con = sqlite3.connect(db)
        con.execute("update audios set transcript = 'hola' where id = 1")
        con.commit()
        con.close()
        capped = [l.split("\u200e<attached")[0] + "audio omitted" if "AUDIO" in l else l for l in IOS_CHAT[:4]]
        r = cli("ingest", make_export(tmp_path / "capped", capped) / "_chat.txt", "--db", db)
        assert r.returncode != 0
        assert "of its messages aren't in this export" in r.stderr and "transcripts wouldn't carry over" in r.stderr

    def test_correcting_the_day_month_order_is_not_a_drop(self, tmp_path):
        lines = ["[03/04/2025, 10:00:00] Ana: a", "[05/06/2025, 10:00:00] Bob: b"]
        export, db, r = ingested(tmp_path, lines, [])
        assert "assuming dd/mm" in r.stdout
        r = cli("ingest", export, "--db", db)
        assert r.returncode == 0 and "as the current DB does (a guess there too" in r.stdout
        r = cli("ingest", export, "--db", db, "--date-order", "mdy")
        assert r.returncode == 0, r.stderr
        assert rows(db, "select date from messages order by id") == [("2025-03-04",), ("2025-05-06",)]

    def test_chat_name_is_looked_up_inside_the_export_first(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES)
        cwd = make_export(tmp_path / "older", IOS_CHAT[:2])
        db = tmp_path / "out" / "chat.sqlite"
        r = cli("ingest", export, "--db", db, "--chat", "_chat.txt", cwd=cwd)
        assert r.returncode == 0, r.stderr
        assert rows(db, "select count(*) from messages") == [(5,)]
        assert rows(db, "select value from meta where key = 'chat_file'")[0][0] == os.path.realpath(export / "_chat.txt")

    def test_unreadable_current_db_can_be_replaced_with_allow_drop(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES)
        db = tmp_path / "chat.sqlite"
        db.write_bytes(b"not a database at all, just bytes" * 10)
        r = cli("ingest", export, "--db", db)
        assert r.returncode != 0 and "can't be read for carry-over" in r.stderr
        assert cli("ingest", export, "--db", db, "--allow-drop").returncode == 0
        assert rows(db, "select count(*) from messages") == [(5,)]

    @pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
    def test_rejected_files_dont_stop_the_run(self, tmp_path):
        names = [f"{i:08d}-PHOTO.jpg" for i in range(12)]
        lines = [f"[21/04/2025, 11:{i:02d}:00] Ana: <attached: {n}>" for i, n in enumerate(names)]
        _, db, _ = ingested(tmp_path, lines, names)
        for n in names:
            (tmp_path / "export" / n).write_bytes(b"")
        r = cli("ocr", "--db", db)
        assert r.returncode == 1 and "in a row" not in r.stderr and "12 rejected" in r.stdout
        assert rows(db, "select distinct ocr_error from messages") == [("Rejected: empty file",)]

    @pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("sips")), reason="needs tesseract and sips")
    def test_heic_marked_unsupported_earlier_is_requeued(self, tmp_path):
        db, heic = self.ocr_db(tmp_path, "00000003-PHOTO.heic")
        png(tmp_path / "src.png")
        subprocess.run(["sips", "-s", "format", "heic", str(tmp_path / "src.png"), "--out", str(heic)],
                       capture_output=True, check=True)
        con = sqlite3.connect(db)
        con.execute("update messages set ocr_error = 'unsupported format: heic' where kind = 'image'")
        con.commit()
        con.close()
        r = cli("ocr", "--db", db)
        assert "re-queued 1 HEIC" in r.stdout and r.returncode == 0
        assert rows(db, "select ocr_text, ocr_error from messages where kind = 'image'") == [("", None)]

    @pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
    def test_a_trashed_known_bad_file_doesnt_block_ocr(self, tmp_path):
        lines = IOS_CHAT[:2] + [f"[21/04/2025, 11:4{i}:00] Ana: <attached: 0000000{i}-PHOTO.png>" for i in (3, 4)]
        _, db, _ = ingested(tmp_path, lines, ["00000003-PHOTO.png", "00000004-PHOTO.png"])
        png(tmp_path / "export" / "00000004-PHOTO.png")
        (tmp_path / "export" / "00000003-PHOTO.png").write_bytes(b"")
        assert cli("ocr", "--db", db).returncode == 1  # the empty one is rejected
        (tmp_path / "export" / "00000003-PHOTO.png").unlink()
        con = sqlite3.connect(db)
        con.execute("update messages set ocr_text = null, ocr_error = null where media_file = '00000004-PHOTO.png'")
        con.commit()
        con.close()
        r = cli("ocr", "--db", db)
        assert r.returncode == 0, r.stderr

    def test_status_names_the_command_holding_the_lock_and_flags_a_crashed_one(self, tmp_path):
        _, db, _ = ingested(tmp_path)
        con = sqlite3.connect(db)
        con.execute("insert into meta values('last_run:transcribe', 'transcribe started 2026-01-01 pid 4242')")
        con.execute("insert into meta values('last_run', 'transcribe started 2025-01-01 pid 1')")
        con.commit()
        con.close()
        with open(f"{db}.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write("5151 ocr")
            fh.flush()
            out = cli("status", "--db", db).stdout
        assert "RUNNING: ocr (pid 5151)" in out and "WARNING: a run ended without finishing (transcribe" in out
        assert "2025-01-01" not in out  # the old single-slot key is ignored

    def test_status_voice_note_counts_dont_overlap(self, tmp_path):
        export, db, _ = ingested(tmp_path, IOS_CHAT, IOS_FILES[:1] + IOS_FILES[2:])
        con = sqlite3.connect(db)
        con.execute("update audios set transcript = 'carried' where id = 2")
        con.commit()
        con.close()
        out = cli("status", "--db", db).stdout
        assert "transcribed 1," in out and "not in export 0" in out


class TestRoundFive:
    """Regressions from the fifth review round."""

    def test_a_changed_date_format_is_not_a_drop(self, tmp_path):
        export, db, _ = ingested(tmp_path, ["[13/04/2025, 10:00:00] Ana: a", "[14/04/2025, 10:00:00] Bob: b"], [])
        newer = ["[4/13/25, 10:00:00 AM] Ana: a", "[4/14/25, 10:00:00 AM] Bob: b", "[4/15/25, 9:00:00 AM] Ana: c"]
        (export / "_chat.txt").write_text("\r\n".join(newer) + "\r\n", encoding="utf-8")
        r = cli("ingest", export, "--db", db)
        assert r.returncode == 0, r.stderr
        assert "corrected" not in r.stdout
        assert rows(db, "select count(*) from messages") == [(3,)]

    def test_a_wrong_date_order_is_refused_not_folded_away(self, tmp_path):
        lines = [f"[{d:02d}/04/2025, 10:00:00] Ana: day {d}" for d in range(1, 29)]
        export, db, _ = ingested(tmp_path, lines, [])
        r = cli("ingest", export, "--db", db, "--date-order", "mdy")
        assert r.returncode != 0 and "16 of its messages aren't in this export" in r.stderr
        assert "corrected" not in r.stdout
        assert rows(db, "select count(*) from messages") == [(28,)]

    def test_impossible_timestamps_in_an_older_db_are_ignored(self, tmp_path):
        export, db, _ = ingested(tmp_path)
        con = sqlite3.connect(db)
        con.execute("insert into messages(ts, date, sender, kind, text) values('2025-13-04T10:00:00', '2025-13-04', 'Ana', 'text', 'x')")
        con.commit()
        con.close()
        assert cli("ingest", export, "--db", db).returncode == 0

    def test_bare_txt_reports_unlinked_files_beside_it(self, tmp_path):
        folder = tmp_path / "Downloads"
        make_export(folder, ["13/01/2025, 21:05 - Ana: Invoice March.pdf (file attached)"], ["Invoice March.pdf"])
        r = cli("ingest", folder / "_chat.txt", "--db", tmp_path / "out" / "chat.sqlite")
        assert r.returncode == 0, r.stderr
        assert "weren't linked because their names" in r.stdout and "absent from the export" not in r.stdout

    def test_bare_txt_in_a_shared_folder_has_no_orphan_warning(self, tmp_path):
        folder = tmp_path / "Downloads"
        make_export(folder, IOS_CHAT[:2], ["IMG-20250101-WA0001.jpg", "VID-20250101-WA0002.mp4"])
        r = cli("ingest", folder / "_chat.txt", "--db", tmp_path / "out" / "chat.sqlite")
        assert r.returncode == 0 and "aren't referenced" not in r.stdout

    def test_dated_file_names_are_not_whatsapp_media(self, tmp_path):
        folder = tmp_path / "Downloads"
        make_export(folder, IOS_CHAT[:2] + ["[21/04/2025, 11:45:00] Eve: <attached: 20240315-bank.png>"],
                    ["20240315-bank.png"])
        db = tmp_path / "out" / "chat.sqlite"
        assert cli("ingest", folder / "_chat.txt", "--db", db).returncode == 0
        assert rows(db, "select path from media") == [(None,)]

    def test_symlinks_in_a_folder_export_are_not_followed(self, tmp_path):
        export = make_export(tmp_path / "export", IOS_CHAT, IOS_FILES[:2])
        png(tmp_path / "elsewhere.png")
        os.symlink(tmp_path / "elsewhere.png", export / IOS_FILES[2])
        db = tmp_path / "out" / "chat.sqlite"
        assert cli("ingest", export, "--db", db).returncode == 0
        assert rows(db, "select path from media where file = ?", IOS_FILES[2]) == [(None,)]

    @pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
    def test_rejections_share_one_error_and_all_rejected_looks_systemic(self, tmp_path):
        names = [f"{i:08d}-PHOTO.jpg" for i in range(12)]
        lines = [f"[21/04/2025, 11:{i:02d}:00] Ana: <attached: {n}>" for i, n in enumerate(names)]
        _, db, _ = ingested(tmp_path, lines, names)
        for i, n in enumerate(names):
            (tmp_path / "export" / n).write_bytes(f"<html>placeholder {i}</html>".encode())
        r = cli("ocr", "--db", db)
        assert r.returncode == 1 and "looks systemic" in r.stdout and "starts with 3c 68 74 6d" in r.stdout
        assert rows(db, "select count(distinct ocr_error) from messages where kind = 'image'") == [(1,)]
        assert "ocr failure x12" in cli("status", "--db", db).stdout

    @pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("sips")), reason="needs tesseract and sips")
    def test_sips_error_keeps_the_specific_cause(self, tmp_path):
        lines = IOS_CHAT[:2] + ["[21/04/2025, 11:44:00] Ana: <attached: 00000003-PHOTO.heic>"]
        _, db, _ = ingested(tmp_path, lines, ["00000003-PHOTO.heic"])
        (tmp_path / "export" / "00000003-PHOTO.heic").write_bytes(b"\x00\x00\x00\x18ftypheic" + b"junk" * 50)
        assert cli("ocr", "--db", db).returncode == 1
        [(err,)] = rows(db, "select ocr_error from messages where kind = 'image'")
        assert err.startswith("RuntimeError: sips couldn't convert") and ";" in err

    def test_status_blank_and_failed_dont_overlap(self, tmp_path):
        _, db, _ = ingested(tmp_path)
        con = sqlite3.connect(db)
        con.execute("update audios set transcript = '', error = 'RuntimeError: x' where id = 1")
        con.commit()
        con.close()
        out = cli("status", "--db", db).stdout
        assert "blank 0," in out and "failed 1," in out

    def test_superseded_extractions_are_listed_while_a_transcribe_runs(self, tmp_path):
        _, db, _ = ingested(tmp_path)
        (db.parent / "export-20200101-000000").mkdir()
        with open(f"{db}.lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write("4242 transcribe")
            fh.flush()
            out = cli("status", "--db", db).stdout
        assert "superseded extractions" in out and "export-20200101-000000" in out
