"""
Streak tracking -- the "Solvestreak" in the app's own name, and the one
clearly missing piece given how much activity the app already logs per
user. A "streak" is just the number of consecutive calendar days on which
a signed-in user generated *something*: a full daily run (notes + paper +
solutions), a Custom Notes set, or a Custom Paper. No separate table is
needed for this -- history.db's existing runs/custom_notes/custom_papers
tables already have created_at + user_id on every row, so a streak is
purely derived from data that's already there.

This intentionally lives in its own small module (same pattern as usage.py
and feedback.py) rather than being bolted onto history.py, since it reads
history.db but is a distinct concern from saving/loading run content.
"""
import sqlite3
from datetime import date, timedelta

DB_PATH = "history.db"

# Any one of these tables counts as "practiced today" for streak purposes.
_ACTIVITY_TABLES = ("runs", "custom_notes", "custom_papers")


def _activity_dates(user_id):
    """All distinct calendar dates (YYYY-MM-DD strings) on which this user
    generated anything, across every activity table."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    dates = set()
    for table in _ACTIVITY_TABLES:
        cursor.execute(
            f"SELECT DISTINCT date(created_at) FROM {table} WHERE user_id = ? AND created_at IS NOT NULL",
            (user_id,),
        )
        dates.update(row[0] for row in cursor.fetchall() if row[0])
    conn.close()
    return dates


def get_streak(user_id):
    """
    Returns:
        current: consecutive days of activity, counted back from today if
            active today, or back from yesterday if not active yet today
            (the streak isn't considered broken until a FULL day passes
            with zero activity -- same logic Duolingo etc. use, so someone
            doesn't lose their streak just for not having opened the app
            yet this morning).
        longest: the longest consecutive-day run this user has ever had.
        active_today: whether they've already generated something today.
        at_risk: True when the streak is alive only because of yesterday's
            activity -- i.e. it drops to 0 at midnight if nothing happens
            before then.
        last_active_date: the most recent date with any activity, or None.
    """
    dates = _activity_dates(user_id)
    if not dates:
        return {
            "current": 0,
            "longest": 0,
            "active_today": False,
            "at_risk": False,
            "last_active_date": None,
        }

    today = date.today()
    active_today = today.isoformat() in dates

    anchor = today if active_today else today - timedelta(days=1)
    current = 0
    cursor_date = anchor
    while cursor_date.isoformat() in dates:
        current += 1
        cursor_date -= timedelta(days=1)

    at_risk = (not active_today) and current > 0

    longest = 0
    streak_run = 0
    prev = None
    for d_str in sorted(dates):
        d = date.fromisoformat(d_str)
        streak_run = streak_run + 1 if prev is not None and (d - prev).days == 1 else 1
        longest = max(longest, streak_run)
        prev = d

    return {
        "current": current,
        "longest": longest,
        "active_today": active_today,
        "at_risk": at_risk,
        "last_active_date": max(dates),
    }
