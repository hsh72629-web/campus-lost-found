"""
Campus Lost & Found AI Matcher  -  Flask backend
=================================================
Everything lives in this one file so a beginner can follow it top to bottom:

  1. Settings            5. Matching helpers        9. Chat
  2. Database            6. Student pages          10. Claims (student side)
  3. Security helpers    7. Reporting items        11. Admin pages & workflow
  4. Small helpers       8. Matches & enquiries    12. Errors, start-up

Run with:   python app.py      then open http://127.0.0.1:5000
"""
import os
import re
import base64
import io
import json
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from PIL import Image, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash

from ai.matching import HAVE_IMAGEHASH, MIN_SCORE, compute_image_hash, find_matches

# ----------------------------------------------------------------------------
# 1. SETTINGS
# ----------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
DB_PATH = os.path.join(INSTANCE_DIR, "database.db")
os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Leave empty to accept any valid email. Example: "mycollege.edu.in"
COLLEGE_EMAIL_DOMAIN = ""

# ---- HACKATHON DEMO ADMIN (CHANGE BEFORE ANY REAL DEPLOYMENT) --------------
ADMIN_USERNAME = "admin"
ADMIN_DEFAULT_PASSWORD = "admin"
# ----------------------------------------------------------------------------

CATEGORIES = ["Water Bottle", "Calculator", "Books & Notes", "ID Card / Card Holder",
              "Bag / Backpack", "Umbrella", "Electronics", "Keys",
              "Clothing & Accessories", "Stationery", "Other"]
# Students no longer pick a category. It is guessed from the words they write
# (so AI matching still works) and falls back to "Other". Staff can still see it.
CATEGORY_KEYWORDS = {
    "Water Bottle": ("bottle", "flask", "tumbler", "sipper", "thermos"),
    "Calculator": ("calculator", "casio", "fx-"),
    "Books & Notes": ("book", "notebook", "notes", "diary", "record", "textbook", "journal"),
    "ID Card / Card Holder": ("id card", "card holder", "cardholder", "id-card", "identity", "wallet", "pass"),
    "Bag / Backpack": ("bag", "backpack", "pouch", "satchel", "purse", "laptop bag"),
    "Umbrella": ("umbrella",),
    "Electronics": ("phone", "mobile", "earphone", "earbud", "airpod", "charger", "laptop", "tablet",
                    "headphone", "power bank", "powerbank", "pendrive", "pen drive", "usb", "watch", "mouse"),
    "Keys": ("key", "keychain", "keyring"),
    "Clothing & Accessories": ("jacket", "hoodie", "shirt", "sweater", "cap", "scarf", "glasses",
                               "spectacles", "sunglasses", "belt", "shoe", "ring", "bracelet", "chain"),
    "Stationery": ("pen", "pencil", "marker", "compass", "geometry", "eraser", "ruler", "highlighter"),
}


def guess_category(*texts):
    words = " ".join(t for t in texts if t).lower()
    best, best_hits = "Other", 0
    for cat, keys in CATEGORY_KEYWORDS.items():
        hits = sum(1 for k in keys if re.search(r"\b" + re.escape(k), words))
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


STORAGE_LOCATIONS = ["Security Office", "Lost & Found Desk", "College Reception", "Department Office"]

STATUSES = ["Lost Reported", "Found Reported", "Potential Match", "Enquiry Started",
            "Deposited at Office", "Ownership Verification Pending", "Ownership Verified",
            "Ready for Handover", "Returned to Owner", "Claim Rejected", "Disputed"]
STATUS_CLASS = {
    "Lost Reported": "b-slate", "Found Reported": "b-blue", "Potential Match": "b-violet",
    "Enquiry Started": "b-blue", "Deposited at Office": "b-navy",
    "Ownership Verification Pending": "b-orange", "Ownership Verified": "b-green-soft",
    "Ready for Handover": "b-green", "Returned to Owner": "b-green-solid",
    "Claim Rejected": "b-red", "Disputed": "b-red-solid",
}
EDITABLE_STATUSES = ("Lost Reported", "Found Reported", "Potential Match", "Enquiry Started")
OPEN_CLAIM_STATUSES = ("Pending Review", "More Info Requested", "Ownership Verified", "Approved")
CLAIM_STATUSES = ("Pending Review", "More Info Requested", "Ownership Verified",
                  "Approved", "Rejected", "Handed Over")

# ---- AI PHOTO DESCRIPTION ---------------------------------------------------
# Set the environment variable ANTHROPIC_API_KEY to let Claude look at the photo
# and write the description. Without a key the app uses a simple built-in
# colour/shape reader so the button still works offline.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
# -----------------------------------------------------------------------------

app = Flask(__name__)


def load_secret_key():
    """Keep the session key in a file so logins survive an app restart."""
    path = os.path.join(INSTANCE_DIR, "secret.key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key


app.config.update(
    SECRET_KEY=load_secret_key(),
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,          # 6 MB upload limit
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

# ----------------------------------------------------------------------------
# 2. DATABASE
# ----------------------------------------------------------------------------
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE,
    usn TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'student' CHECK (role IN ('student','admin')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    color TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    college_logo TEXT DEFAULT 'Not sure',
    distinctive_features TEXT DEFAULT '',
    private_identifying_details TEXT DEFAULT '',
    location TEXT NOT NULL,
    date_reported TEXT NOT NULL,
    image_path TEXT,
    image_hash TEXT,
    report_type TEXT NOT NULL CHECK (report_type IN ('lost','found')),
    reporter_id INTEGER NOT NULL REFERENCES users(id),
    finder_usn TEXT,
    owner_usn TEXT,
    deposit_intent INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ({", ".join("'" + s + "'" for s in STATUSES)})),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lost_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    found_item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    text_score REAL NOT NULL,
    image_score REAL,
    score REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (lost_item_id, found_item_id)
);

CREATE TABLE IF NOT EXISTS enquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL UNIQUE REFERENCES matches(id) ON DELETE CASCADE,
    lost_item_id INTEGER NOT NULL REFERENCES items(id),
    found_item_id INTEGER NOT NULL REFERENCES items(id),
    owner_id INTEGER NOT NULL REFERENCES users(id),
    finder_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','Office Deposit Arranged','Closed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    sender_id INTEGER NOT NULL REFERENCES users(id),
    receiver_id INTEGER NOT NULL REFERENCES users(id),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS office_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
    finder_usn TEXT NOT NULL,
    officer_id TEXT NOT NULL,
    received_date TEXT NOT NULL,
    received_time TEXT NOT NULL,
    storage_location TEXT NOT NULL CHECK (storage_location IN
        ('Security Office','Lost & Found Desk','College Reception','Department Office')),
    status TEXT NOT NULL DEFAULT 'Deposited at Office',
    photo_path TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    lost_item_id INTEGER REFERENCES items(id),
    claimant_id INTEGER NOT NULL REFERENCES users(id),
    claimant_usn TEXT NOT NULL,
    private_details TEXT NOT NULL,
    verification_status TEXT NOT NULL DEFAULT 'Pending Review' CHECK (verification_status IN
        ({", ".join("'" + s + "'" for s in CLAIM_STATUSES)})),
    verification_notes TEXT NOT NULL DEFAULT '',
    officer_id TEXT,
    claim_date TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT UNIQUE,
    item_id INTEGER NOT NULL REFERENCES items(id),
    claim_id INTEGER NOT NULL UNIQUE REFERENCES claims(id),
    finder_usn TEXT,
    receiver_usn TEXT NOT NULL,
    officer_id TEXT NOT NULL,
    handover_date TEXT,
    handover_time TEXT,
    verification_status TEXT NOT NULL,
    final_status TEXT NOT NULL CHECK (final_status IN ('Handover Confirmed','Returned to Owner')),
    confirmed_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
    lost_item_id INTEGER NOT NULL REFERENCES items(id),
    found_item_id INTEGER NOT NULL REFERENCES items(id),
    finder_id INTEGER NOT NULL REFERENCES users(id),
    owner_id INTEGER NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'Awaiting Owner' CHECK (status IN ('Awaiting Owner','Completed','Declined')),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    link TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    is_read INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    claim_id INTEGER REFERENCES claims(id),
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','Resolved')),
    previous_status TEXT NOT NULL,
    opened_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id),
    actor_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_items_type_status ON items(report_type, status);
CREATE INDEX IF NOT EXISTS idx_items_reporter ON items(reporter_id);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read);
CREATE INDEX IF NOT EXISTS idx_msg_enq ON messages(enquiry_id);
CREATE INDEX IF NOT EXISTS idx_msg_recv ON messages(receiver_id, id);
CREATE INDEX IF NOT EXISTS idx_exch_enq ON exchanges(enquiry_id);
CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id);
"""


def schema_is_old(db):
    """True if an older version of the database (different columns) is found."""
    cols = db.execute("PRAGMA table_info(users)").fetchall()
    if not cols:
        return False
    email_not_null = [c for c in cols if c[1] == "email"][0][3]
    item_cols = [c[1] for c in db.execute("PRAGMA table_info(items)")]
    return bool(email_not_null) or "owner_usn" not in item_cols


def init_db():
    """Create tables (if missing) and the one required admin account."""
    db = sqlite3.connect(DB_PATH)
    if schema_is_old(db):
        db.close()
        os.replace(DB_PATH, DB_PATH + ".old")
        print(">> Old database found. It was kept as instance/database.db.old and a fresh one was created.")
        db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    exists = db.execute("SELECT 1 FROM users WHERE role='admin' AND email=?",
                        (ADMIN_USERNAME,)).fetchone()
    if not exists:
        db.execute(
            "INSERT INTO users (name, email, usn, password_hash, role) VALUES (?,?,?,?, 'admin')",
            ("College Administrator", ADMIN_USERNAME, None,
             generate_password_hash(ADMIN_DEFAULT_PASSWORD, method="pbkdf2:sha256")))
        print(">> Admin account created (ID: admin). Change this password before real use!")
    db.commit()
    db.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    return get_db().execute(sql, args).lastrowid


def scalar(sql, args=()):
    row = query(sql, args, one=True)
    return row[0] if row else 0


@app.after_request
def commit_or_rollback(response):
    """Every request is one database transaction: saved if OK, undone on error."""
    db = g.pop("db", None)
    if db is not None:
        if response.status_code < 400:
            db.commit()
        else:
            db.rollback()
        db.close()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ----------------------------------------------------------------------------
# 3. SECURITY HELPERS  (sessions, roles, CSRF, throttling)
# ----------------------------------------------------------------------------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
        if g.user is None:
            session.clear()
    if request.method == "POST":
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("_csrf", "")
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, "Your form expired. Please go back, refresh the page and try again.")


def student_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "student":
            return redirect(url_for("admin_dashboard"))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            return redirect(url_for("admin_login"))
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def any_user_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not g.user:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


_ATTEMPTS = {}


def too_many_attempts(key):
    now = time.time()
    recent = [t for t in _ATTEMPTS.get(key, []) if now - t < 300]
    _ATTEMPTS[key] = recent
    return len(recent) >= 10


def record_failed_attempt(key):
    _ATTEMPTS.setdefault(key, []).append(time.time())


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


def start_session(user):
    session.clear()
    session["uid"] = user["id"]
    session["role"] = user["role"]
    session.permanent = True
    csrf_token()


# ----------------------------------------------------------------------------
# 4. SMALL HELPERS
# ----------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USN_RE = re.compile(r"^[A-Z0-9]{6,15}$")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def notify(user_id, message, link=None):
    execute("INSERT INTO notifications (user_id, message, link) VALUES (?,?,?)",
            (user_id, message, link))


def notify_admins(message, link=None):
    for row in query("SELECT id FROM users WHERE role='admin'"):
        notify(row["id"], message, link)


def notify_reporter(item, message, link=None):
    """Notify the student who reported `item` (admins are skipped)."""
    owner = query("SELECT id, role FROM users WHERE id=?", (item["reporter_id"],), one=True)
    if owner and owner["role"] == "student":
        notify(owner["id"], message, link)


def log_action(item_pk, action, detail=""):
    """action = text students can see. detail = extra info, admin-only."""
    actor = g.user["id"] if g.user else None
    execute("INSERT INTO audit_log (item_id, actor_id, action, detail) VALUES (?,?,?,?)",
            (item_pk, actor, action, detail))


def next_code(prefix, table, column):
    """LF-2026-001 style codes."""
    year = datetime.now().year
    head = f"{prefix}-{year}-"
    last = scalar(f"SELECT MAX(CAST(substr({column}, {len(head) + 1}) AS INTEGER)) "
                  f"FROM {table} WHERE {column} LIKE ?", (head + "%",)) or 0
    return f"{head}{last + 1:03d}"


def set_item_status(item_pk, status):
    assert status in STATUSES
    execute("UPDATE items SET status=?, updated_at=? WHERE id=?", (status, now_str(), item_pk))


def my_matched_lost(found_pk):
    """The current student's own lost reports that the AI matched with this found item."""
    return query("SELECT l.id, l.item_id, l.name FROM matches m JOIN items l ON l.id=m.lost_item_id "
                 "WHERE m.found_item_id=? AND l.reporter_id=? AND l.status!='Returned to Owner' "
                 "ORDER BY m.score DESC", (found_pk, g.user["id"]))


def student_can_view(item):
    """Students may open an item only if it is theirs, matches one of theirs, or they claimed it."""
    uid = g.user["id"]
    if item["reporter_id"] == uid:
        return True
    if query("SELECT 1 FROM matches m JOIN items l ON l.id=m.lost_item_id JOIN items f ON f.id=m.found_item_id "
             "WHERE (m.lost_item_id=? AND f.reporter_id=?) OR (m.found_item_id=? AND l.reporter_id=?)",
             (item["id"], uid, item["id"], uid), one=True):
        return True
    return bool(query("SELECT 1 FROM claims WHERE item_id=? AND claimant_id=?", (item["id"], uid), one=True))


def get_item_or_404(code):
    item = query("SELECT * FROM items WHERE item_id=?", (code,), one=True)
    if not item:
        abort(404)
    return item


def mask(value):
    if not value:
        return ""
    value = str(value)
    if len(value) <= 4:
        return "•" * len(value)
    return value[:2] + "•" * (len(value) - 4) + value[-2:]


@app.template_filter("status_class")
def status_class_filter(status):
    return STATUS_CLASS.get(status, "b-slate")


@app.template_filter("nice_dt")
def nice_dt(value):
    if not value:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(value, fmt)
            return d.strftime("%d %b %Y, %I:%M %p") if fmt != "%Y-%m-%d" else d.strftime("%d %b %Y")
        except ValueError:
            continue
    return value


@app.template_filter("pct")
def pct(value):
    return int(round((value or 0) * 100))


@app.template_filter("score_label")
def score_label(value):
    v = value or 0
    if v >= 0.65:
        return "Strong"
    if v >= 0.40:
        return "Possible"
    return "Weak"


app.jinja_env.filters["mask"] = mask
app.jinja_env.globals["csrf_token"] = csrf_token


@app.context_processor
def inject_globals():
    unread, msg_max = 0, 0
    if g.get("user"):
        unread = scalar("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                        (g.user["id"],))
        if g.user["role"] == "student":
            msg_max = scalar("SELECT COALESCE(MAX(id),0) FROM messages WHERE receiver_id=?", (g.user["id"],))
    return dict(me=g.get("user"), unread_count=unread, msg_max=msg_max, csrf_token=csrf_token,
                CATEGORIES=CATEGORIES, STORAGE_LOCATIONS=STORAGE_LOCATIONS, STATUSES=STATUSES,
                EDITABLE_STATUSES=EDITABLE_STATUSES, today=date.today().isoformat(),
                now_time=datetime.now().strftime("%H:%M"))


def save_image(file_storage):
    """
    Validate and store an uploaded photo.
    Returns (relative_path, image_hash, error_message).
    The picture is re-saved as a clean JPEG, which removes hidden metadata
    and rejects files that only *pretend* to be images.
    """
    if not file_storage or not file_storage.filename:
        return None, None, None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in {"png", "jpg", "jpeg", "webp"}:
        return None, None, "Photos must be PNG, JPG or WEBP files."
    try:
        img = Image.open(file_storage.stream)
        img.verify()
        file_storage.stream.seek(0)
        img = Image.open(file_storage.stream)
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((1280, 1280))
        name = secrets.token_hex(12) + ".jpg"
        img.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=85)
    except Exception:
        return None, None, "That file could not be read as an image. Please try another photo."
    return f"uploads/{name}", compute_image_hash(os.path.join(UPLOAD_DIR, name)), None


# ----------------------------------------------------------------------------
# 5. MATCHING HELPERS
# ----------------------------------------------------------------------------
def refresh_match_status(item_pk):
    """Move an item to / from 'Potential Match' depending on whether matches exist."""
    item = query("SELECT * FROM items WHERE id=?", (item_pk,), one=True)
    if not item:
        return
    count = scalar("SELECT COUNT(*) FROM matches WHERE lost_item_id=? OR found_item_id=?",
                   (item_pk, item_pk))
    if count and item["status"] in ("Lost Reported", "Found Reported"):
        set_item_status(item_pk, "Potential Match")
    elif not count and item["status"] == "Potential Match":
        set_item_status(item_pk, "Lost Reported" if item["report_type"] == "lost" else "Found Reported")


def create_enquiry(match_id, lost, found):
    """Open a chat between the owner (lost report) and the finder (found report)."""
    eid = execute("INSERT INTO enquiries (match_id, lost_item_id, found_item_id, owner_id, finder_id) "
                  "VALUES (?,?,?,?,?)", (match_id, lost["id"], found["id"], lost["reporter_id"], found["reporter_id"]))
    for row in (lost, found):
        cur = query("SELECT status FROM items WHERE id=?", (row["id"],), one=True)
        if cur["status"] in ("Lost Reported", "Found Reported", "Potential Match"):
            set_item_status(row["id"], "Enquiry Started")
        log_action(row["id"], "Chat opened between owner and finder")
    return eid


def recompute_matches(item_pk):
    """
    Compare one item with every report of the opposite type and store the
    results in the `matches` table. Only matches of 20% or more are kept
    (MIN_SCORE in ai/matching.py). When the finder is a student, a chat between
    owner and finder opens automatically and both are notified.
    """
    item = query("SELECT * FROM items WHERE id=?", (item_pk,), one=True)
    if not item or item["status"] == "Returned to Owner":
        return
    kind = item["report_type"]
    other = "found" if kind == "lost" else "lost"
    my_col, other_col = ("lost_item_id", "found_item_id") if kind == "lost" else ("found_item_id", "lost_item_id")

    candidates = query("SELECT * FROM items WHERE report_type=? AND reporter_id!=? "
                       "AND status!='Returned to Owner'", (other, item["reporter_id"]))
    corpus = [dict(r) for r in query("SELECT * FROM items")]
    results = find_matches(dict(item), [dict(c) for c in candidates], corpus)
    by_pk = {c["id"]: c for c in candidates}

    old = {r[other_col]: r["id"] for r in query(
        f"SELECT id, {other_col} FROM matches WHERE {my_col}=?", (item_pk,))}
    affected = set(old.keys())
    keep = set()

    for r in results:
        cid = r["id"]
        keep.add(cid)
        if cid in old:
            execute("UPDATE matches SET text_score=?, image_score=?, score=? WHERE id=?",
                    (r["text_score"], r["image_score"], r["score"], old[cid]))
            continue
        lost_row, found_row = (item, by_pk[cid]) if kind == "lost" else (by_pk[cid], item)
        match_id = execute("INSERT INTO matches (lost_item_id, found_item_id, text_score, image_score, score) "
                           "VALUES (?,?,?,?,?)", (lost_row["id"], found_row["id"], r["text_score"],
                                                  r["image_score"], r["score"]))
        affected.add(cid)
        percent = f"{int(round(r['score'] * 100))}%"
        finder = query("SELECT role FROM users WHERE id=?", (found_row["reporter_id"],), one=True)
        if finder and finder["role"] == "student":
            eid = create_enquiry(match_id, lost_row, found_row)
            notify_reporter(lost_row, f"Match found ({percent}) for your lost item “{lost_row['name']}”. "
                                      f"A chat with the finder is open.", f"/chat/{eid}")
            notify_reporter(found_row, f"Match found ({percent}) for the item you found, “{found_row['name']}”. "
                                       f"A chat with the owner is open.", f"/chat/{eid}")
        else:
            notify_reporter(lost_row, f"Possible match ({percent}) for your lost item “{lost_row['name']}” "
                                      f"is held at the college office.", f"/matches?item={lost_row['item_id']}")
        log_action(item_pk, "AI found a potential match")

    for cid, match_id in old.items():
        if cid not in keep and not query("SELECT 1 FROM enquiries WHERE match_id=?", (match_id,), one=True):
            execute("DELETE FROM matches WHERE id=?", (match_id,))

    for pk in affected | {item_pk}:
        refresh_match_status(pk)


MATCH_SELECT = """
SELECT m.id AS match_id, m.score, m.text_score, m.image_score,
       l.id AS l_pk, l.item_id AS l_code, l.name AS l_name, l.category AS l_cat,
       l.description AS l_desc, l.location AS l_loc, l.image_path AS l_img,
       l.status AS l_status, l.reporter_id AS l_rep, l.date_reported AS l_date,
       f.id AS f_pk, f.item_id AS f_code, f.name AS f_name, f.category AS f_cat,
       f.description AS f_desc, f.location AS f_loc, f.image_path AS f_img,
       f.status AS f_status, f.reporter_id AS f_rep, f.date_reported AS f_date,
       fu.role AS f_role, e.id AS enquiry_id
FROM matches m
JOIN items l ON l.id = m.lost_item_id
JOIN items f ON f.id = m.found_item_id
JOIN users fu ON fu.id = f.reporter_id
LEFT JOIN enquiries e ON e.match_id = m.id
"""


def fetch_matches(user_id=None, item_pk=None, search=None):
    sql, args = MATCH_SELECT + " WHERE 1=1", []
    if user_id is not None:
        sql += " AND (l.reporter_id=? OR f.reporter_id=?)"
        args += [user_id, user_id]
    if item_pk is not None:
        sql += " AND (l.id=? OR f.id=?)"
        args += [item_pk, item_pk]
    if search:
        like = f"%{search}%"
        sql += " AND (l.name LIKE ? OR f.name LIKE ? OR l.item_id LIKE ? OR f.item_id LIKE ?)"
        args += [like, like, like, like]
    sql += " ORDER BY m.score DESC"
    return query(sql, args)


# ----------------------------------------------------------------------------
# 6. PUBLIC & STUDENT PAGES
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


def norm_name(name):
    return " ".join(name.split()).casefold()


@app.route("/user", methods=["GET", "POST"])
def login():
    """Students enter only their name and USN. A new USN creates the account."""
    if g.user:
        return redirect(url_for("dashboard" if g.user["role"] == "student" else "admin_dashboard"))
    if request.method == "POST":
        key = "student:" + (request.remote_addr or "")
        if too_many_attempts(key):
            flash("Too many failed attempts. Please wait a few minutes and try again.", "error")
            return render_template("user_login.html", form=request.form)
        name = " ".join(request.form.get("name", "").split())
        usn = request.form.get("usn", "").strip().upper()
        errors = []
        if len(name) < 2 or len(name) > 80:
            errors.append("Enter your full name (2 to 80 characters).")
        if not USN_RE.match(usn):
            errors.append("USN must be 6 to 15 letters or digits, with no spaces.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("user_login.html", form=request.form)
        user = query("SELECT * FROM users WHERE usn=? AND role='student'", (usn,), one=True)
        created = False
        if user:
            if norm_name(user["name"]) != norm_name(name):
                record_failed_attempt(key)
                flash("This USN is already registered under a different name. "
                      "Enter your name exactly as you did the first time.", "error")
                return render_template("user_login.html", form=request.form)
        else:
            execute("INSERT INTO users (name, email, usn, password_hash, role) VALUES (?,NULL,?,?, 'student')",
                    (name, usn, generate_password_hash(secrets.token_hex(16), method="pbkdf2:sha256")))
            user = query("SELECT * FROM users WHERE usn=?", (usn,), one=True)
            created = True
        nxt = safe_next(request.args.get("next"))
        start_session(user)
        _ATTEMPTS.pop(key, None)
        flash("Account created. Welcome!" if created else f"Welcome back, {user['name']}!", "success")
        return redirect(nxt or url_for("dashboard"))
    return render_template("user_login.html", form={})


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/dashboard")
@student_required
def dashboard():
    uid, usn = g.user["id"], g.user["usn"]
    stats = {
        "total": scalar("SELECT COUNT(*) FROM items WHERE reporter_id=?", (uid,)),
        "lost": scalar("SELECT COUNT(*) FROM items WHERE reporter_id=? AND report_type='lost'", (uid,)),
        "found": scalar("SELECT COUNT(*) FROM items WHERE reporter_id=? AND report_type='found'", (uid,)),
        "matches": scalar("SELECT COUNT(*) FROM matches m JOIN items l ON l.id=m.lost_item_id "
                          "JOIN items f ON f.id=m.found_item_id WHERE l.reporter_id=? OR f.reporter_id=?",
                          (uid, uid)),
        "returned": scalar("SELECT COUNT(DISTINCT i.id) FROM items i WHERE i.status='Returned to Owner' "
                           "AND (i.reporter_id=? OR i.id IN (SELECT item_id FROM claims "
                           "WHERE claimant_id=? AND verification_status='Handed Over'))", (uid, uid)),
    }
    activity = query(
        "SELECT a.action, a.created_at, i.item_id, i.name FROM audit_log a JOIN items i ON i.id=a.item_id "
        "WHERE i.reporter_id=? OR i.id IN (SELECT item_id FROM claims WHERE claimant_id=?) "
        "ORDER BY a.id DESC LIMIT 8", (uid, uid))
    notes = query("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,))
    claims = query("SELECT c.id, c.verification_status, i.item_id, i.name FROM claims c "
                   "JOIN items i ON i.id=c.item_id WHERE c.claimant_id=? ORDER BY c.id DESC LIMIT 5", (uid,))
    return render_template("dashboard.html", stats=stats, activity=activity, notes=notes, claims=claims)


@app.route("/profile", methods=["GET", "POST"])
@any_user_required
def profile():
    if request.method == "POST":
        if g.user["role"] != "admin":
            abort(403)
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash("Your current password is incorrect.", "error")
        elif len(new) < 5:
            flash("New admin password must be at least 5 characters.", "error")
        elif new != confirm:
            flash("New passwords do not match.", "error")
        else:
            execute("UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(new, method="pbkdf2:sha256"), g.user["id"]))
            flash("Password changed.", "success")
        return redirect(url_for("profile"))
    counts = {
        "lost": scalar("SELECT COUNT(*) FROM items WHERE reporter_id=? AND report_type='lost'", (g.user["id"],)),
        "found": scalar("SELECT COUNT(*) FROM items WHERE reporter_id=? AND report_type='found'", (g.user["id"],)),
    }
    return render_template("profile.html", counts=counts)


# ----------------------------------------------------------------------------
# 7. REPORTING AND LISTING ITEMS
# ----------------------------------------------------------------------------
def parse_item_form(existing_category=None):
    f = request.form
    data = {k: f.get(k, "").strip() for k in
            ("name", "description", "color", "brand", "college_logo", "location",
             "date_reported", "distinctive_features", "private_identifying_details")}
    errors = []
    if not data["name"] or len(data["name"]) > 80:
        errors.append("Item name is required (up to 80 characters).")
    if len(data["description"]) < 10:
        errors.append("Describe the item in at least 10 characters.")
    if data["college_logo"] not in ("Yes", "No", "Not sure"):
        data["college_logo"] = "Not sure"
    if not data["location"] or len(data["location"]) > 120:
        errors.append("Location is required (up to 120 characters).")
    try:
        d = date.fromisoformat(data["date_reported"])
        if d > date.today():
            errors.append("The date cannot be in the future.")
    except ValueError:
        errors.append("Enter a valid date.")
    for field, limit in (("description", 1000), ("distinctive_features", 500),
                         ("private_identifying_details", 500), ("color", 40), ("brand", 60)):
        if len(data[field]) > limit:
            errors.append(f"{field.replace('_', ' ').capitalize()} is too long (max {limit} characters).")
    guessed = guess_category(data["name"], data["brand"], data["description"], data["distinctive_features"])
    data["category"] = guessed if guessed != "Other" else (existing_category or "Other")
    return data, errors


@app.route("/report/<kind>", methods=["GET", "POST"])
@student_required
def report(kind):
    if kind not in ("lost", "found"):
        abort(404)
    if request.method == "POST":
        data, errors = parse_item_form()
        image_path, image_hash, img_err = save_image(request.files.get("image"))
        if img_err:
            errors.append(img_err)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("report_item.html", kind=kind, item=None, form=request.form)
        deposit = 1 if (kind == "found" and request.form.get("deposited") == "on") else 0
        status = "Lost Reported" if kind == "lost" else "Found Reported"
        finder_usn = g.user["usn"] if kind == "found" else None
        pk, code = None, None
        for _ in range(5):
            code = next_code("LF", "items", "item_id")
            try:
                pk = execute(
                    "INSERT INTO items (item_id, name, category, description, color, brand, college_logo, "
                    "distinctive_features, private_identifying_details, location, date_reported, image_path, "
                    "image_hash, report_type, reporter_id, finder_usn, owner_usn, deposit_intent, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (code, data["name"], data["category"], data["description"], data["color"], data["brand"],
                     data["college_logo"], data["distinctive_features"], data["private_identifying_details"],
                     data["location"], data["date_reported"], image_path, image_hash, kind, g.user["id"],
                     finder_usn, g.user["usn"] if kind == "lost" else None, deposit, status))
                break
            except sqlite3.IntegrityError:
                continue
        if pk is None:
            abort(500)
        log_action(pk, f"{kind.capitalize()} item reported")
        if deposit:
            notify_admins(f"Finder says item {code} was deposited at the college office. Please register it.",
                          f"/admin/office?item_id={code}")
        recompute_matches(pk)
        flash(f"Report submitted. Your Item ID is {code}.", "success")
        return redirect(url_for("item_details", code=code))
    return render_template("report_item.html", kind=kind, item=None, form={})


@app.route("/item/<code>/edit", methods=["GET", "POST"])
@student_required
def edit_item(code):
    item = get_item_or_404(code)
    if item["reporter_id"] != g.user["id"]:
        abort(403)
    if item["status"] not in EDITABLE_STATUSES:
        flash("This report can no longer be edited because it is already with the college office.", "error")
        return redirect(url_for("item_details", code=code))
    if request.method == "POST":
        data, errors = parse_item_form(item["category"])
        image_path, image_hash, img_err = save_image(request.files.get("image"))
        if img_err:
            errors.append(img_err)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("report_item.html", kind=item["report_type"], item=item, form=request.form)
        deposit = item["deposit_intent"]
        if item["report_type"] == "found":
            deposit = 1 if request.form.get("deposited") == "on" else 0
        execute("UPDATE items SET name=?, category=?, description=?, color=?, brand=?, college_logo=?, "
                "distinctive_features=?, private_identifying_details=?, location=?, date_reported=?, "
                "image_path=COALESCE(?, image_path), image_hash=COALESCE(?, image_hash), "
                "deposit_intent=?, updated_at=? WHERE id=?",
                (data["name"], data["category"], data["description"], data["color"], data["brand"],
                 data["college_logo"], data["distinctive_features"], data["private_identifying_details"],
                 data["location"], data["date_reported"], image_path, image_hash, deposit, now_str(), item["id"]))
        log_action(item["id"], "Report updated")
        recompute_matches(item["id"])
        flash("Report updated and matches recalculated.", "success")
        return redirect(url_for("item_details", code=code))
    return render_template("report_item.html", kind=item["report_type"], item=item, form=dict(item))


def filtered_items(args, reporter_id=None):
    sql = ("SELECT i.*, u.name AS reporter_name, o.storage_location AS office_location "
           "FROM items i JOIN users u ON u.id=i.reporter_id "
           "LEFT JOIN office_items o ON o.item_id=i.id WHERE 1=1")
    params = []
    if reporter_id is not None:
        sql += " AND i.reporter_id=?"
        params.append(reporter_id)
    q = args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        sql += (" AND (i.name LIKE ? OR i.category LIKE ? OR i.location LIKE ? OR i.item_id LIKE ? "
                "OR i.brand LIKE ? OR i.color LIKE ?)")
        params += [like] * 6
    if args.get("type") in ("lost", "found"):
        sql += " AND i.report_type=?"
        params.append(args["type"])
    if args.get("status") in STATUSES:
        sql += " AND i.status=?"
        params.append(args["status"])
    if args.get("category") in CATEGORIES:
        sql += " AND i.category=?"
        params.append(args["category"])
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        val = args.get(key, "")
        try:
            date.fromisoformat(val)
            sql += f" AND i.date_reported {op} ?"
            params.append(val)
        except ValueError:
            pass
    sql += " ORDER BY i.id ASC" if args.get("sort") == "oldest" else " ORDER BY i.id DESC"
    return query(sql + " LIMIT 300", params)


@app.route("/my-reports")
@student_required
def my_reports():
    items = filtered_items(request.args, reporter_id=g.user["id"])
    return render_template("item_list.html", items=items, mine=True, args=request.args)


def features_visible(item):
    """Distinctive features of FOUND items stay hidden so nobody can copy them into a false claim."""
    if g.user["role"] == "admin" or g.user["id"] == item["reporter_id"]:
        return True
    if item["report_type"] == "lost":
        return True
    return False


@app.route("/item/<code>")
@any_user_required
def item_details(code):
    item = get_item_or_404(code)
    is_admin = g.user["role"] == "admin"
    is_owner_of_report = g.user["id"] == item["reporter_id"]
    if not is_admin and not student_can_view(item):
        abort(404)   # not matched with this student's items, so it stays hidden
    office = query("SELECT * FROM office_items WHERE item_id=?", (item["id"],), one=True)
    reporter = query("SELECT name, usn FROM users WHERE id=?", (item["reporter_id"],), one=True)
    matches = []
    if is_admin or is_owner_of_report:
        matches = fetch_matches(item_pk=item["id"])
    my_claim = None
    if not is_admin:
        my_claim = query("SELECT * FROM claims WHERE item_id=? AND claimant_id=? ORDER BY id DESC LIMIT 1",
                         (item["id"], g.user["id"]), one=True)
    can_claim = (not is_admin and item["report_type"] == "found" and not is_owner_of_report
                 and bool(my_matched_lost(item["id"]))
                 and item["status"] in ("Deposited at Office", "Claim Rejected", "Ownership Verification Pending")
                 and (my_claim is None or my_claim["verification_status"] == "Rejected"))
    history = query("SELECT action, detail, created_at FROM audit_log WHERE item_id=? ORDER BY id DESC", (item["id"],)) \
        if (is_admin or is_owner_of_report) else []
    claims = query("SELECT c.*, u.name AS claimant_name FROM claims c JOIN users u ON u.id=c.claimant_id "
                   "WHERE c.item_id=? ORDER BY c.id DESC", (item["id"],)) if is_admin else []
    return render_template("item_details.html", item=item, office=office, reporter=reporter, matches=matches,
                           can_claim=can_claim, my_claim=my_claim, history=history, claims=claims,
                           show_features=features_visible(item), is_admin=is_admin,
                           is_reporter=is_owner_of_report,
                           can_edit=is_owner_of_report and item["status"] in EDITABLE_STATUSES)


# ----------------------------------------------------------------------------
# 8. AI MATCH RESULTS AND ENQUIRIES
# ----------------------------------------------------------------------------
@app.route("/matches")
@student_required
def matches_page():
    code = request.args.get("item", "").strip()
    search = request.args.get("q", "").strip()
    item_pk = None
    if code:
        row = query("SELECT id FROM items WHERE item_id=? AND reporter_id=?", (code, g.user["id"]), one=True)
        item_pk = row["id"] if row else -1
    rows = fetch_matches(user_id=g.user["id"], item_pk=item_pk, search=search)
    groups = {}
    for r in rows:
        mine_is_lost = r["l_rep"] == g.user["id"]
        key = r["l_code"] if mine_is_lost else r["f_code"]
        grp = groups.setdefault(key, {"mine_is_lost": mine_is_lost, "rows": [],
                                      "name": r["l_name"] if mine_is_lost else r["f_name"], "code": key})
        grp["rows"].append(r)
    return render_template("matches.html", groups=list(groups.values()), q=search, code=code)


@app.route("/enquiry/start", methods=["POST"])
@student_required
def start_enquiry():
    """Backup route: chats normally open automatically when a match is found."""
    try:
        match_id = int(request.form.get("match_id", ""))
    except ValueError:
        abort(400)
    m = query(MATCH_SELECT + " WHERE m.id=?", (match_id,), one=True)
    if not m or g.user["id"] not in (m["l_rep"], m["f_rep"]):
        abort(403)
    if m["score"] < MIN_SCORE:
        flash("A chat opens only for matches of 20% or more.", "error")
        return redirect(url_for("matches_page"))
    if m["f_role"] == "admin":
        flash("This item is already at the college office. Submit a claim so staff can verify ownership.", "error")
        return redirect(url_for("matches_page"))
    enquiry = query("SELECT id FROM enquiries WHERE match_id=?", (match_id,), one=True)
    if enquiry:
        return redirect(url_for("chat", enquiry_id=enquiry["id"]))
    lost = query("SELECT * FROM items WHERE id=?", (m["l_pk"],), one=True)
    found = query("SELECT * FROM items WHERE id=?", (m["f_pk"],), one=True)
    return redirect(url_for("chat", enquiry_id=create_enquiry(match_id, lost, found)))


# ----------------------------------------------------------------------------
# 9. CHAT
# ----------------------------------------------------------------------------
ENQUIRY_SELECT = """
SELECT e.*, l.item_id AS l_code, l.name AS l_name, l.description AS l_desc, l.location AS l_loc,
       l.image_path AS l_img, f.item_id AS f_code, f.name AS f_name, f.description AS f_desc,
       f.location AS f_loc, f.image_path AS f_img, f.status AS f_status,
       ou.name AS owner_name, ou.usn AS owner_usn, fu.name AS finder_name, fu.usn AS finder_usn
FROM enquiries e
JOIN items l ON l.id=e.lost_item_id JOIN items f ON f.id=e.found_item_id
JOIN users ou ON ou.id=e.owner_id JOIN users fu ON fu.id=e.finder_id
"""


def get_enquiry_for_user(enquiry_id):
    e = query(ENQUIRY_SELECT + " WHERE e.id=?", (enquiry_id,), one=True)
    if not e or g.user["id"] not in (e["owner_id"], e["finder_id"]):
        abort(403)
    return e


@app.route("/chat")
@app.route("/chat/<int:enquiry_id>")
@student_required
def chat(enquiry_id=None):
    uid = g.user["id"]
    convs = query(ENQUIRY_SELECT + " WHERE e.owner_id=? OR e.finder_id=? ORDER BY e.id DESC", (uid, uid))
    current, msgs = None, []
    if enquiry_id:
        current = get_enquiry_for_user(enquiry_id)
        msgs = query("SELECT * FROM messages WHERE enquiry_id=? ORDER BY id", (enquiry_id,))
        execute("UPDATE notifications SET is_read=1 WHERE user_id=? AND link=?", (uid, f"/chat/{enquiry_id}"))
    exchange = (query("SELECT * FROM exchanges WHERE enquiry_id=? ORDER BY id DESC LIMIT 1", (enquiry_id,), one=True)
                if current else None)
    return render_template("chat.html", convs=convs, current=current, msgs=msgs, exchange=exchange)


def add_message(enquiry, text):
    uid = g.user["id"]
    receiver = enquiry["finder_id"] if uid == enquiry["owner_id"] else enquiry["owner_id"]
    mid = execute("INSERT INTO messages (enquiry_id, item_id, sender_id, receiver_id, message) VALUES (?,?,?,?,?)",
                  (enquiry["id"], enquiry["found_item_id"], uid, receiver, text))
    link = f"/chat/{enquiry['id']}"
    if not query("SELECT 1 FROM notifications WHERE user_id=? AND link=? AND is_read=0", (receiver, link), one=True):
        notify(receiver, f"New message from {g.user['name']} about “{enquiry['f_name']}”.", link)
    return mid


@app.route("/chat/<int:enquiry_id>/send", methods=["POST"])
@student_required
def chat_send(enquiry_id):
    enquiry = get_enquiry_for_user(enquiry_id)
    text = (request.form.get("message") or (request.get_json(silent=True) or {}).get("message", "")).strip()
    wants_json = request.headers.get("X-Requested-With") == "fetch"
    if not text or len(text) > 1000:
        if wants_json:
            return jsonify(error="Message must be 1 to 1000 characters."), 400
        flash("Message must be 1 to 1000 characters.", "error")
        return redirect(url_for("chat", enquiry_id=enquiry_id))
    mid = add_message(enquiry, text)
    if wants_json:
        row = query("SELECT * FROM messages WHERE id=?", (mid,), one=True)
        return jsonify(id=mid, text=row["message"], time=nice_dt(row["created_at"]), mine=True)
    return redirect(url_for("chat", enquiry_id=enquiry_id))


@app.route("/chat/<int:enquiry_id>/poll")
@student_required
def chat_poll(enquiry_id):
    get_enquiry_for_user(enquiry_id)
    after = request.args.get("after", 0, type=int)
    rows = query("SELECT * FROM messages WHERE enquiry_id=? AND id>? ORDER BY id", (enquiry_id, after))
    return jsonify([{"id": r["id"], "text": r["message"], "time": nice_dt(r["created_at"]),
                     "mine": r["sender_id"] == g.user["id"]} for r in rows])


@app.route("/chat/<int:enquiry_id>/office", methods=["POST"])
@student_required
def chat_office(enquiry_id):
    e = get_enquiry_for_user(enquiry_id)
    if g.user["id"] == e["finder_id"]:
        add_message(e, "I will deposit the item at the college office so staff can verify ownership.")
        execute("UPDATE items SET deposit_intent=1 WHERE id=?", (e["found_item_id"],))
        execute("UPDATE enquiries SET status='Office Deposit Arranged' WHERE id=?", (e["id"],))
        notify_admins(f"Finder plans to deposit item {e['f_code']} at the office.", f"/admin/office?item_id={e['f_code']}")
        log_action(e["found_item_id"], "Finder arranged to deposit the item at the college office")
        flash("Marked as: finder will deposit the item at the college office.", "success")
    else:
        add_message(e, "Please deposit the item at the college office so staff can verify ownership. Thank you!")
        flash("Request sent to the finder.", "success")
    return redirect(url_for("chat", enquiry_id=enquiry_id))


# ---- Direct exchange: finder hands the item to the owner, owner confirms -------
def exchange_blockers(e):
    """Reason why a direct finder-to-owner exchange is not possible, or None."""
    if e["status"] != "Open":
        return "This enquiry is closed or already arranged through the college office."
    lost = query("SELECT status FROM items WHERE id=?", (e["lost_item_id"],), one=True)
    found = query("SELECT status FROM items WHERE id=?", (e["found_item_id"],), one=True)
    if lost["status"] == "Returned to Owner" or found["status"] == "Returned to Owner":
        return "This item has already been returned."
    if found["status"] not in ("Found Reported", "Potential Match", "Enquiry Started"):
        return "This item is already with the college office. Please use a claim instead."
    return None


@app.route("/chat/<int:enquiry_id>/exchange/<action>", methods=["POST"])
@student_required
def chat_exchange(enquiry_id, action):
    if action not in ("handed", "confirm", "decline"):
        abort(404)
    e = get_enquiry_for_user(enquiry_id)
    back = redirect(url_for("chat", enquiry_id=enquiry_id))
    uid = g.user["id"]
    pending = query("SELECT * FROM exchanges WHERE enquiry_id=? AND status='Awaiting Owner' ORDER BY id DESC LIMIT 1",
                    (enquiry_id,), one=True)

    if action == "handed":
        if uid != e["finder_id"]:
            abort(403)
        problem = exchange_blockers(e)
        if problem:
            flash(problem, "error")
            return back
        if pending:
            flash("You already told the owner you handed over the item. Waiting for their confirmation.", "error")
            return back
        execute("INSERT INTO exchanges (enquiry_id, lost_item_id, found_item_id, finder_id, owner_id) VALUES (?,?,?,?,?)",
                (enquiry_id, e["lost_item_id"], e["found_item_id"], e["finder_id"], e["owner_id"]))
        add_message(e, "I have handed the item over to you. Please confirm once you have it in your hands.")
        notify(e["owner_id"], f"{g.user['name']} says they handed over “{e['f_name']}”. Please confirm that you received it.",
               f"/chat/{enquiry_id}")
        log_action(e["found_item_id"], "Finder handed the item to the owner. Waiting for owner confirmation")
        flash("The owner was asked to confirm that they received the item.", "success")

    elif action == "confirm":
        if uid != e["owner_id"]:
            abort(403)
        if not pending:
            flash("There is no handover waiting for your confirmation.", "error")
            return back
        if request.form.get("physical") != "on":
            flash("Tick the box to confirm you really have the item.", "error")
            return back
        problem = exchange_blockers(e)
        if problem:
            flash(problem, "error")
            return back
        execute("UPDATE exchanges SET status='Completed', completed_at=? WHERE id=?", (now_str(), pending["id"]))
        for pk in (e["lost_item_id"], e["found_item_id"]):
            set_item_status(pk, "Returned to Owner")
        add_message(e, "I received the item. Thank you!")
        # every other chat about these two items is now finished
        execute("UPDATE enquiries SET status='Closed' WHERE lost_item_id=? OR found_item_id=? OR id=?",
                (e["lost_item_id"], e["found_item_id"], enquiry_id))
        log_action(e["found_item_id"], "Item exchanged directly between finder and owner. Returned to owner")
        log_action(e["lost_item_id"], "Item returned to owner by the finder")
        notify(e["finder_id"], f"{g.user['name']} confirmed they received “{e['f_name']}”. Thank you for returning it!",
               f"/item/{e['f_code']}")
        flash("Item marked as returned. Thank you!", "success")

    else:  # decline
        if uid != e["owner_id"]:
            abort(403)
        if not pending:
            flash("There is no handover waiting for your confirmation.", "error")
            return back
        execute("UPDATE exchanges SET status='Declined', completed_at=? WHERE id=?", (now_str(), pending["id"]))
        add_message(e, "I have not received the item yet.")
        notify(e["finder_id"], f"{g.user['name']} says they have not received “{e['f_name']}” yet.", f"/chat/{enquiry_id}")
        log_action(e["found_item_id"], "Owner said the item was not received yet")
        flash("The finder was told that you have not received the item.", "success")
    return back


# ---- Pop-up alerts for new chat messages -----------------------------------------
@app.route("/api/new-messages")
@student_required
def api_new_messages():
    after = request.args.get("after", 0, type=int)
    rows = query("SELECT m.id, m.enquiry_id, m.message, u.name AS sender, i.name AS item_name "
                 "FROM messages m JOIN users u ON u.id=m.sender_id JOIN items i ON i.id=m.item_id "
                 "WHERE m.receiver_id=? AND m.id>? ORDER BY m.id LIMIT 20", (g.user["id"], after))
    latest = scalar("SELECT COALESCE(MAX(id),0) FROM messages WHERE receiver_id=?", (g.user["id"],))
    return jsonify(latest=latest, messages=[
        {"id": r["id"], "enquiry_id": r["enquiry_id"], "sender": r["sender"], "item": r["item_name"],
         "text": r["message"][:140]} for r in rows])


# ---- AI description from a photo ------------------------------------------------
COLOR_NAMES = [
    ("black", (25, 25, 25)), ("dark grey", (85, 85, 90)), ("grey", (150, 150, 155)), ("silver", (200, 200, 205)),
    ("white", (240, 240, 240)), ("red", (200, 40, 40)), ("maroon", (110, 25, 40)), ("orange", (235, 130, 40)),
    ("yellow", (235, 210, 60)), ("green", (60, 160, 80)), ("dark green", (25, 90, 55)),
    ("teal", (30, 150, 150)), ("light blue", (120, 180, 230)), ("blue", (50, 100, 205)),
    ("navy blue", (25, 40, 100)), ("purple", (120, 70, 170)), ("pink", (235, 140, 180)),
    ("brown", (110, 70, 40)), ("beige", (210, 190, 150)),
]


def nearest_color(rgb):
    return min(COLOR_NAMES, key=lambda c: sum((a - b) ** 2 for a, b in zip(c[1], rgb)))[0]


def describe_locally(img):
    """Offline fallback: reads colours and shape only (it cannot recognise objects)."""
    small = ImageOps.fit(img, (120, 120))
    # the centre of the picture is usually the item, the edges the background
    centre = small.crop((30, 30, 90, 90)).quantize(colors=4).convert("RGB")
    counts = sorted(centre.getcolors(), reverse=True)
    main = nearest_color(counts[0][1])
    second = next((nearest_color(c[1]) for c in counts[1:] if nearest_color(c[1]) != main), None)
    w, h = img.size
    ratio = h / w if w else 1
    shape = "tall" if ratio > 1.35 else ("wide" if ratio < 0.74 else "compact")
    desc = f"A {shape}, {main} item"
    if second:
        desc += f" with {second} parts"
    desc += ". Please add what it is, its size and any marks you can see."
    return {"name": "", "color": main.title(), "brand": "", "description": desc,
            "distinctive_features": "", "source": "local"}


def describe_with_claude(img):
    """Ask Claude to look at the photo. Returns a dict or None if anything goes wrong."""
    if not ANTHROPIC_API_KEY:
        return None
    buf = io.BytesIO()
    pic = img.copy()
    pic.thumbnail((900, 900))
    pic.save(buf, "JPEG", quality=80)
    prompt = (
        "You help a college lost-and-found desk. Look at this photo of one item and reply with ONLY a JSON object "
        'with these string keys: "name" (short item name, max 6 words), "color" (main colours), '
        '"brand" (only if clearly visible, else ""), "description" (2 plain sentences: what it is, size/shape, '
        'material, condition), "distinctive_features" (visible stickers, scratches, dents, logos, text; '
        'else ""). Describe only what you can see. Do not guess names, numbers or personal details.')
    body = json.dumps({
        "model": ANTHROPIC_MODEL, "max_tokens": 400,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(buf.getvalue()).decode()}},
            {"type": "text", "text": prompt}]}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            payload = json.loads(resp.read().decode())
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0))
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, AttributeError, OSError):
        return None
    out = {k: str(data.get(k, "") or "").strip() for k in
           ("name", "color", "brand", "description", "distinctive_features")}
    if len(out["description"]) < 10:
        return None
    out.update(name=out["name"][:80], color=out["color"][:40], brand=out["brand"][:60],
               description=out["description"][:1000], distinctive_features=out["distinctive_features"][:500],
               source="claude")
    return out


@app.route("/api/describe-photo", methods=["POST"])
@student_required
def api_describe_photo():
    key = f"describe:{g.user['id']}"
    if too_many_attempts(key):
        return jsonify(error="Too many photo requests. Please wait a few minutes."), 429
    record_failed_attempt(key)   # counts every request; 10 per 5 minutes is plenty for a form
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify(error="No photo received."), 400
    try:
        img = Image.open(file.stream)
        img.verify()
        file.stream.seek(0)
        img = ImageOps.exif_transpose(Image.open(file.stream)).convert("RGB")
    except Exception:
        return jsonify(error="That file could not be read as an image."), 400
    result = describe_with_claude(img) or describe_locally(img)
    return jsonify(result)


# ----------------------------------------------------------------------------
# 10. NOTIFICATIONS AND CLAIMS (student side)
# ----------------------------------------------------------------------------
@app.route("/notifications")
@any_user_required
def notifications():
    rows = query("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100", (g.user["id"],))
    return render_template("notifications.html", notes=rows)


@app.route("/notifications/read", methods=["POST"])
@any_user_required
def notifications_read():
    nid = request.form.get("id")
    if nid == "all":
        execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.user["id"],))
        return redirect(url_for("notifications"))
    row = query("SELECT * FROM notifications WHERE id=? AND user_id=?", (nid, g.user["id"]), one=True)
    if not row:
        abort(404)
    execute("UPDATE notifications SET is_read=1 WHERE id=?", (row["id"],))
    return redirect(safe_next(row["link"]) or url_for("notifications"))


@app.route("/claim/<code>", methods=["GET", "POST"])
@student_required
def claim_item(code):
    item = get_item_or_404(code)
    if item["report_type"] != "found" or item["reporter_id"] == g.user["id"]:
        abort(403)
    if not my_matched_lost(item["id"]):
        abort(404)   # only a student whose lost report matches this item can claim it
    if item["status"] not in ("Deposited at Office", "Claim Rejected", "Ownership Verification Pending"):
        flash("This item is not at the college office yet. Chat with the finder to arrange a deposit first.", "error")
        return redirect(url_for("item_details", code=code))
    existing = query("SELECT * FROM claims WHERE item_id=? AND claimant_id=? AND verification_status!='Rejected'",
                     (item["id"], g.user["id"]), one=True)
    if existing:
        return redirect(url_for("claim_status", claim_id=existing["id"]))
    my_lost = my_matched_lost(item["id"])
    if request.method == "POST":
        details = request.form.get("private_details", "").strip()
        lost_pk = request.form.get("lost_item_id") or None
        if str(lost_pk) not in {str(r["id"]) for r in my_lost}:
            flash("Choose which of your lost reports this item matches.", "error")
            return render_template("claim_form.html", item=item, my_lost=my_lost, claim=None, form=request.form)
        if len(details) < 15 or len(details) > 1000:
            flash("Describe private details only the owner would know (15 to 1000 characters).", "error")
            return render_template("claim_form.html", item=item, my_lost=my_lost, claim=None, form=request.form)
        cid = execute("INSERT INTO claims (item_id, lost_item_id, claimant_id, claimant_usn, private_details) "
                      "VALUES (?,?,?,?,?)", (item["id"], lost_pk, g.user["id"], g.user["usn"], details))
        if item["status"] in ("Deposited at Office", "Claim Rejected"):
            set_item_status(item["id"], "Ownership Verification Pending")
        log_action(item["id"], "Ownership claim submitted")
        notify(g.user["id"], f"Your claim for {code} was submitted. College staff will review it.", f"/claims/{cid}")
        notify_admins(f"New ownership claim for item {code}.", f"/admin/verification/{cid}")
        flash("Claim submitted. College staff will contact you for verification.", "success")
        return redirect(url_for("claim_status", claim_id=cid))
    return render_template("claim_form.html", item=item, my_lost=my_lost, claim=None, form={})


@app.route("/claims/<int:claim_id>", methods=["GET", "POST"])
@student_required
def claim_status(claim_id):
    claim = query("SELECT c.*, i.item_id AS code, i.name AS item_name, i.description AS item_desc, "
                  "i.image_path, o.storage_location FROM claims c JOIN items i ON i.id=c.item_id "
                  "LEFT JOIN office_items o ON o.item_id=i.id WHERE c.id=?", (claim_id,), one=True)
    if not claim or claim["claimant_id"] != g.user["id"]:
        abort(403)
    if request.method == "POST":
        extra = request.form.get("more_details", "").strip()
        if claim["verification_status"] not in ("More Info Requested", "Pending Review"):
            flash("You can add details only while the claim is under review.", "error")
        elif len(extra) < 10 or len(extra) > 1000:
            flash("Add 10 to 1000 characters of identifying details.", "error")
        else:
            merged = (claim["private_details"] + f"\n\n[Added {now_str()}] " + extra)[:3000]
            execute("UPDATE claims SET private_details=?, verification_status='Pending Review' WHERE id=?",
                    (merged, claim_id))
            notify_admins(f"Claimant added verification details for item {claim['code']}.",
                          f"/admin/verification/{claim_id}")
            flash("Details sent to college staff.", "success")
        return redirect(url_for("claim_status", claim_id=claim_id))
    return render_template("claim_form.html", item=None, my_lost=[], claim=claim, form={})


# ----------------------------------------------------------------------------
# 11. ADMIN
# ----------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if g.user and g.user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        key = "admin:" + (request.remote_addr or "")
        if too_many_attempts(key):
            flash("Too many failed attempts. Please wait a few minutes and try again.", "error")
            return render_template("admin_login.html")
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = query("SELECT * FROM users WHERE email=? AND role='admin'", (username,), one=True)
        if user and check_password_hash(user["password_hash"], password):
            start_session(user)
            _ATTEMPTS.pop(key, None)
            return redirect(url_for("admin_dashboard"))
        record_failed_attempt(key)
        flash("Incorrect username or password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    flash("Admin session ended.", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    s = {
        "total": scalar("SELECT COUNT(*) FROM items"),
        "lost": scalar("SELECT COUNT(*) FROM items WHERE report_type='lost'"),
        "found": scalar("SELECT COUNT(*) FROM items WHERE report_type='found'"),
        "at_office": scalar("SELECT COUNT(*) FROM items WHERE status IN ('Deposited at Office',"
                            "'Ownership Verification Pending','Ownership Verified','Ready for Handover','Claim Rejected')"),
        "pending_claims": scalar("SELECT COUNT(*) FROM claims WHERE verification_status IN "
                                 "('Pending Review','More Info Requested')"),
        "ready": scalar("SELECT COUNT(*) FROM claims c WHERE verification_status='Approved'"),
        "returned": scalar("SELECT COUNT(*) FROM items WHERE report_type='found' AND status='Returned to Owner'"),
        "disputed": scalar("SELECT COUNT(*) FROM disputes WHERE status='Open'"),
        "matches": scalar("SELECT COUNT(*) FROM matches"),
        "students": scalar("SELECT COUNT(*) FROM users WHERE role='student'"),
    }
    awaiting = query("SELECT item_id, name, location, date_reported FROM items WHERE report_type='found' "
                     "AND deposit_intent=1 AND status IN ('Found Reported','Potential Match','Enquiry Started') "
                     "ORDER BY id DESC LIMIT 6")
    pending = query("SELECT c.id, c.verification_status, c.claim_date, i.item_id, i.name FROM claims c "
                    "JOIN items i ON i.id=c.item_id WHERE c.verification_status IN "
                    "('Pending Review','More Info Requested') ORDER BY c.id DESC LIMIT 6")
    activity = query("SELECT a.action, a.created_at, i.item_id FROM audit_log a LEFT JOIN items i ON i.id=a.item_id "
                     "ORDER BY a.id DESC LIMIT 8")
    return render_template("admin_dashboard.html", s=s, awaiting=awaiting, pending=pending, activity=activity)


@app.route("/admin/items")
@admin_required
def admin_items():
    items = filtered_items(request.args)
    return render_template("admin_items.html", items=items, args=request.args)


@app.route("/admin/matches")
@admin_required
def admin_matches():
    search = request.args.get("q", "").strip()
    return render_template("admin_matches.html", rows=fetch_matches(search=search), q=search)


@app.route("/admin/matches/recalculate", methods=["POST"])
@admin_required
def admin_recalculate():
    for row in query("SELECT id FROM items WHERE status!='Returned to Owner'"):
        recompute_matches(row["id"])
    flash("All AI matches were recalculated.", "success")
    return redirect(url_for("admin_matches"))


@app.route("/admin/office", methods=["GET", "POST"])
@admin_required
def office_register():
    prefill = {}
    code = request.values.get("item_id", "").strip()
    pre_item = query("SELECT * FROM items WHERE item_id=?", (code,), one=True) if code else None
    if pre_item:
        prefill = {"item_id": pre_item["item_id"], "finder_usn": pre_item["finder_usn"] or ""}
    if request.method == "POST":
        f = request.form
        errors = []
        officer_id = f.get("officer_id", "").strip().upper()
        finder_usn = f.get("finder_usn", "").strip().upper()
        storage = f.get("storage_location", "")
        received_date, received_time = f.get("received_date", ""), f.get("received_time", "")
        if not re.match(r"^[A-Z0-9\-]{2,20}$", officer_id):
            errors.append("Enter a valid Officer ID (letters, digits or hyphens).")
        if not USN_RE.match(finder_usn):
            errors.append("Enter the finder's USN (6 to 15 letters or digits).")
        if storage not in STORAGE_LOCATIONS:
            errors.append("Choose a storage location.")
        try:
            date.fromisoformat(received_date)
            datetime.strptime(received_time, "%H:%M")
        except ValueError:
            errors.append("Enter a valid received date and time.")
        photo_path, photo_hash, img_err = save_image(request.files.get("photo"))
        if img_err:
            errors.append(img_err)

        item, new_item = None, False
        if code:
            item = query("SELECT * FROM items WHERE item_id=?", (code,), one=True)
            if not item or item["report_type"] != "found":
                errors.append("Item ID must belong to a FOUND report.")
            elif item["status"] not in ("Found Reported", "Potential Match", "Enquiry Started"):
                errors.append(f"Item {code} is already '{item['status']}' and cannot be registered again.")
            elif query("SELECT 1 FROM office_items WHERE item_id=?", (item["id"],), one=True):
                errors.append(f"Item {code} is already registered at the office.")
        else:
            new_item = True
            name = f.get("item_name", "").strip()
            description = f.get("item_description", "").strip()
            category = f.get("category", "")
            if not name or len(description) < 10 or category not in CATEGORIES:
                errors.append("For an item with no online report, enter name, category and description "
                              "(at least 10 characters).")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("office_register.html", prefill=request.form, recent=_recent_office())

        if new_item:
            pk = None
            for _ in range(5):
                new_code = next_code("LF", "items", "item_id")
                try:
                    pk = execute(
                        "INSERT INTO items (item_id, name, category, description, color, brand, location, "
                        "date_reported, image_path, image_hash, report_type, reporter_id, finder_usn, "
                        "deposit_intent, status, distinctive_features) VALUES (?,?,?,?,?,?,?,?,?,?, 'found', ?,?,1,"
                        "'Deposited at Office', ?)",
                        (new_code, name, category, description, f.get("color", "").strip(), f.get("brand", "").strip(),
                         storage, received_date, photo_path, photo_hash, g.user["id"], finder_usn,
                         f.get("distinctive_features", "").strip()))
                    break
                except sqlite3.IntegrityError:
                    continue
            if pk is None:
                abort(500)
            code = new_code
        else:
            pk = item["id"]
            set_item_status(pk, "Deposited at Office")
            execute("UPDATE items SET finder_usn=?, deposit_intent=1 WHERE id=?", (finder_usn, pk))
            if photo_path and not item["image_path"]:
                execute("UPDATE items SET image_path=?, image_hash=? WHERE id=?", (photo_path, photo_hash, pk))
        execute("INSERT INTO office_items (item_id, finder_usn, officer_id, received_date, received_time, "
                "storage_location, photo_path) VALUES (?,?,?,?,?,?,?)",
                (pk, finder_usn, officer_id, received_date, received_time, storage, photo_path))
        log_action(pk, f"Item registered at the college office ({storage})", f"Officer {officer_id}, finder {finder_usn}")

        item_row = query("SELECT * FROM items WHERE id=?", (pk,), one=True)
        notify_reporter(item_row, f"Your found item {code} was received at the college office. Thank you!",
                        f"/item/{code}")
        if new_item:
            recompute_matches(pk)
        for m in query("SELECT l.reporter_id, l.name, l.item_id FROM matches m JOIN items l ON l.id=m.lost_item_id "
                       "WHERE m.found_item_id=?", (pk,)):
            notify(m["reporter_id"], f"A possible match for your lost item “{m['name']}” has reached the college "
                                     f"office ({storage}). You can submit a claim.", f"/item/{code}")
        flash(f"Item {code} registered at the office and marked 'Deposited at Office'.", "success")
        return redirect(url_for("item_details", code=code))
    return render_template("office_register.html", prefill=prefill, recent=_recent_office())


def _recent_office():
    return query("SELECT o.*, i.item_id AS code, i.name FROM office_items o JOIN items i ON i.id=o.item_id "
                 "ORDER BY o.id DESC LIMIT 6")


# ---- Ownership verification -------------------------------------------------
@app.route("/admin/verification")
@admin_required
def verification_list():
    status = request.args.get("status", "")
    sql = ("SELECT c.*, i.item_id AS code, i.name AS item_name, u.name AS claimant_name FROM claims c "
           "JOIN items i ON i.id=c.item_id JOIN users u ON u.id=c.claimant_id")
    args = []
    if status in CLAIM_STATUSES:
        sql += " WHERE c.verification_status=?"
        args.append(status)
    claims = query(sql + " ORDER BY c.id DESC LIMIT 200", args)
    return render_template("verification.html", claims=claims, claim=None, status=status, CLAIM_STATUSES=CLAIM_STATUSES)


@app.route("/admin/verification/<int:claim_id>")
@admin_required
def verification_detail(claim_id):
    claim = query("SELECT c.*, u.name AS claimant_name, u.email AS claimant_email FROM claims c "
                  "JOIN users u ON u.id=c.claimant_id WHERE c.id=?", (claim_id,), one=True)
    if not claim:
        abort(404)
    item = query("SELECT * FROM items WHERE id=?", (claim["item_id"],), one=True)
    office = query("SELECT * FROM office_items WHERE item_id=?", (item["id"],), one=True)
    lost = query("SELECT * FROM items WHERE id=?", (claim["lost_item_id"],), one=True) if claim["lost_item_id"] else None
    others = query("SELECT id, verification_status FROM claims WHERE item_id=? AND id!=?", (item["id"], claim_id))
    history = query("SELECT action, detail, created_at FROM audit_log WHERE item_id=? ORDER BY id DESC", (item["id"],))
    handover = query("SELECT * FROM handovers WHERE claim_id=?", (claim_id,), one=True)
    return render_template("verification.html", claims=None, claim=claim, item=item, office=office, lost=lost,
                           others=others, history=history, handover=handover, CLAIM_STATUSES=CLAIM_STATUSES)


def sync_item_status_from_claims(item_pk):
    """After a claim is rejected, set the item status from any claims still open."""
    states = {r["verification_status"] for r in query(
        "SELECT verification_status FROM claims WHERE item_id=?", (item_pk,))}
    if "Approved" in states:
        set_item_status(item_pk, "Ready for Handover")
    elif "Ownership Verified" in states:
        set_item_status(item_pk, "Ownership Verified")
    elif states & {"Pending Review", "More Info Requested"}:
        set_item_status(item_pk, "Ownership Verification Pending")
    else:
        set_item_status(item_pk, "Claim Rejected")


@app.route("/admin/verification/<int:claim_id>/<action>", methods=["POST"])
@admin_required
def claim_action(claim_id, action):
    claim = query("SELECT * FROM claims WHERE id=?", (claim_id,), one=True)
    if not claim or action not in ("request_info", "verify", "approve", "reject", "dispute"):
        abort(404)
    item = query("SELECT * FROM items WHERE id=?", (claim["item_id"],), one=True)
    officer_id = request.form.get("officer_id", "").strip().upper()
    note = request.form.get("notes", "").strip()
    status = claim["verification_status"]
    back = redirect(url_for("verification_detail", claim_id=claim_id))

    if not re.match(r"^[A-Z0-9\-]{2,20}$", officer_id):
        flash("Enter your Officer ID so this action is recorded.", "error")
        return back
    if item["status"] in ("Returned to Owner",) or status in ("Handed Over",):
        flash("This item has already been returned. No further changes are allowed.", "error")
        return back
    if item["status"] == "Disputed" and action != "dispute":
        flash("This item is disputed. Resolve the dispute before continuing.", "error")
        return back
    if action in ("request_info", "reject", "dispute") and len(note) < 5:
        flash("Please add a short note explaining this decision.", "error")
        return back

    stamp = f"[{now_str()}] Officer {officer_id}: "
    if action == "request_info":
        if status not in ("Pending Review", "More Info Requested"):
            flash("Additional verification can only be requested on a claim under review.", "error")
            return back
        new_status = "More Info Requested"
        notify(claim["claimant_id"], f"College staff need more details to verify your claim for {item['item_id']}.",
               f"/claims/{claim_id}")
        log_action(item["id"], "Additional verification requested", f"Officer {officer_id}")
    elif action == "verify":
        if status not in ("Pending Review", "More Info Requested"):
            flash("Only a claim under review can be marked as verified.", "error")
            return back
        if request.form.get("physical_check") != "on":
            flash("Tick the box to confirm you compared the claim with the physical item.", "error")
            return back
        if query("SELECT 1 FROM claims WHERE item_id=? AND id!=? AND verification_status IN "
                 "('Ownership Verified','Approved')", (item["id"], claim_id), one=True):
            flash("Another claim for this item is already verified. Review it first.", "error")
            return back
        new_status = "Ownership Verified"
        set_item_status(item["id"], "Ownership Verified")
        notify(claim["claimant_id"], f"Ownership verified for item {item['item_id']}. Waiting for final approval.",
               f"/claims/{claim_id}")
        log_action(item["id"], "Ownership verified by college staff", f"Officer {officer_id}")
    elif action == "approve":
        if status != "Ownership Verified" or item["status"] != "Ownership Verified":
            flash("A claim must be verified before it can be approved.", "error")
            return back
        new_status = "Approved"
        set_item_status(item["id"], "Ready for Handover")
        office = query("SELECT storage_location FROM office_items WHERE item_id=?", (item["id"],), one=True)
        where = office["storage_location"] if office else "the college office"
        notify(claim["claimant_id"], f"Item {item['item_id']} is ready for handover. Collect it from {where} "
                                     f"with your college ID.", f"/claims/{claim_id}")
        log_action(item["id"], "Claim approved. Item ready for handover", f"Officer {officer_id}")
    elif action == "reject":
        if status not in ("Pending Review", "More Info Requested", "Ownership Verified"):
            flash("This claim can no longer be rejected.", "error")
            return back
        new_status = "Rejected"
        notify(claim["claimant_id"], f"Your claim for {item['item_id']} could not be verified. "
                                     f"Please contact the college office if you have more details.",
               f"/claims/{claim_id}")
        log_action(item["id"], "Claim not verified", f"Officer {officer_id}")
    else:  # dispute
        if item["status"] == "Disputed":
            flash("This item is already disputed.", "error")
            return back
        execute("INSERT INTO disputes (item_id, claim_id, notes, previous_status, opened_by) VALUES (?,?,?,?,?)",
                (item["id"], claim_id, stamp + note, item["status"], g.user["id"]))
        set_item_status(item["id"], "Disputed")
        log_action(item["id"], "Item marked as disputed for review", f"Officer {officer_id}")
        flash("Item marked as disputed. Review it on the Disputes page.", "success")
        return redirect(url_for("disputes"))

    notes = claim["verification_notes"] + (stamp + (note or action.replace("_", " ").title()) + "\n")
    execute("UPDATE claims SET verification_status=?, verification_notes=?, officer_id=? WHERE id=?",
            (new_status, notes, officer_id, claim_id))
    if action == "reject":
        sync_item_status_from_claims(item["id"])
    flash({"request_info": "Additional verification requested.", "verify": "Ownership marked as verified.",
           "approve": "Claim approved. Item is ready for handover.", "reject": "Claim rejected."}[action], "success")
    return back


# ---- Handover -----------------------------------------------------------------
@app.route("/admin/handovers")
@admin_required
def handovers():
    ready = query("SELECT c.id AS claim_id, c.claimant_usn, c.officer_id, i.item_id AS code, i.name, "
                  "u.name AS claimant_name, o.storage_location FROM claims c JOIN items i ON i.id=c.item_id "
                  "JOIN users u ON u.id=c.claimant_id LEFT JOIN office_items o ON o.item_id=i.id "
                  "WHERE c.verification_status='Approved' AND i.status='Ready for Handover' "
                  "AND NOT EXISTS (SELECT 1 FROM handovers h WHERE h.claim_id=c.id)")
    confirmed = query("SELECT h.*, i.item_id AS code, i.name FROM handovers h JOIN items i ON i.id=h.item_id "
                      "WHERE h.final_status='Handover Confirmed'")
    history = query("SELECT h.*, i.item_id AS code, i.name FROM handovers h JOIN items i ON i.id=h.item_id "
                    "WHERE h.final_status='Returned to Owner' ORDER BY h.id DESC LIMIT 100")
    exchanges = query("SELECT x.*, l.item_id AS lost_code, l.name AS lost_name, f.item_id AS found_code, "
                      "f.name AS found_name, fu.name AS finder_name, fu.usn AS finder_usn, "
                      "ou.name AS owner_name, ou.usn AS owner_usn FROM exchanges x "
                      "JOIN items l ON l.id=x.lost_item_id JOIN items f ON f.id=x.found_item_id "
                      "JOIN users fu ON fu.id=x.finder_id JOIN users ou ON ou.id=x.owner_id "
                      "ORDER BY x.id DESC LIMIT 100")
    return render_template("handover.html", ready=ready, confirmed=confirmed, history=history, exchanges=exchanges)


@app.route("/admin/handovers/<int:claim_id>/confirm", methods=["POST"])
@admin_required
def confirm_handover(claim_id):
    claim = query("SELECT * FROM claims WHERE id=?", (claim_id,), one=True)
    if not claim:
        abort(404)
    item = query("SELECT * FROM items WHERE id=?", (claim["item_id"],), one=True)
    receiver = request.form.get("receiver_usn", "").strip().upper()
    officer_id = request.form.get("officer_id", "").strip().upper()
    if claim["verification_status"] != "Approved" or item["status"] != "Ready for Handover":
        flash("Handover is only possible after ownership is verified and the claim is approved.", "error")
    elif query("SELECT 1 FROM handovers WHERE claim_id=?", (claim_id,), one=True):
        flash("Handover was already confirmed for this claim.", "error")
    elif not re.match(r"^[A-Z0-9\-]{2,20}$", officer_id):
        flash("Enter a valid Officer ID.", "error")
    elif receiver != claim["claimant_usn"]:
        flash("Receiver USN does not match the verified claimant. Check the student's college ID.", "error")
    else:
        execute("INSERT INTO handovers (item_id, claim_id, finder_usn, receiver_usn, officer_id, "
                "verification_status, final_status) VALUES (?,?,?,?,?, 'Ownership Verified', 'Handover Confirmed')",
                (item["id"], claim_id, item["finder_usn"], receiver, officer_id))
        log_action(item["id"], "Physical handover confirmed by college staff", f"Officer {officer_id}")
        flash("Handover confirmed. Now mark the item as received by the owner.", "success")
    return redirect(url_for("handovers"))


@app.route("/admin/handovers/<int:handover_id>/received", methods=["POST"])
@admin_required
def mark_received(handover_id):
    h = query("SELECT * FROM handovers WHERE id=?", (handover_id,), one=True)
    if not h:
        abort(404)
    claim = query("SELECT * FROM claims WHERE id=?", (h["claim_id"],), one=True)
    item = query("SELECT * FROM items WHERE id=?", (h["item_id"],), one=True)
    if h["final_status"] != "Handover Confirmed" or claim["verification_status"] != "Approved" \
            or item["status"] != "Ready for Handover":
        flash("The item cannot be marked as received at this stage.", "error")
        return redirect(url_for("handovers"))
    if request.form.get("physical") != "on":
        flash("Tick the box to confirm the owner physically received the item.", "error")
        return redirect(url_for("handovers"))
    receipt_id = next_code("HR", "handovers", "receipt_id")
    now = datetime.now()
    execute("UPDATE handovers SET receipt_id=?, handover_date=?, handover_time=?, final_status='Returned to Owner' "
            "WHERE id=?", (receipt_id, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), handover_id))
    set_item_status(item["id"], "Returned to Owner")
    execute("UPDATE claims SET verification_status='Handed Over' WHERE id=?", (claim["id"],))
    execute("UPDATE office_items SET status='Returned to Owner' WHERE item_id=?", (item["id"],))
    if claim["lost_item_id"]:
        set_item_status(claim["lost_item_id"], "Returned to Owner")
        log_action(claim["lost_item_id"], "Item returned to owner")
    for other in query("SELECT * FROM claims WHERE item_id=? AND id!=? AND verification_status IN "
                       "('Pending Review','More Info Requested','Ownership Verified')", (item["id"], claim["id"])):
        execute("UPDATE claims SET verification_status='Rejected', verification_notes=verification_notes||? WHERE id=?",
                (f"[{now_str()}] System: item returned to a verified owner.\n", other["id"]))
        notify(other["claimant_id"], f"Item {item['item_id']} has been returned to its verified owner.", None)
    log_action(item["id"], "Item returned to owner", f"Record {receipt_id}")
    notify(claim["claimant_id"], f"Item {item['item_id']} has been returned to you.", f"/item/{item['item_id']}")
    finder = query("SELECT id FROM users WHERE usn=?", (item["finder_usn"],), one=True)
    if finder:
        notify(finder["id"], f"The item you found ({item['item_id']}) was returned to its owner. Thank you!",
               f"/item/{item['item_id']}")
    flash(f"Item returned to owner. Saved in the handover history as {receipt_id}.", "success")
    return redirect(url_for("handovers"))


# ---- Disputes -----------------------------------------------------------------
@app.route("/admin/disputes")
@admin_required
def disputes():
    rows = query("SELECT d.*, i.item_id AS code, i.name FROM disputes d JOIN items i ON i.id=d.item_id "
                 "ORDER BY (d.status='Open') DESC, d.id DESC")
    rejected = query("SELECT c.id, c.claim_date, i.item_id AS code, i.name, u.name AS claimant_name "
                     "FROM claims c JOIN items i ON i.id=c.item_id JOIN users u ON u.id=c.claimant_id "
                     "WHERE c.verification_status='Rejected' ORDER BY c.id DESC LIMIT 50")
    return render_template("disputes.html", rows=rows, rejected=rejected)


@app.route("/admin/disputes/<int:dispute_id>/<action>", methods=["POST"])
@admin_required
def dispute_action(dispute_id, action):
    d = query("SELECT * FROM disputes WHERE id=?", (dispute_id,), one=True)
    if not d or action not in ("note", "resolve"):
        abort(404)
    note = request.form.get("notes", "").strip()
    officer = request.form.get("officer_id", "").strip().upper()
    if d["status"] != "Open":
        flash("This dispute is already resolved.", "error")
    elif len(note) < 5 or not officer:
        flash("Enter your Officer ID and a short note.", "error")
    else:
        text = d["notes"] + f"\n[{now_str()}] Officer {officer}: {note}"
        if action == "note":
            execute("UPDATE disputes SET notes=? WHERE id=?", (text, dispute_id))
            flash("Note added to the dispute record.", "success")
        else:
            execute("UPDATE disputes SET notes=?, status='Resolved', resolved_at=? WHERE id=?",
                    (text + " (resolved)", now_str(), dispute_id))
            set_item_status(d["item_id"], d["previous_status"])
            log_action(d["item_id"], "Dispute resolved. Item returned to its previous status", f"Officer {officer}")
            flash("Dispute resolved.", "success")
    return redirect(url_for("disputes"))


@app.route("/admin/item/<code>/dispute", methods=["POST"])
@admin_required
def dispute_item(code):
    item = get_item_or_404(code)
    note = request.form.get("notes", "").strip()
    officer = request.form.get("officer_id", "").strip().upper()
    if item["status"] in ("Returned to Owner", "Disputed") or len(note) < 5 or not officer:
        flash("Enter your Officer ID and a note. Returned or already-disputed items cannot be disputed.", "error")
        return redirect(url_for("item_details", code=code))
    execute("INSERT INTO disputes (item_id, notes, previous_status, opened_by) VALUES (?,?,?,?)",
            (item["id"], f"[{now_str()}] Officer {officer}: {note}", item["status"], g.user["id"]))
    set_item_status(item["id"], "Disputed")
    log_action(item["id"], "Item marked as disputed for review", f"Officer {officer}")
    flash("Item marked as disputed.", "success")
    return redirect(url_for("disputes"))


# ---- Records: the permanent trail of every item handled by the office ----------
RECORD_SELECT = """
SELECT i.id, i.item_id AS code, i.name, i.category, i.status, i.finder_usn,
       o.officer_id AS received_officer, o.received_date, o.received_time, o.storage_location,
       (SELECT COUNT(*) FROM claims c WHERE c.item_id=i.id) AS claim_count,
       (SELECT h.receiver_usn FROM handovers h WHERE h.item_id=i.id AND h.final_status='Returned to Owner') AS receiver_usn,
       (SELECT h.officer_id FROM handovers h WHERE h.item_id=i.id AND h.final_status='Returned to Owner') AS given_officer,
       (SELECT h.receipt_id FROM handovers h WHERE h.item_id=i.id AND h.final_status='Returned to Owner') AS receipt_id,
       (SELECT h.handover_date || ' ' || h.handover_time FROM handovers h
         WHERE h.item_id=i.id AND h.final_status='Returned to Owner') AS given_at
FROM items i JOIN office_items o ON o.item_id=i.id
"""


def fetch_records(search):
    sql, args = RECORD_SELECT + " WHERE 1=1", []
    if search:
        like = f"%{search}%"
        sql += (" AND (i.item_id LIKE ? OR i.name LIKE ? OR i.finder_usn LIKE ? "
                "OR EXISTS (SELECT 1 FROM claims c WHERE c.item_id=i.id AND c.claimant_usn LIKE ?) "
                "OR EXISTS (SELECT 1 FROM handovers h WHERE h.item_id=i.id AND h.receiver_usn LIKE ?))")
        args += [like] * 5
    return query(sql + " ORDER BY i.id DESC LIMIT 500", args)


@app.route("/admin/records")
@admin_required
def records():
    search = request.args.get("q", "").strip()
    return render_template("records.html", rows=fetch_records(search), q=search)


@app.route("/admin/records.csv")
@admin_required
def records_csv():
    import csv
    import io
    from flask import Response
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Item ID", "Item", "Category", "Status", "Finder USN", "Received date", "Received time",
                "Received by officer", "Storage", "Claims", "Receiver USN", "Given by officer", "Given at", "Receipt"])
    for r in fetch_records(request.args.get("q", "").strip()):
        row = [r["code"], r["name"], r["category"], r["status"], r["finder_usn"], r["received_date"],
               r["received_time"], r["received_officer"], r["storage_location"], r["claim_count"],
               r["receiver_usn"], r["given_officer"], r["given_at"], r["receipt_id"]]
        w.writerow(["'" + str(v) if str(v).startswith(("=", "+", "-", "@")) else ("" if v is None else v) for v in row])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=item-records.csv"})


@app.route("/admin/record/<code>")
@admin_required
def record_detail(code):
    item = get_item_or_404(code)
    office = query("SELECT * FROM office_items WHERE item_id=?", (item["id"],), one=True)
    reporter = query("SELECT name, usn FROM users WHERE id=?", (item["reporter_id"],), one=True)
    claims = query("SELECT c.*, u.name AS claimant_name, l.item_id AS lost_code, l.name AS lost_name, "
                   "l.owner_usn AS lost_owner_usn FROM claims c JOIN users u ON u.id=c.claimant_id "
                   "LEFT JOIN items l ON l.id=c.lost_item_id WHERE c.item_id=? ORDER BY c.id", (item["id"],))
    handover = query("SELECT * FROM handovers WHERE item_id=? ORDER BY id DESC", (item["id"],), one=True)
    dispute_rows = query("SELECT * FROM disputes WHERE item_id=? ORDER BY id", (item["id"],))
    history = query("SELECT a.action, a.detail, a.created_at, u.name AS actor FROM audit_log a "
                    "LEFT JOIN users u ON u.id=a.actor_id WHERE a.item_id=? ORDER BY a.id", (item["id"],))
    checks = []
    if handover and handover["final_status"] == "Returned to Owner":
        claim = next((c for c in claims if c["claimant_usn"] == handover["receiver_usn"]), None)
        if claim and claim["lost_owner_usn"]:
            ok = claim["lost_owner_usn"] == handover["receiver_usn"]
            checks.append((ok, "Receiver USN " + ("matches" if ok else "does NOT match")
                           + " the owner USN on the linked lost report."))
        else:
            checks.append((False, "No linked lost report was found for the receiver."))
    if len(claims) > 1:
        checks.append((False, f"{len(claims)} different claims were made for this item. Compare them."))
    return render_template("record.html", item=item, office=office, reporter=reporter, claims=claims,
                           handover=handover, disputes=dispute_rows, history=history, checks=checks)


# ----------------------------------------------------------------------------
# 12. ERRORS, START-UP
# ----------------------------------------------------------------------------
def render_error(code, title, message):
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(400)
def err_400(e):
    return render_error(400, "Request could not be processed", getattr(e, "description", "Bad request."))


@app.errorhandler(403)
def err_403(e):
    return render_error(403, "You don't have access to this page", "This page is limited to authorised users.")


@app.errorhandler(404)
def err_404(e):
    return render_error(404, "Page not found", "The page or item you are looking for does not exist.")


@app.errorhandler(413)
def err_413(e):
    return render_error(413, "File too large", "Photos must be smaller than 6 MB.")


@app.errorhandler(500)
def err_500(e):
    return render_error(500, "Something went wrong", "Please go back and try again.")


init_db()

if __name__ == "__main__":
    print("Image matching engine:", "ImageHash (pHash)" if HAVE_IMAGEHASH else "Pillow fallback (install ImageHash for pHash)")
    app.run(host="127.0.0.1", port=5000, debug=False)
