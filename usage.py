"""
Daily AI-generation usage tracking.

Two things are tracked, both keyed by calendar date so they reset
automatically at midnight with no cleanup job needed -- a new date just
starts counting from zero:

  1. Per-user generation count -- how many times THIS person has triggered
     an AI generation today (Practice Paper, Notes, Solutions, Custom Paper,
     Custom Notes all count equally, one each). Caps one person from using
     up the whole app's shared daily AI quota by themselves.

  2. Global call count -- how many actual provider calls (not "generations"
     -- a Custom Paper with 9 marks buckets is 9 calls in one generation)
     the WHOLE app has made today, across every user combined. This is what
     actually maps to the free-tier daily limits Groq/Gemini/OpenRouter
     enforce, so it's checked and recorded separately from the per-user
     count, using the real number of provider calls a generation is about
     to make.

Both are checked BEFORE a generation runs (see app.py's _check_usage_budget)
so a request that would exceed either limit never spends a real provider
call -- and both are only recorded AFTER a generation actually succeeds, so
nobody loses a day's allowance to a provider having a flaky moment.
"""
import sqlite3
from datetime import date, timedelta

DB_PATH = "usage.db"


def init_usage_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_user_usage (
            user_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            generations INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, usage_date)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_global_usage (
            usage_date TEXT PRIMARY KEY,
            calls INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Light housekeeping -- nobody needs usage rows from a month ago lying
    # around forever. Not load-bearing for correctness (a missing row just
    # reads back as zero usage), purely to keep the database small.
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    cursor.execute("DELETE FROM daily_user_usage WHERE usage_date < ?", (cutoff,))
    cursor.execute("DELETE FROM daily_global_usage WHERE usage_date < ?", (cutoff,))
    conn.commit()
    conn.close()


def _today():
    return date.today().isoformat()


def get_user_count(user_id, usage_date=None):
    """How many generations this user has used up today (0 if none yet)."""
    usage_date = usage_date or _today()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT generations FROM daily_user_usage WHERE user_id = ? AND usage_date = ?",
        (user_id, usage_date),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def get_global_count(usage_date=None):
    """How many real provider calls the whole app has made today (0 if none yet)."""
    usage_date = usage_date or _today()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT calls FROM daily_global_usage WHERE usage_date = ?", (usage_date,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def record_usage(user_id, call_count):
    """
    Call this ONLY after a generation has actually succeeded. Adds 1 to this
    user's daily generation count and `call_count` (the real number of
    provider calls that generation made -- 1 for most actions, up to 9 for
    a multi-bucket Custom Paper) to today's global call count.
    """
    today = _today()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO daily_user_usage (user_id, usage_date, generations)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, usage_date) DO UPDATE SET generations = generations + 1
        """,
        (user_id, today),
    )
    cursor.execute(
        """
        INSERT INTO daily_global_usage (usage_date, calls)
        VALUES (?, ?)
        ON CONFLICT(usage_date) DO UPDATE SET calls = calls + excluded.calls
        """,
        (today, call_count),
    )
    conn.commit()
    conn.close()
