import sqlite3
from datetime import datetime

# Kept in its own small database, same pattern as users.db/tracker.db --
# feedback is unrelated to any of the app's other data, so it doesn't belong
# bolted onto history.db or users.db.
DB_PATH = "feedback.db"

# A generous but real cap -- this is a quick "jot a note" box, not a
# document, and an unbounded textarea could otherwise be used to store
# arbitrarily large blobs in the database.
MAX_MESSAGE_LENGTH = 4000

# A real cap, not just the client-side hint in flag-screenshot's helper
# text -- SQLite will happily store a much bigger BLOB, so this is enforced
# server-side in the /feedback/flag route before save_feedback is ever
# called.
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
ALLOWED_SCREENSHOT_MIMETYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def init_feedback_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    # Migration for a feedback.db created before "Flag this question"
    # existed: add the new columns if they aren't there yet. context is
    # a short free-text description of what was flagged (e.g. "Solutions -
    # Problem 03 (Arrays), run #12"), NULL for ordinary feedback-popup
    # notes. screenshot/screenshot_mimetype are NULL unless a flag included
    # one.
    cursor.execute("PRAGMA table_info(feedback)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "context" not in existing_columns:
        cursor.execute("ALTER TABLE feedback ADD COLUMN context TEXT")
    if "screenshot" not in existing_columns:
        cursor.execute("ALTER TABLE feedback ADD COLUMN screenshot BLOB")
    if "screenshot_mimetype" not in existing_columns:
        cursor.execute("ALTER TABLE feedback ADD COLUMN screenshot_mimetype TEXT")
    conn.commit()
    conn.close()


def save_feedback(user_id, message, context=None, screenshot_bytes=None, screenshot_mimetype=None):
    """
    Saves one feedback/suggestion note for this user -- or, when context
    (and optionally a screenshot) is given, a "Flag this question" report
    tagged to a specific generated question. `message` is expected to
    already be trimmed and non-empty -- the route is responsible for that
    validation, same division of responsibility as the rest of this app
    (auth.py, history.py never re-validate what their caller already did).
    Returns the new row's id.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO feedback (user_id, message, created_at, context, screenshot, screenshot_mimetype)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, message, datetime.now().isoformat(), context, screenshot_bytes, screenshot_mimetype),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def list_feedback(user_id, limit=50):
    """
    This person's own past feedback, most recent first -- never another
    user's, even though the table holds everyone's. Returns a list of
    {"id", "message", "created_at", "context", "has_screenshot"} dicts,
    ready for the /feedback JSON endpoint to serialize directly. The raw
    screenshot bytes are never included here (see get_feedback_screenshot)
    -- this is a lightweight list, not a blob dump.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, message, created_at, context, screenshot IS NOT NULL
        FROM feedback WHERE user_id = ? ORDER BY id DESC LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r[0], "message": r[1], "created_at": r[2], "context": r[3], "has_screenshot": bool(r[4])}
        for r in rows
    ]


def list_all_feedback(limit=200):
    """
    EVERY user's feedback, most recent first -- admin-only (see the /admin
    route in app.py, gated behind auth.is_admin). Unlike list_feedback above,
    this deliberately does NOT filter by user_id, since the whole point of
    the admin dashboard is to see what everyone has sent in. Returns a list
    of {"id", "user_id", "message", "created_at", "context", "has_screenshot"}
    dicts; app.py joins these against auth.get_all_users() to attach a
    display name/email per row, since feedback.db and users.db are separate
    SQLite files with no way to JOIN across them directly.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, user_id, message, created_at, context, screenshot IS NOT NULL
        FROM feedback ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r[0], "user_id": r[1], "message": r[2], "created_at": r[3], "context": r[4], "has_screenshot": bool(r[5])}
        for r in rows
    ]


def get_feedback_screenshot(feedback_id):
    """Returns (bytes, mimetype) for one flagged item's screenshot, or None
    if that item has no screenshot (or doesn't exist). Kept as its own
    lookup, not part of list_all_feedback, so listing feedback never has to
    pull every stored image into memory at once."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT screenshot, screenshot_mimetype FROM feedback WHERE id = ?", (feedback_id,))
    row = cursor.fetchone()
    conn.close()
    if row is None or row[0] is None:
        return None
    return row[0], row[1]
