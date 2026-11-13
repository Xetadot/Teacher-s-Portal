"""
Teacher Portal - an all-in-one web app for lecturers.
Backend: Python (Flask + SQLite)   Frontend: HTML/CSS/JS
AI: Google Gemini - inquiry summaries + replies, daily briefing, at-risk insights,
    meeting finder, extension advisor, natural-language events, task suggestions, chat assistant

Run:
    pip install flask
    python app.py
Then open http://127.0.0.1:5000
Demo login: lecturer@apu.edu.my / password123
"""
import os
import json
import re
import secrets
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (Flask, g, jsonify, redirect, render_template, request,
                   session, url_for, abort)
# All pages (lecturer portal, login, student pages) live in templates/portal.html
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "portal.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

# Put your key here, OR set the GEMINI_API_KEY environment variable,
# OR paste it on the Settings page inside the app.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# ----------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


SCHEMA = """
CREATE TABLE IF NOT EXISTS lecturers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    gemini_key TEXT DEFAULT '',
    inquiry_token TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecturer_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    type TEXT DEFAULT 'class',          -- class / appointment / meeting / other
    date TEXT NOT NULL,                 -- YYYY-MM-DD
    start_time TEXT NOT NULL,           -- HH:MM
    end_time TEXT NOT NULL,
    location TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    student_name TEXT DEFAULT '',
    student_email TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS booking_links(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecturer_id INTEGER NOT NULL,
    token TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    duration INTEGER DEFAULT 30,        -- minutes per slot
    days TEXT DEFAULT '0,1,2,3,4',      -- 0=Mon ... 6=Sun
    start_hour TEXT DEFAULT '09:00',
    end_hour TEXT DEFAULT '17:00',
    location TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS courses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecturer_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS students(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,
    student_id TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attendance(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,
    student_row INTEGER NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL,               -- present / late / absent
    UNIQUE(student_row, date)
);
CREATE TABLE IF NOT EXISTS inquiries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecturer_id INTEGER NOT NULL,
    student_name TEXT NOT NULL,
    student_id TEXT DEFAULT '',
    student_email TEXT DEFAULT '',
    module TEXT DEFAULT '',
    subject TEXT NOT NULL,
    message TEXT NOT NULL,
    summary TEXT DEFAULT '',
    category TEXT DEFAULT 'General',
    priority TEXT DEFAULT 'Normal',
    suggested_reply TEXT DEFAULT '',
    reply TEXT DEFAULT '',
    status TEXT DEFAULT 'new',          -- new / read / replied
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lecturer_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    due TEXT DEFAULT '',
    done INTEGER DEFAULT 0
);
"""


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    if db.execute("SELECT COUNT(*) FROM lecturers").fetchone()[0] == 0:
        seed_demo(db)
    db.commit()
    db.close()


def seed_demo(db):
    cur = db.execute(
        "INSERT INTO lecturers(name,email,password,inquiry_token) VALUES(?,?,?,?)",
        ("Dr. Sarah Lim", "lecturer@apu.edu.my",
         generate_password_hash("password123"), secrets.token_urlsafe(8)))
    lid = cur.lastrowid
    today = date.today()

    def d(offset):
        return (today + timedelta(days=offset)).isoformat()

    events = [
        ("Java Programming - Lecture", "class", d(0), "09:00", "11:00", "Room B-07-3"),
        ("Consultation with Ali", "appointment", d(0), "14:00", "14:30", "Office L4-12"),
        ("Web Design - Lab", "class", d(1), "10:00", "12:00", "Lab 4"),
        ("Faculty Meeting", "meeting", d(2), "15:00", "16:00", "Meeting Room 2"),
        ("Java Programming - Tutorial", "class", d(3), "13:00", "15:00", "Room B-05-1"),
    ]
    for t, ty, dt, s, e, loc in events:
        db.execute("INSERT INTO events(lecturer_id,title,type,date,start_time,end_time,location)"
                   " VALUES(?,?,?,?,?,?,?)", (lid, t, ty, dt, s, e, loc))

    c1 = db.execute("INSERT INTO courses(lecturer_id,code,name) VALUES(?,?,?)",
                    (lid, "AAPP013-4-2", "Java Programming")).lastrowid
    c2 = db.execute("INSERT INTO courses(lecturer_id,code,name) VALUES(?,?,?)",
                    (lid, "AAPP012-4-2", "Responsive Web Design")).lastrowid
    names = ["Ahmad Faiz", "Tan Wei Ling", "Priya Nair", "Lim Jun Hao",
             "Nur Aisyah", "Wong Kai Xin", "Daniel Lee", "Siti Hajar"]
    import random
    random.seed(7)
    for cid in (c1, c2):
        for i, n in enumerate(names):
            sid = "TP09%04d" % (1000 + i + cid * 37)
            srow = db.execute("INSERT INTO students(course_id,student_id,name,email) VALUES(?,?,?,?)",
                              (cid, sid, n, sid.lower() + "@mail.apu.edu.my")).lastrowid
            weak = (i == 2 or i == 6)
            for k in range(1, 9):
                r = random.random()
                if weak:
                    st = "absent" if r < 0.45 else ("late" if r < 0.6 else "present")
                else:
                    st = "absent" if r < 0.07 else ("late" if r < 0.15 else "present")
                db.execute("INSERT INTO attendance(course_id,student_row,date,status) VALUES(?,?,?,?)",
                           (cid, srow, d(-7 * k), st))

    samples = [
        ("Tan Wei Ling", "TP091037", "Java Programming", "Assignment extension request",
         "Hi Dr. Lim, I was admitted to hospital last Thursday because of dengue and I couldn't "
         "finish my Java assignment. I have the medical certificate. Is it possible to get a one-week "
         "extension for the assignment that is due this Friday? Thank you so much."),
        ("Daniel Lee", "TP091043", "Responsive Web Design", "Question about the group project marking",
         "Hello, for the group project do we each get the same mark or is it individual? One of my "
         "group members has not done any work for 3 weeks and I'm worried it will affect my grade."),
        ("Priya Nair", "TP091039", "Java Programming", "Can't log in to Moodle",
         "Hi, I cannot access the lab sheets on Moodle since yesterday, it says my account is "
         "locked. Can you send me the week 6 lab sheet by email?"),
    ]
    for n, sid, mod, subj, msg in samples:
        ai = fallback_analysis(subj, msg)
        db.execute("INSERT INTO inquiries(lecturer_id,student_name,student_id,student_email,module,subject,"
                   "message,summary,category,priority,suggested_reply,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (lid, n, sid, sid.lower() + "@mail.apu.edu.my", mod, subj, msg, ai["summary"],
                    ai["category"], ai["priority"], ai["suggested_reply"],
                    datetime.now().isoformat(timespec="minutes")))

    for t, due in [("Mark Java assignment batch 1", d(2)),
                   ("Upload week 7 lab sheet", d(1)),
                   ("Submit module report", d(5))]:
        db.execute("INSERT INTO tasks(lecturer_id,text,due) VALUES(?,?,?)", (lid, t, due))
    db.execute("INSERT INTO booking_links(lecturer_id,token,title,duration,location,created_at) "
               "VALUES(?,?,?,?,?,?)", (lid, secrets.token_urlsafe(8), "Consultation Hours", 30,
                                       "Office L4-12", datetime.now().isoformat(timespec="minutes")))


# ----------------------------------------------------------------- AI (Gemini)
CATEGORIES = ["Extension Request", "Assignment Question", "Grades / Marks",
              "Technical Issue", "Attendance", "Appointment", "General"]


def fallback_analysis(subject, message):
    """Simple rule-based backup used when no Gemini key is set or the API fails."""
    text = (subject + " " + message).lower()
    rules = [
        ("Extension Request", ["extension", "extend", "medical", " mc ", "hospital", "late submission"]),
        ("Technical Issue", ["login", "log in", "moodle", "password", "locked", "error", "cannot access"]),
        ("Grades / Marks", ["mark", "grade", "result", "score"]),
        ("Attendance", ["attendance", "absent", "missed class"]),
        ("Appointment", ["meet", "consultation", "appointment"]),
        ("Assignment Question", ["assignment", "project", "lab", "question"]),
    ]
    category = "General"
    for cat, words in rules:
        if any(w in text for w in words):
            category = cat
            break
    urgent = any(w in text for w in ["urgent", "asap", "hospital", "due this", "tomorrow", "today", "deadline"])
    words = message.split()
    summary = " ".join(words[:22]) + ("..." if len(words) > 22 else "")
    reply = ("Hi, thank you for your message regarding \"%s\". I have received it and will "
             "get back to you shortly.\n\nRegards" % subject)
    return {"summary": summary, "category": category,
            "priority": "High" if urgent else "Normal", "suggested_reply": reply,
            "source": "fallback"}


def get_key(lecturer_id=None):
    if lecturer_id:
        r = get_db().execute("SELECT gemini_key FROM lecturers WHERE id=?", (lecturer_id,)).fetchone()
        if r and r["gemini_key"]:
            return r["gemini_key"]
    return GEMINI_API_KEY


def call_gemini(prompt, key):
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % GEMINI_MODEL)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read())
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(json)?|```$", "", text.strip()).strip()
    return json.loads(text)


def analyse_inquiry(subject, message, student_name, lecturer_name, key):
    if not key:
        return fallback_analysis(subject, message)
    prompt = (
        "You help a university lecturer manage student emails.\n"
        "Read this student inquiry and reply ONLY with JSON in this exact shape:\n"
        '{"summary": "one short sentence (max 20 words) saying what the student wants",'
        ' "category": one of %s,'
        ' "priority": "High" or "Normal" or "Low",'
        ' "suggested_reply": "a polite, short reply the lecturer could send, signed by %s"}\n\n'
        "Student: %s\nSubject: %s\nMessage:\n%s" % (json.dumps(CATEGORIES), lecturer_name,
                                                     student_name, subject, message))
    try:
        out = call_gemini(prompt, key)
        result = fallback_analysis(subject, message)
        for k in ("summary", "category", "priority", "suggested_reply"):
            if out.get(k):
                result[k] = str(out[k])
        result["source"] = "gemini"
        return result
    except Exception as e:
        print("Gemini error, using fallback:", e)
        return fallback_analysis(subject, message)


# ----------------------------------------------------------------- auth
def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "lecturer_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not logged in"}), 401
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


def me():
    return session["lecturer_id"]


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        r = get_db().execute("SELECT * FROM lecturers WHERE email=?", (email,)).fetchone()
        if r and check_password_hash(r["password"], pw):
            session["lecturer_id"] = r["id"]
            return redirect(url_for("home"))
        error = "Wrong email or password."
    return render_template("portal.html", view="auth", error=error, mode="login")


@app.route("/register", methods=["GET", "POST"])
def register():
    error = ""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not name or not email or len(pw) < 6:
            error = "Fill in all fields. Password must be at least 6 characters."
        else:
            db = get_db()
            try:
                cur = db.execute("INSERT INTO lecturers(name,email,password,inquiry_token) VALUES(?,?,?,?)",
                                 (name, email, generate_password_hash(pw), secrets.token_urlsafe(8)))
                db.commit()
                session["lecturer_id"] = cur.lastrowid
                return redirect(url_for("home"))
            except sqlite3.IntegrityError:
                error = "That email is already registered."
    return render_template("portal.html", view="auth", error=error, mode="register")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    return render_template("portal.html", view="app")


# ----------------------------------------------------------------- API: profile & dashboard
@app.route("/api/me")
@login_required
def api_me():
    r = get_db().execute("SELECT id,name,email,gemini_key,inquiry_token FROM lecturers WHERE id=?",
                         (me(),)).fetchone()
    d = dict(r)
    d["has_key"] = bool(d.pop("gemini_key") or GEMINI_API_KEY)
    d["inquiry_link"] = request.host_url.rstrip("/") + "/ask/" + d["inquiry_token"]
    return jsonify(d)


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    data = request.json or {}
    db = get_db()
    if "name" in data and data["name"].strip():
        db.execute("UPDATE lecturers SET name=? WHERE id=?", (data["name"].strip(), me()))
    if "gemini_key" in data:
        db.execute("UPDATE lecturers SET gemini_key=? WHERE id=?", (data["gemini_key"].strip(), me()))
    if data.get("new_inquiry_link"):
        db.execute("UPDATE lecturers SET inquiry_token=? WHERE id=?", (secrets.token_urlsafe(8), me()))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/test-ai", methods=["POST"])
@login_required
def api_test_ai():
    key = get_key(me())
    if not key:
        return jsonify({"ok": False, "message": "No Gemini key saved yet."})
    try:
        call_gemini('Reply with JSON {"ok": true}', key)
        return jsonify({"ok": True, "message": "Gemini is connected and working."})
    except urllib.error.HTTPError as e:
        return jsonify({"ok": False, "message": "Gemini error %s: %s" % (e.code, e.read()[:200].decode(errors="ignore"))})
    except Exception as e:
        return jsonify({"ok": False, "message": "Could not reach Gemini: %s" % e})


@app.route("/api/dashboard")
@login_required
def api_dashboard():
    db = get_db()
    today = date.today().isoformat()
    week = (date.today() + timedelta(days=7)).isoformat()
    upcoming = rows(db.execute(
        "SELECT * FROM events WHERE lecturer_id=? AND date>=? AND date<=? ORDER BY date,start_time",
        (me(), today, week)))
    inq = rows(db.execute(
        "SELECT id,student_name,module,subject,summary,category,priority,status,created_at "
        "FROM inquiries WHERE lecturer_id=? ORDER BY (status='replied'),"
        "(priority='High') DESC, id DESC LIMIT 9", (me(),)))
    unread = db.execute("SELECT COUNT(*) FROM inquiries WHERE lecturer_id=? AND status='new'",
                        (me(),)).fetchone()[0]
    tasks = rows(db.execute("SELECT * FROM tasks WHERE lecturer_id=? AND done=0 ORDER BY due",
                            (me(),)))
    return jsonify({
        "today": today,
        "upcoming": upcoming,
        "today_count": sum(1 for e in upcoming if e["date"] == today),
        "inquiries": inq,
        "unread": unread,
        "at_risk": at_risk_students(),
        "tasks": tasks,
    })


# ----------------------------------------------------------------- API: calendar
@app.route("/api/events")
@login_required
def api_events():
    start = request.args.get("start", "0000-00-00")
    end = request.args.get("end", "9999-99-99")
    return jsonify(rows(get_db().execute(
        "SELECT * FROM events WHERE lecturer_id=? AND date>=? AND date<=? ORDER BY date,start_time",
        (me(), start, end))))


@app.route("/api/events", methods=["POST"])
@login_required
def api_add_event():
    d = request.json or {}
    if not d.get("title") or not d.get("date") or not d.get("start_time") or not d.get("end_time"):
        return jsonify({"error": "Title, date, start and end time are required."}), 400
    if d["end_time"] <= d["start_time"]:
        return jsonify({"error": "End time must be after start time."}), 400
    db = get_db()
    repeat = max(1, min(int(d.get("repeat_weeks") or 1), 14))
    base = date.fromisoformat(d["date"])
    for w in range(repeat):
        db.execute("INSERT INTO events(lecturer_id,title,type,date,start_time,end_time,location,notes)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   (me(), d["title"], d.get("type", "class"), (base + timedelta(weeks=w)).isoformat(),
                    d["start_time"], d["end_time"], d.get("location", ""), d.get("notes", "")))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/events/<int:eid>", methods=["DELETE"])
@login_required
def api_del_event(eid):
    db = get_db()
    db.execute("DELETE FROM events WHERE id=? AND lecturer_id=?", (eid, me()))
    db.commit()
    return jsonify({"ok": True})


# ----------------------------------------------------------------- API: booking links
@app.route("/api/links")
@login_required
def api_links():
    out = rows(get_db().execute("SELECT * FROM booking_links WHERE lecturer_id=? ORDER BY id DESC", (me(),)))
    for l in out:
        l["url"] = request.host_url.rstrip("/") + "/book/" + l["token"]
    return jsonify(out)


@app.route("/api/links", methods=["POST"])
@login_required
def api_add_link():
    d = request.json or {}
    days = d.get("days") or [0, 1, 2, 3, 4]
    db = get_db()
    db.execute("INSERT INTO booking_links(lecturer_id,token,title,duration,days,start_hour,end_hour,location,created_at)"
               " VALUES(?,?,?,?,?,?,?,?,?)",
               (me(), secrets.token_urlsafe(8), d.get("title") or "Consultation",
                int(d.get("duration") or 30), ",".join(str(x) for x in days),
                d.get("start_hour") or "09:00", d.get("end_hour") or "17:00",
                d.get("location", ""), datetime.now().isoformat(timespec="minutes")))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/links/<int:lid>", methods=["PATCH", "DELETE"])
@login_required
def api_edit_link(lid):
    db = get_db()
    if request.method == "DELETE":
        db.execute("DELETE FROM booking_links WHERE id=? AND lecturer_id=?", (lid, me()))
    else:
        db.execute("UPDATE booking_links SET active=1-active WHERE id=? AND lecturer_id=?", (lid, me()))
    db.commit()
    return jsonify({"ok": True})


def to_min(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(mins):
    return "%02d:%02d" % (mins // 60, mins % 60)


def free_slots(link, days_ahead=14):
    db = get_db()
    allowed = {int(x) for x in link["days"].split(",") if x != ""}
    start, end, dur = to_min(link["start_hour"]), to_min(link["end_hour"]), link["duration"]
    now = datetime.now()
    result = []
    for i in range(1, days_ahead + 1):
        day = date.today() + timedelta(days=i)
        if day.weekday() not in allowed:
            continue
        busy = [(to_min(e["start_time"]), to_min(e["end_time"])) for e in db.execute(
            "SELECT start_time,end_time FROM events WHERE lecturer_id=? AND date=?",
            (link["lecturer_id"], day.isoformat()))]
        slots = []
        t = start
        while t + dur <= end:
            if not any(t < b_end and t + dur > b_start for b_start, b_end in busy):
                slots.append(to_hhmm(t))
            t += dur
        if slots:
            result.append({"date": day.isoformat(), "label": day.strftime("%a, %d %b"), "slots": slots})
    return result


@app.route("/book/<token>", methods=["GET", "POST"])
def book_page(token):
    db = get_db()
    link = db.execute("SELECT b.*, l.name AS lecturer FROM booking_links b JOIN lecturers l "
                      "ON l.id=b.lecturer_id WHERE token=?", (token,)).fetchone()
    if not link or not link["active"]:
        return render_template("portal.html", view="public", page="closed", lecturer="")
    link = dict(link)
    msg, err = "", ""
    if request.method == "POST":
        f = request.form
        name, email, day, slot = f.get("name", "").strip(), f.get("email", "").strip(), f.get("date"), f.get("slot")
        reason = f.get("reason", "").strip()
        valid = any(d["date"] == day and slot in d["slots"] for d in free_slots(link))
        if not name or not email or not valid:
            err = "Please fill in your details and choose a free slot (it may have just been taken)."
        else:
            end = to_hhmm(to_min(slot) + link["duration"])
            db.execute("INSERT INTO events(lecturer_id,title,type,date,start_time,end_time,location,notes,"
                       "student_name,student_email) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (link["lecturer_id"], "%s: %s" % (link["title"], name), "appointment", day, slot, end,
                        link["location"], reason, name, email))
            db.commit()
            msg = "Booked! %s on %s, %s - %s at %s." % (link["title"], day, slot, end,
                                                          link["location"] or "the lecturer's office")
    return render_template("portal.html", view="public", page="book", link=link, days=free_slots(link),
                           lecturer=link["lecturer"], msg=msg, err=err)


# ----------------------------------------------------------------- inquiries
@app.route("/ask/<token>", methods=["GET", "POST"])
def ask_page(token):
    db = get_db()
    lec = db.execute("SELECT * FROM lecturers WHERE inquiry_token=?", (token,)).fetchone()
    if not lec:
        return render_template("portal.html", view="public", page="closed", lecturer="")
    msg, err = "", ""
    if request.method == "POST":
        f = request.form
        name, subject, message = f.get("name", "").strip(), f.get("subject", "").strip(), f.get("message", "").strip()
        if not name or not subject or not message:
            err = "Please fill in your name, subject and message."
        else:
            ai = analyse_inquiry(subject, message, name, lec["name"], get_key(lec["id"]))
            db.execute("INSERT INTO inquiries(lecturer_id,student_name,student_id,student_email,module,subject,message,"
                       "summary,category,priority,suggested_reply,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (lec["id"], name, f.get("student_id", "").strip(), f.get("email", "").strip(),
                        f.get("module", "").strip(), subject, message, ai["summary"], ai["category"],
                        ai["priority"], ai["suggested_reply"], datetime.now().isoformat(timespec="minutes")))
            db.commit()
            msg = "Your message has been sent to %s. You will get a reply soon." % lec["name"]
    return render_template("portal.html", view="public", page="ask", lecturer=lec["name"], msg=msg, err=err)


@app.route("/api/inquiries")
@login_required
def api_inquiries():
    return jsonify(rows(get_db().execute(
        "SELECT * FROM inquiries WHERE lecturer_id=? ORDER BY (status='replied'),"
        "(priority='High') DESC, id DESC", (me(),))))


@app.route("/api/inquiries/<int:iid>", methods=["GET", "POST", "DELETE"])
@login_required
def api_inquiry(iid):
    db = get_db()
    r = db.execute("SELECT * FROM inquiries WHERE id=? AND lecturer_id=?", (iid, me())).fetchone()
    if not r:
        abort(404)
    if request.method == "DELETE":
        db.execute("DELETE FROM inquiries WHERE id=?", (iid,))
        db.commit()
        return jsonify({"ok": True})
    if request.method == "POST":
        d = request.json or {}
        if d.get("action") == "reanalyse":
            if not get_key(me()):
                return need_key()
            lec = db.execute("SELECT name FROM lecturers WHERE id=?", (me(),)).fetchone()
            ai = analyse_inquiry(r["subject"], r["message"], r["student_name"], lec["name"], get_key(me()))
            db.execute("UPDATE inquiries SET summary=?,category=?,priority=?,suggested_reply=? WHERE id=?",
                       (ai["summary"], ai["category"], ai["priority"], ai["suggested_reply"], iid))
        elif d.get("action") == "reply":
            db.execute("UPDATE inquiries SET reply=?,status='replied' WHERE id=?", (d.get("reply", ""), iid))
        elif d.get("action") == "unread":
            db.execute("UPDATE inquiries SET status='new' WHERE id=?", (iid,))
        db.commit()
    elif r["status"] == "new":
        db.execute("UPDATE inquiries SET status='read' WHERE id=?", (iid,))
        db.commit()
    return jsonify(dict(db.execute("SELECT * FROM inquiries WHERE id=?", (iid,)).fetchone()))


# ----------------------------------------------------------------- attendance
@app.route("/api/courses")
@login_required
def api_courses():
    db = get_db()
    out = rows(db.execute("SELECT * FROM courses WHERE lecturer_id=? ORDER BY code", (me(),)))
    for c in out:
        c["students"] = db.execute("SELECT COUNT(*) FROM students WHERE course_id=?", (c["id"],)).fetchone()[0]
    return jsonify(out)


@app.route("/api/courses", methods=["POST"])
@login_required
def api_add_course():
    d = request.json or {}
    if not d.get("code") or not d.get("name"):
        return jsonify({"error": "Course code and name are required."}), 400
    db = get_db()
    cid = db.execute("INSERT INTO courses(lecturer_id,code,name) VALUES(?,?,?)",
                     (me(), d["code"].strip(), d["name"].strip())).lastrowid
    add_students(db, cid, d.get("students", ""))
    db.commit()
    return jsonify({"ok": True, "id": cid})


def own_course(cid):
    c = get_db().execute("SELECT * FROM courses WHERE id=? AND lecturer_id=?", (cid, me())).fetchone()
    if not c:
        abort(404)
    return c


def add_students(db, cid, text):
    """One student per line: TP number, name, email (email optional)."""
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            db.execute("INSERT INTO students(course_id,student_id,name,email) VALUES(?,?,?,?)",
                       (cid, parts[0], parts[1], parts[2] if len(parts) > 2 else ""))


@app.route("/api/courses/<int:cid>", methods=["DELETE"])
@login_required
def api_del_course(cid):
    own_course(cid)
    db = get_db()
    db.execute("DELETE FROM attendance WHERE course_id=?", (cid,))
    db.execute("DELETE FROM students WHERE course_id=?", (cid,))
    db.execute("DELETE FROM courses WHERE id=?", (cid,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/courses/<int:cid>/students", methods=["POST"])
@login_required
def api_add_students(cid):
    own_course(cid)
    db = get_db()
    add_students(db, cid, (request.json or {}).get("students", ""))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/students/<int:sid>", methods=["DELETE"])
@login_required
def api_del_student(sid):
    db = get_db()
    s = db.execute("SELECT s.* FROM students s JOIN courses c ON c.id=s.course_id "
                   "WHERE s.id=? AND c.lecturer_id=?", (sid, me())).fetchone()
    if s:
        db.execute("DELETE FROM attendance WHERE student_row=?", (sid,))
        db.execute("DELETE FROM students WHERE id=?", (sid,))
        db.commit()
    return jsonify({"ok": True})


def student_stats(course_id):
    db = get_db()
    out = rows(db.execute("SELECT * FROM students WHERE course_id=? ORDER BY name", (course_id,)))
    for s in out:
        recs = rows(db.execute("SELECT status FROM attendance WHERE student_row=?", (s["id"],)))
        total = len(recs)
        present = sum(1 for r in recs if r["status"] == "present")
        late = sum(1 for r in recs if r["status"] == "late")
        s["total"], s["present"], s["late"] = total, present, late
        s["absent"] = total - present - late
        s["percent"] = round((present + late) * 100 / total) if total else 100
        s["at_risk"] = total > 0 and s["percent"] < 80
    return out


def at_risk_students():
    db = get_db()
    out = []
    for c in db.execute("SELECT * FROM courses WHERE lecturer_id=?", (me(),)).fetchall():
        for s in student_stats(c["id"]):
            if s["at_risk"]:
                out.append({"name": s["name"], "student_id": s["student_id"], "course": c["code"],
                            "percent": s["percent"]})
    return sorted(out, key=lambda x: x["percent"])


@app.route("/api/courses/<int:cid>/attendance")
@login_required
def api_attendance(cid):
    c = own_course(cid)
    day = request.args.get("date", date.today().isoformat())
    db = get_db()
    marks = {r["student_row"]: r["status"] for r in db.execute(
        "SELECT student_row,status FROM attendance WHERE course_id=? AND date=?", (cid, day))}
    students = student_stats(cid)
    for s in students:
        s["today"] = marks.get(s["id"], "")
    sessions = [r[0] for r in db.execute(
        "SELECT DISTINCT date FROM attendance WHERE course_id=? ORDER BY date DESC", (cid,))]
    return jsonify({"course": dict(c), "date": day, "students": students, "sessions": sessions})


@app.route("/api/courses/<int:cid>/attendance", methods=["POST"])
@login_required
def api_save_attendance(cid):
    own_course(cid)
    d = request.json or {}
    day = d.get("date") or date.today().isoformat()
    db = get_db()
    valid_ids = {r[0] for r in db.execute("SELECT id FROM students WHERE course_id=?", (cid,))}
    for sid, status in (d.get("marks") or {}).items():
        sid = int(sid)
        if sid not in valid_ids:
            continue
        if status in ("present", "late", "absent"):
            db.execute("INSERT INTO attendance(course_id,student_row,date,status) VALUES(?,?,?,?) "
                       "ON CONFLICT(student_row,date) DO UPDATE SET status=excluded.status",
                       (cid, sid, day, status))
        else:
            db.execute("DELETE FROM attendance WHERE student_row=? AND date=?", (sid, day))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/courses/<int:cid>/export")
@login_required
def api_export(cid):
    c = own_course(cid)
    lines = ["Student ID,Name,Present,Late,Absent,Attendance %,Status"]
    for s in student_stats(cid):
        lines.append('%s,"%s",%d,%d,%d,%d,%s' % (s["student_id"], s["name"], s["present"], s["late"],
                                                 s["absent"], s["percent"], "AT RISK" if s["at_risk"] else "OK"))
    return ("\n".join(lines), 200, {"Content-Type": "text/csv",
                                   "Content-Disposition": "attachment; filename=%s_attendance.csv" % c["code"]})


# ----------------------------------------------------------------- tasks
@app.route("/api/tasks", methods=["GET", "POST"])
@login_required
def api_tasks():
    db = get_db()
    if request.method == "POST":
        d = request.json or {}
        if d.get("text", "").strip():
            db.execute("INSERT INTO tasks(lecturer_id,text,due) VALUES(?,?,?)",
                       (me(), d["text"].strip(), d.get("due", "")))
            db.commit()
    return jsonify(rows(db.execute("SELECT * FROM tasks WHERE lecturer_id=? ORDER BY done, due", (me(),))))


@app.route("/api/tasks/<int:tid>", methods=["PATCH", "DELETE"])
@login_required
def api_task(tid):
    db = get_db()
    if request.method == "DELETE":
        db.execute("DELETE FROM tasks WHERE id=? AND lecturer_id=?", (tid, me()))
    else:
        db.execute("UPDATE tasks SET done=1-done WHERE id=? AND lecturer_id=?", (tid, me()))
    db.commit()
    return jsonify({"ok": True})


# ================================================================= AI FEATURES
# Every feature asks Gemini first. If there is no key, or Gemini fails,
# it falls back to simple rules so the portal always works.
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


NEED_KEY_MSG = "Add your Gemini API key in Settings before you can use AI features."


def need_key():
    """Sent back when an AI feature is used without a Gemini key."""
    return jsonify({"need_key": True, "error": NEED_KEY_MSG}), 428


def has_key():
    return bool(get_key(me()))


def lecturer_name():
    return get_db().execute("SELECT name FROM lecturers WHERE id=?", (me(),)).fetchone()["name"]


def ask_ai(prompt, fallback):
    """Run a Gemini prompt that returns JSON. Returns (result, source)."""
    key = get_key(me())
    if key:
        try:
            out = call_gemini(prompt, key)
            if out:
                return out, "gemini"
        except Exception as e:
            print("Gemini error, using fallback:", e)
    return fallback(), "basic"


def busy_times(day_iso):
    return [(to_min(e["start_time"]), to_min(e["end_time"])) for e in get_db().execute(
        "SELECT start_time,end_time FROM events WHERE lecturer_id=? AND date=?", (me(), day_iso))]


def free_windows(days=7, start="09:00", end="17:00", length=30):
    """Free time slots for the lecturer over the next few days."""
    out = []
    for i in range(1, days + 1):
        day = date.today() + timedelta(days=i)
        if day.weekday() >= 5:
            continue
        busy = busy_times(day.isoformat())
        t = to_min(start)
        while t + length <= to_min(end):
            if not any(t < b2 and t + length > b1 for b1, b2 in busy):
                out.append({"date": day.isoformat(), "weekday": day.strftime("%A"),
                            "start": to_hhmm(t), "end": to_hhmm(t + length)})
            t += length
    return out


def portal_snapshot():
    """A compact summary of the lecturer's data, used as context for the AI."""
    db = get_db()
    today = date.today()
    events = rows(db.execute(
        "SELECT title,type,date,start_time,end_time,location FROM events WHERE lecturer_id=? AND date>=? AND date<=? "
        "ORDER BY date,start_time", (me(), (today - timedelta(days=7)).isoformat(), (today + timedelta(days=14)).isoformat())))
    courses = []
    for c in db.execute("SELECT * FROM courses WHERE lecturer_id=?", (me(),)).fetchall():
        studs = student_stats(c["id"])
        courses.append({"code": c["code"], "name": c["name"], "students": [
            {"name": s["name"], "id": s["student_id"], "attendance_percent": s["percent"],
             "absent": s["absent"], "late": s["late"], "sessions": s["total"]} for s in studs]})
    inquiries = rows(db.execute(
        "SELECT student_name,subject,summary,category,priority,status,created_at FROM inquiries WHERE lecturer_id=? "
        "ORDER BY id DESC LIMIT 20", (me(),)))
    tasks = rows(db.execute("SELECT text,due,done FROM tasks WHERE lecturer_id=?", (me(),)))
    return {"today": today.isoformat(), "weekday": today.strftime("%A"), "events": events,
            "courses": courses, "inquiries": inquiries, "tasks": tasks, "at_risk": at_risk_students()}


# ---------- 1. AI daily briefing (dashboard)
@app.route("/api/ai/briefing")
@login_required
def ai_briefing():
    if not has_key():
        return need_key()
    snap = portal_snapshot()
    today = snap["today"]
    todays = [e for e in snap["events"] if e["date"] == today]
    unread = [q for q in snap["inquiries"] if q["status"] == "new"]
    urgent = [q for q in unread if q["priority"] == "High"]
    due = [t for t in snap["tasks"] if not t["done"] and t["due"] and t["due"] <= (date.today() + timedelta(days=2)).isoformat()]

    def basic():
        b = []
        if todays:
            b.append("You have %d item(s) today, starting with %s at %s." % (len(todays), todays[0]["title"], todays[0]["start_time"]))
        else:
            b.append("Nothing on your calendar today.")
        if unread:
            b.append("%d new student message(s)%s." % (len(unread), ", %d marked urgent" % len(urgent) if urgent else ""))
        if snap["at_risk"]:
            names = ", ".join("%s (%s, %d%%)" % (s["name"], s["course"], s["percent"]) for s in snap["at_risk"][:3])
            b.append("%d student(s) are below 80%% attendance: %s." % (len(snap["at_risk"]), names))
        if due:
            b.append("Due soon: %s." % "; ".join(t["text"] for t in due[:3]))
        return {"headline": "Here is your day at a glance.", "bullets": b}

    prompt = ("You are an assistant for a university lecturer named %s. Write a short, friendly morning briefing.\n"
              "Reply ONLY with JSON: {\"headline\": \"one short sentence\", \"bullets\": [\"3 to 5 short, specific points\"]}.\n"
              "Mention today's schedule, urgent student messages, at-risk students and tasks due soon. "
              "Be specific with names and times. Suggest one priority for the day.\n\nDATA:\n%s"
              % (lecturer_name(), json.dumps({"today": today, "weekday": snap["weekday"], "today_events": todays,
                                              "unread_inquiries": unread, "at_risk": snap["at_risk"], "tasks_due_soon": due})))
    out, src = ask_ai(prompt, basic)
    return jsonify({"headline": str(out.get("headline", "")), "bullets": [str(x) for x in out.get("bullets", [])][:5],
                    "source": src})


# ---------- 2. AI at-risk student insights (attendance)
def attendance_pattern(student_row):
    recs = rows(get_db().execute("SELECT date,status FROM attendance WHERE student_row=? ORDER BY date", (student_row,)))
    by_day = {}
    for r in recs:
        if r["status"] == "absent":
            wd = date.fromisoformat(r["date"]).strftime("%A")
            by_day[wd] = by_day.get(wd, 0) + 1
    last = [r["status"] for r in recs[-4:]]
    return recs, by_day, last


@app.route("/api/ai/insights/<int:cid>", methods=["POST"])
@login_required
def ai_insights(cid):
    if not has_key():
        return need_key()
    c = own_course(cid)
    risky = [s for s in student_stats(cid) if s["at_risk"]]
    if not risky:
        return jsonify({"items": [], "source": "basic", "message": "No at-risk students in this module."})
    data = []
    for s in risky:
        recs, by_day, last = attendance_pattern(s["id"])
        data.append({"row": s["id"], "name": s["name"], "student_id": s["student_id"], "email": s["email"],
                     "attendance_percent": s["percent"], "present": s["present"], "late": s["late"],
                     "absent": s["absent"], "absences_by_weekday": by_day, "last_4_classes": last})
    lname = lecturer_name()

    def basic():
        items = []
        for d in data:
            worst = max(d["absences_by_weekday"], key=d["absences_by_weekday"].get) if d["absences_by_weekday"] else None
            trend = "missed the last class" if d["last_4_classes"][-1:] == ["absent"] else "attended the last class"
            insight = "Missed %d of %d classes (%d%%)%s, and %s." % (
                d["absent"], d["present"] + d["late"] + d["absent"], d["attendance_percent"],
                ", mostly on %ss" % worst if worst else "", trend)
            items.append({"row": d["row"], "insight": insight,
                          "action": "Send a friendly check-in and offer a consultation slot.",
                          "risk": "High" if d["attendance_percent"] < 60 else "Medium",
                          "email_subject": "Checking in - %s" % c["name"],
                          "email_body": "Hi %s,\n\nI noticed your attendance in %s is at %d%%. Is everything okay? "
                                        "If something is affecting your classes, let me know and we can work out a plan. "
                                        "You are welcome to book a consultation with me.\n\nBest regards,\n%s"
                                        % (d["name"].split()[0], c["name"], d["attendance_percent"], lname)})
        return {"items": items}

    prompt = ("You help a university lecturer support students with low attendance in the module %s (%s).\n"
              "For EACH student, look at the pattern and reply ONLY with JSON:\n"
              "{\"items\": [{\"row\": the row number given, \"insight\": \"1-2 sentences on the pattern (weekdays, trend, lateness)\","
              " \"action\": \"one practical next step\", \"risk\": \"High\" or \"Medium\","
              " \"email_subject\": \"short subject\", \"email_body\": \"a kind, non-judgemental check-in email signed by %s\"}]}\n\n"
              "STUDENTS:\n%s" % (c["name"], c["code"], lname, json.dumps(data)))
    out, src = ask_ai(prompt, basic)
    info = {d["row"]: d for d in data}
    items = []
    for it in out.get("items", []):
        try:
            d = info[int(it.get("row"))]
        except (TypeError, ValueError, KeyError):
            continue
        items.append({"row": d["row"], "name": d["name"], "student_id": d["student_id"], "email": d["email"],
                      "percent": d["attendance_percent"], "insight": str(it.get("insight", "")),
                      "action": str(it.get("action", "")), "risk": str(it.get("risk", "Medium")),
                      "email_subject": str(it.get("email_subject", "")), "email_body": str(it.get("email_body", ""))})
    if not items:
        return ai_insights_basic_only(basic, info)
    return jsonify({"items": items, "source": src})


def ai_insights_basic_only(basic, info):
    items = []
    for it in basic()["items"]:
        d = info[it["row"]]
        it.update({"name": d["name"], "student_id": d["student_id"], "email": d["email"], "percent": d["attendance_percent"]})
        items.append(it)
    return jsonify({"items": items, "source": "basic"})


# ---------- 3. AI meeting finder (inquiries)
def own_inquiry(iid):
    r = get_db().execute("SELECT * FROM inquiries WHERE id=? AND lecturer_id=?", (iid, me())).fetchone()
    if not r:
        abort(404)
    return r


@app.route("/api/ai/schedule/<int:iid>", methods=["POST"])
@login_required
def ai_schedule(iid):
    if not has_key():
        return need_key()
    q = own_inquiry(iid)
    slots = free_windows(days=7)
    text = (q["subject"] + " " + q["message"]).lower()

    def basic():
        wants = any(w in text for w in ["meet", "consult", "appointment", "discuss in person", "see you", "talk to you",
                                        "your office", "free to", "available"])
        days = [d.capitalize() for d in WEEKDAYS if d in text] if wants else []
        part = "afternoon" if "afternoon" in text else "morning" if "morning" in text else ""
        pick = [s for s in slots if (not days or s["weekday"] in days)
                and (part != "afternoon" or s["start"] >= "12:00") and (part != "morning" or s["start"] < "12:00")]
        pick = (pick or slots)[:3]
        return {"wants_meeting": wants,
                "student_request": ("Mentions " + ", ".join(days) + (" " + part if part else "")) if days else
                                   ("Asks to meet" if wants else "No clear meeting request"),
                "suggestions": [dict(s, reason="Free in your calendar") for s in pick]}

    prompt = ("A student sent this message to their lecturer. Today is %s (%s).\n"
              "Decide if the student wants to meet. If they do, pick up to 3 of the lecturer's FREE slots that best match what "
              "the student asked for (day, time of day, urgency).\n"
              "Reply ONLY with JSON: {\"wants_meeting\": true/false, \"student_request\": \"short description of what they asked for\","
              " \"suggestions\": [{\"date\": \"YYYY-MM-DD\", \"start\": \"HH:MM\", \"end\": \"HH:MM\", \"reason\": \"short reason\"}]}\n"
              "Only use slots from FREE_SLOTS.\n\nSUBJECT: %s\nMESSAGE: %s\n\nFREE_SLOTS:\n%s"
              % (date.today().isoformat(), date.today().strftime("%A"), q["subject"], q["message"], json.dumps(slots[:60])))
    out, src = ask_ai(prompt, basic)
    valid = {(s["date"], s["start"]) for s in slots}
    sugg = [s for s in out.get("suggestions", []) if (s.get("date"), s.get("start")) in valid][:3]
    if not sugg and src == "gemini" and out.get("wants_meeting"):
        sugg = basic()["suggestions"]
    for s in sugg:
        s["label"] = date.fromisoformat(s["date"]).strftime("%a %d %b") + ", " + s["start"] + " - " + s["end"]
    return jsonify({"wants_meeting": bool(out.get("wants_meeting")), "student_request": str(out.get("student_request", "")),
                    "suggestions": sugg, "student_name": q["student_name"], "student_email": q["student_email"],
                    "source": src})


@app.route("/api/ai/schedule/<int:iid>/book", methods=["POST"])
@login_required
def ai_schedule_book(iid):
    q = own_inquiry(iid)
    d = request.json or {}
    if (d.get("date"), d.get("start")) not in {(s["date"], s["start"]) for s in free_windows(days=7)}:
        return jsonify({"error": "That slot is no longer free."}), 400
    db = get_db()
    db.execute("INSERT INTO events(lecturer_id,title,type,date,start_time,end_time,location,notes,student_name,student_email)"
               " VALUES(?,?,?,?,?,?,?,?,?,?)",
               (me(), "Consultation: %s" % q["student_name"], "appointment", d["date"], d["start"], d["end"],
                d.get("location", ""), "From inquiry: " + q["subject"], q["student_name"], q["student_email"]))
    db.commit()
    return jsonify({"ok": True})


# ---------- 4. AI extension-request advisor (inquiries)
@app.route("/api/ai/extension/<int:iid>", methods=["POST"])
@login_required
def ai_extension(iid):
    if not has_key():
        return need_key()
    q = own_inquiry(iid)
    db = get_db()
    att = None
    if q["student_id"]:
        s = db.execute("SELECT s.id FROM students s JOIN courses c ON c.id=s.course_id WHERE c.lecturer_id=? "
                       "AND UPPER(s.student_id)=UPPER(?)", (me(), q["student_id"])).fetchone()
        if s:
            for st in student_stats(db.execute("SELECT course_id FROM students WHERE id=?", (s["id"],)).fetchone()[0]):
                if st["id"] == s["id"]:
                    att = st["percent"]
    text = (q["subject"] + " " + q["message"]).lower()
    lname = lecturer_name()

    def basic():
        evidence = any(w in text for w in ["medical", " mc", "certificate", "hospital", "doctor", "clinic", "funeral"])
        if evidence:
            rec, reason = "Approve", "The student gives a serious reason and mentions supporting evidence."
        else:
            rec, reason = "Ask for evidence", "No supporting document is mentioned yet."
        reply = ("Hi %s,\n\nThank you for letting me know. %s\n\nBest regards,\n%s" % (
            q["student_name"].split()[0],
            "I can approve a one-week extension once you email me a copy of your supporting document."
            if rec == "Approve" else
            "Could you please send me any supporting document (for example a medical certificate) so I can review your request?",
            lname))
        return {"recommendation": rec, "reason": reason, "suggested_extension": "7 days" if rec == "Approve" else "-",
                "checklist": ["Supporting document received", "Check the module's extension policy", "Update the deadline in the system"],
                "reply": reply}

    prompt = ("You advise a university lecturer on a student's assignment extension request. The lecturer makes the final decision.\n"
              "Reply ONLY with JSON: {\"recommendation\": \"Approve\" or \"Ask for evidence\" or \"Decline\","
              " \"reason\": \"1-2 sentences\", \"suggested_extension\": \"e.g. 7 days or -\","
              " \"checklist\": [\"2-3 short things the lecturer should check\"],"
              " \"reply\": \"a polite reply to the student, signed by %s\"}\n"
              "Be fair and kind. Serious reasons with evidence (medical, family emergency) usually justify approval.\n\n"
              "STUDENT: %s\nMODULE: %s\nSTUDENT ATTENDANCE: %s\nSUBJECT: %s\nMESSAGE: %s"
              % (lname, q["student_name"], q["module"] or "-", "%s%%" % att if att is not None else "unknown",
                 q["subject"], q["message"]))
    out, src = ask_ai(prompt, basic)
    base = basic()
    res = {k: out.get(k) or base[k] for k in base}
    res["checklist"] = [str(x) for x in res["checklist"]][:4]
    res.update({"attendance": att, "source": src})
    return jsonify(res)


# ---------- 5. AI natural-language event creator (calendar)
def basic_parse_event(text):
    t = text.lower()
    today = date.today()
    res = {"title": "", "type": "class", "date": (today + timedelta(days=1)).isoformat(), "start_time": "09:00",
           "end_time": "10:00", "location": "", "repeat_weeks": 1, "notes": ""}
    for i, wd in enumerate(WEEKDAYS):
        if wd in t or re.search(r"\b%s\b" % wd[:3], t):
            ahead = (i - today.weekday()) % 7 or 7
            res["date"] = (today + timedelta(days=ahead)).isoformat()
            break
    if "today" in t:
        res["date"] = today.isoformat()
    elif "tomorrow" in t:
        res["date"] = (today + timedelta(days=1)).isoformat()

    def hhmm(h, m, ap):
        h = int(h); m = int(m or 0)
        if ap == "pm" and h < 12:
            h += 12
        if not ap and 1 <= h <= 7:
            h += 12
        return "%02d:%02d" % (h, m)
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|to|until|till)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if m:
        ap2 = m.group(6) or ""
        res["start_time"] = hhmm(m.group(1), m.group(2), m.group(3) or ap2)
        res["end_time"] = hhmm(m.group(4), m.group(5), ap2)
    else:
        m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
        if m:
            res["start_time"] = hhmm(m.group(1), m.group(2), m.group(3) or "")
            res["end_time"] = to_hhmm(to_min(res["start_time"]) + 60)
    m = re.search(r"\b(?:in|at|@)\s+((?:room|lab|hall|office|block|teams|zoom|online)[\w\s\-]*?)(?=\s+(?:for|every|on|from|at)\b|[,.]|$)", t)
    if m:
        res["location"] = m.group(1).strip().title()
    m = re.search(r"for\s+(\d{1,2})\s+weeks", t)
    if m:
        res["repeat_weeks"] = min(int(m.group(1)), 14)
    elif "semester" in t:
        res["repeat_weeks"] = 14
    elif "every" in t or "weekly" in t:
        res["repeat_weeks"] = 4
    if any(w in t for w in ["meeting", "faculty"]):
        res["type"] = "meeting"
    elif any(w in t for w in ["consultation", "appointment", "meet with", "1-on-1"]):
        res["type"] = "appointment"
    title = re.split(r"\s+(?:every|on|at|from|in|for|tomorrow|today|this|next)\b|\d", text, maxsplit=1)[0].strip(" ,.-")
    res["title"] = (title[:1].upper() + title[1:]) if title else "New event"
    return res


@app.route("/api/ai/parse-event", methods=["POST"])
@login_required
def ai_parse_event():
    if not has_key():
        return need_key()
    text = ((request.json or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "Type what you want to add first."}), 400
    prompt = ("Turn this calendar request from a university lecturer into an event. Today is %s (%s).\n"
              "Reply ONLY with JSON: {\"title\": \"...\", \"type\": \"class\" or \"appointment\" or \"meeting\" or \"other\","
              " \"date\": \"YYYY-MM-DD (the next matching date)\", \"start_time\": \"HH:MM 24h\", \"end_time\": \"HH:MM 24h\","
              " \"location\": \"...\", \"repeat_weeks\": number from 1 to 14 (14 = whole semester), \"notes\": \"...\"}\n\n"
              "REQUEST: %s" % (date.today().isoformat(), date.today().strftime("%A"), text))
    out, src = ask_ai(prompt, lambda: basic_parse_event(text))
    res = basic_parse_event(text)
    for k in res:
        if out.get(k) not in (None, ""):
            res[k] = out[k]
    try:
        res["repeat_weeks"] = max(1, min(int(res["repeat_weeks"]), 14))
        date.fromisoformat(res["date"])
    except (ValueError, TypeError):
        res = basic_parse_event(text)
    res["source"] = src
    return jsonify(res)


# ---------- 6. AI task suggestions (to-do)
@app.route("/api/ai/tasks")
@login_required
def ai_tasks():
    if not has_key():
        return need_key()
    snap = portal_snapshot()
    open_tasks = [t["text"].lower() for t in snap["tasks"] if not t["done"]]
    today = date.today()
    soon = [e for e in snap["events"] if today.isoformat() <= e["date"] <= (today + timedelta(days=3)).isoformat()
            and e["type"] == "class"]
    unreplied = [q for q in snap["inquiries"] if q["status"] != "replied"]

    def basic():
        s = []
        for q in unreplied[:3]:
            s.append({"text": "Reply to %s about \"%s\"" % (q["student_name"], q["subject"]),
                      "due": today.isoformat(), "why": "%s priority message not answered yet" % q["priority"]})
        for e in soon[:2]:
            s.append({"text": "Prepare materials for %s" % e["title"],
                      "due": (date.fromisoformat(e["date"]) - timedelta(days=1)).isoformat() if e["date"] > today.isoformat() else today.isoformat(),
                      "why": "Class on %s at %s" % (date.fromisoformat(e["date"]).strftime("%a %d %b"), e["start_time"])})
        for st in snap["at_risk"][:2]:
            s.append({"text": "Check in with %s (%s attendance %d%%)" % (st["name"], st["course"], st["percent"]),
                      "due": (today + timedelta(days=2)).isoformat(), "why": "Below 80% attendance"})
        return {"suggestions": s}

    prompt = ("You help a university lecturer plan their to-do list. Today is %s (%s).\n"
              "Suggest 4 to 6 NEW, specific tasks based on the data (unanswered messages, upcoming classes, at-risk students). "
              "Do not repeat existing tasks.\n"
              "Reply ONLY with JSON: {\"suggestions\": [{\"text\": \"short task\", \"due\": \"YYYY-MM-DD\", \"why\": \"short reason\"}]}\n\n"
              "DATA:\n%s" % (today.isoformat(), today.strftime("%A"), json.dumps(
                  {"unreplied_inquiries": unreplied, "classes_next_3_days": soon, "at_risk": snap["at_risk"],
                   "existing_tasks": open_tasks})))
    out, src = ask_ai(prompt, basic)
    sugg = []
    for s in out.get("suggestions", []):
        txt = str(s.get("text", "")).strip()
        if txt and txt.lower() not in open_tasks:
            due = str(s.get("due", ""))
            try:
                date.fromisoformat(due)
            except ValueError:
                due = ""
            sugg.append({"text": txt, "due": due, "why": str(s.get("why", ""))})
    return jsonify({"suggestions": sugg[:6], "source": src})


# ---------- 7. AI chat assistant (every page)
def basic_chat(msg, snap):
    m = msg.lower()
    today = date.today()
    if "free" in m or "available" in m:
        day = next((i for i, wd in enumerate(WEEKDAYS) if wd in m), None)
        target = today + timedelta(days=((day - today.weekday()) % 7 or 7)) if day is not None else \
            today + timedelta(days=1) if "tomorrow" in m else today
        busy = sorted(busy_times(target.isoformat()))
        free, t = [], to_min("09:00")
        for b1, b2 in busy:
            if b1 > t:
                free.append("%s-%s" % (to_hhmm(t), to_hhmm(b1)))
            t = max(t, b2)
        if t < to_min("17:00"):
            free.append("%s-17:00" % to_hhmm(t))
        return "On %s you are free: %s (between 9am and 5pm)." % (target.strftime("%A %d %b"), ", ".join(free) or "no free time")
    if any(w in m for w in ["absent", "missed", "attendance", "at risk", "at-risk", "falling behind"]):
        allst = [(s["attendance_percent"], s["name"], c["code"], s["absent"]) for c in snap["courses"] for s in c["students"]]
        allst.sort()
        top = allst[:5]
        return "Lowest attendance: " + "; ".join("%s (%s) %d%%, %d absences" % (n, c, p, a) for p, n, c, a in top) + "."
    if any(w in m for w in ["inquir", "message", "email", "unread"]):
        un = [q for q in snap["inquiries"] if q["status"] != "replied"]
        if not un:
            return "You have no unanswered student messages."
        return "%d message(s) still need a reply: " % len(un) + "; ".join(
            "%s - %s (%s)" % (q["student_name"], q["subject"], q["priority"]) for q in un[:5]) + "."
    if any(w in m for w in ["task", "to do", "to-do", "todo"]):
        ts = [t for t in snap["tasks"] if not t["done"]]
        return ("Open tasks: " + "; ".join(t["text"] + (" (due %s)" % t["due"] if t["due"] else "") for t in ts) + ".") if ts else "No open tasks."
    if any(w in m for w in ["today", "schedule", "class", "week", "tomorrow"]):
        span = 1 if "today" in m else 2 if "tomorrow" in m else 7
        start = today + timedelta(days=1) if "tomorrow" in m else today
        evs = [e for e in snap["events"] if start.isoformat() <= e["date"] < (start + timedelta(days=span)).isoformat()]
        if not evs:
            return "Nothing scheduled for that time."
        return "Schedule: " + "; ".join("%s %s %s" % (date.fromisoformat(e["date"]).strftime("%a %d %b"), e["start_time"], e["title"]) for e in evs) + "."
    return ("I can answer questions about your schedule, free time, attendance, student messages and tasks. "
            "Try: \"When am I free on Wednesday?\" or \"Who missed the most classes?\"")


@app.route("/api/ai/chat", methods=["POST"])
@login_required
def ai_chat():
    if not has_key():
        return need_key()
    d = request.json or {}
    msg = (d.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "Type a question first."}), 400
    history = d.get("history") or []
    snap = portal_snapshot()
    convo = "\n".join("%s: %s" % ("Lecturer" if h.get("role") == "user" else "Assistant", str(h.get("text", ""))[:500])
                      for h in history[-6:])
    prompt = ("You are the AI assistant inside TeacherPortal, helping the lecturer %s. Answer using ONLY the portal data below. "
              "Be short, friendly and specific (names, dates, times, numbers). If the data does not contain the answer, say so. "
              "Today is %s (%s). Working hours are 09:00-17:00.\n"
              "Reply ONLY with JSON: {\"reply\": \"your answer (plain text, can use short lines)\"}\n\n"
              "PORTAL DATA:\n%s\n\nCONVERSATION SO FAR:\n%s\n\nLecturer: %s"
              % (lecturer_name(), snap["today"], snap["weekday"], json.dumps(snap), convo, msg))
    out, src = ask_ai(prompt, lambda: {"reply": basic_chat(msg, snap)})
    return jsonify({"reply": str(out.get("reply", "")) or basic_chat(msg, snap), "source": src})


init_db()

if __name__ == "__main__":
    app.run(debug=True)