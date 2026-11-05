import json
import re
import sqlite3
from datetime import datetime
from fpdf import FPDF
from fpdf.errors import FPDFException
from fpdf.fonts import FontFace

DB_PATH = "history.db"


def init_history_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            weak_topics TEXT,
            notes TEXT,
            paper TEXT,
            solutions TEXT,
            user_id INTEGER
        )
    """)
    # Migration for a history.db created before multi-user support existed:
    # add the column if it isn't there yet. Rows from before sign-in existed
    # keep user_id = NULL and simply won't show up in anyone's History page
    # (get_all_runs/get_run always filter by the signed-in user's id).
    cursor.execute("PRAGMA table_info(runs)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "user_id" not in existing_columns:
        cursor.execute("ALTER TABLE runs ADD COLUMN user_id INTEGER")

    # Subject folders for Custom Notes (e.g. "Math", "Microprocessors").
    # folder_id = NULL on a note means "Uncategorized".
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS note_folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            folder_id INTEGER,
            created_at TEXT,
            topics TEXT,
            notes_json TEXT,
            pdf BLOB
        )
    """)

    # Custom Practice Papers -- these used to be download-only/not saved
    # anywhere; now every generation is kept here too so it shows up under
    # History -> Custom Papers. Flat list, no folders (unlike Custom Notes --
    # not asked for here).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            created_at TEXT,
            topics TEXT,
            counts TEXT,
            pdf BLOB
        )
    """)

    # Self-graded correct/incorrect marks on individual practice-paper
    # questions (see solutions.html's Correct/Incorrect buttons). This is
    # what closes the loop between "what got picked as a weak topic" and
    # "did the user actually get it right" -- see
    # get_recently_missed_topics below, used by app.py to blend missed
    # topics back into future weak-topic selection instead of relying
    # purely on raw LeetCode solve counts.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS question_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            run_id INTEGER,
            problem_id INTEGER,
            topic TEXT,
            correct INTEGER,
            marked_at TEXT,
            UNIQUE(run_id, problem_id)
        )
    """)

    # Timed mock-test attempts (see the Start Timed Mock Test flow on
    # paper.html). One row per finished attempt -- a person can retry the
    # same paper under the clock more than once, so this is append-only
    # like question_results, not a single column on runs.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mock_test_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            run_id INTEGER,
            duration_minutes INTEGER,
            elapsed_seconds INTEGER,
            within_time_limit INTEGER,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


def record_mock_test_attempt(user_id, run_id, duration_minutes, elapsed_seconds, within_time_limit):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO mock_test_attempts
            (user_id, run_id, duration_minutes, elapsed_seconds, within_time_limit, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, run_id, duration_minutes, elapsed_seconds, 1 if within_time_limit else 0, datetime.now().isoformat()),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return new_id


def get_best_mock_test_attempt(user_id):
    """The fastest attempt this user has ever finished WITHIN its time
    limit (a late finish doesn't count as a "best time" -- it ran over).
    Returns {"elapsed_seconds", "duration_minutes", "created_at"} or None
    if they've never finished one in time."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT elapsed_seconds, duration_minutes, created_at FROM mock_test_attempts
        WHERE user_id = ? AND within_time_limit = 1
        ORDER BY elapsed_seconds ASC LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return {"elapsed_seconds": row[0], "duration_minutes": row[1], "created_at": row[2]}


def record_question_result(user_id, run_id, problem_id, topic, correct):
    """Marks (or re-marks, via the UNIQUE(run_id, problem_id) upsert) one
    practice-paper question as correct/incorrect for this user."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO question_results (user_id, run_id, problem_id, topic, correct, marked_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, problem_id) DO UPDATE SET
            correct = excluded.correct,
            topic = excluded.topic,
            marked_at = excluded.marked_at
        """,
        (user_id, run_id, problem_id, topic, 1 if correct else 0, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_question_results_for_run(run_id, user_id):
    """{problem_id: True/False} for everything already graded on this run,
    so the Solutions page shows prior marks instead of resetting to
    unmarked on every reload."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT problem_id, correct FROM question_results WHERE run_id = ? AND user_id = ?",
        (run_id, user_id),
    )
    rows = cursor.fetchall()
    conn.close()
    return {problem_id: bool(correct) for problem_id, correct in rows}


def get_recently_missed_topics(user_id, limit=3):
    """Topics from questions this user has marked incorrect, most-recently
    -missed first and deduplicated. This is the self-reported signal that
    get_weak_topics_cached (app.py) blends in ahead of raw LeetCode solve
    counts, so a topic someone is actually struggling with keeps
    resurfacing even once their LeetCode numbers for it look fine."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT topic FROM question_results WHERE user_id = ? AND correct = 0 ORDER BY marked_at DESC",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    seen = []
    for (topic,) in rows:
        if topic and topic not in seen:
            seen.append(topic)
        if len(seen) >= limit:
            break
    return seen


def start_run(weak_topics, user_id=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO runs (created_at, weak_topics, notes, paper, solutions, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), json.dumps(weak_topics), None, None, None, user_id),
    )
    conn.commit()
    run_id = cursor.lastrowid
    conn.close()
    return run_id


def update_run(run_id, **fields):
    if not fields:
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    values = [json.dumps(value) for value in fields.values()]
    values.append(run_id)
    cursor.execute(f"UPDATE runs SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()


def save_run(weak_topics, notes, paper, solutions, user_id=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO runs (created_at, weak_topics, notes, paper, solutions, user_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        json.dumps(weak_topics),
        json.dumps(notes),
        json.dumps(paper),
        json.dumps(solutions),
        user_id,
    ))
    conn.commit()
    run_id = cursor.lastrowid
    conn.close()
    return run_id


def get_all_runs(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, created_at, weak_topics FROM runs WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": row[0], "created_at": row[1], "weak_topics": json.loads(row[2])}
        for row in rows
    ]


def get_weak_topic_frequency(user_id, limit=5):
    """
    How many times each topic has shown up in this user's weak_topics
    across every run they've ever had. This is the only historical signal
    the app currently has on topic strength (the LeetCode-derived snapshot
    in tracker.db is overwritten on every sync, not kept over time), so
    "topics you've needed the most practice on" is read from here rather
    than from a dedicated trend table. Returns a list of (topic, count)
    tuples, most-frequent first.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT weak_topics FROM runs WHERE user_id = ? AND weak_topics IS NOT NULL",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    counts = {}
    for (weak_topics_json,) in rows:
        try:
            topics = json.loads(weak_topics_json)
        except (TypeError, ValueError):
            continue
        for topic in topics:
            counts[topic] = counts.get(topic, 0) + 1

    return sorted(counts.items(), key=lambda pair: pair[1], reverse=True)[:limit]


def get_run(run_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, created_at, weak_topics, notes, paper, solutions FROM runs WHERE id = ? AND user_id = ?",
        (run_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "id": row[0],
        "created_at": row[1],
        "weak_topics": json.loads(row[2]),
        "notes": json.loads(row[3]) if row[3] else None,
        "paper": json.loads(row[4]) if row[4] else None,
        "solutions": json.loads(row[5]) if row[5] else None,
    }


# Which of a run's three AI-generated pieces is present, used to split the
# History page into separate "Practice Papers" / "Solutions" / "Coding
# Notes" lists. Each list reuses the exact same combined-report PDF
# (/history/<run_id>/pdf) that's always existed -- this only changes which
# bucket a run shows up under on the History page, not what downloads.
_RUN_FIELDS = {"notes", "paper", "solutions"}


def get_runs_with(user_id, field):
    if field not in _RUN_FIELDS:
        raise ValueError(f"Unknown run field: {field}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT id, created_at, weak_topics FROM runs WHERE user_id = ? AND {field} IS NOT NULL ORDER BY id DESC",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": row[0], "created_at": row[1], "weak_topics": json.loads(row[2])}
        for row in rows
    ]


def count_runs_with(user_id, field):
    if field not in _RUN_FIELDS:
        raise ValueError(f"Unknown run field: {field}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT COUNT(*) FROM runs WHERE user_id = ? AND {field} IS NOT NULL",
        (user_id,),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


def delete_run_field(run_id, user_id, field):
    """Removes just one piece (notes/paper/solutions) of a run, so deleting
    a "Coding Notes" entry doesn't also wipe that session's Practice Paper
    or Solutions out of their own History lists -- each list is just a view
    filtered by which field is non-NULL (see get_runs_with above). Once all
    three fields are NULL the row is pure clutter, so it's deleted outright
    instead of being left behind empty. Returns False if the run doesn't
    exist or isn't owned by this user, True otherwise."""
    if field not in _RUN_FIELDS:
        raise ValueError(f"Unknown run field: {field}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE runs SET {field} = NULL WHERE id = ? AND user_id = ?",
        (run_id, user_id),
    )
    changed = cursor.rowcount > 0
    if changed:
        cursor.execute(
            "SELECT notes, paper, solutions FROM runs WHERE id = ? AND user_id = ?",
            (run_id, user_id),
        )
        row = cursor.fetchone()
        if row is not None and all(value is None for value in row):
            cursor.execute("DELETE FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id))
    conn.commit()
    conn.close()
    return changed


# =============================================================================
# Custom Notes -- subject folders + the saved generations inside them
# =============================================================================
def create_note_folder(user_id, name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO note_folders (user_id, name, created_at) VALUES (?, ?, ?)",
        (user_id, name, datetime.now().isoformat()),
    )
    conn.commit()
    folder_id = cursor.lastrowid
    conn.close()
    return folder_id


def rename_note_folder(folder_id, user_id, new_name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE note_folders SET name = ? WHERE id = ? AND user_id = ?",
        (new_name, folder_id, user_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_note_folder(folder_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, created_at FROM note_folders WHERE id = ? AND user_id = ?",
        (folder_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "created_at": row[2]}


def get_note_folders(user_id):
    """Every folder this user has, each with how many custom notes are in it."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT f.id, f.name, f.created_at,
               (SELECT COUNT(*) FROM custom_notes c WHERE c.folder_id = f.id AND c.user_id = f.user_id)
        FROM note_folders f
        WHERE f.user_id = ?
        ORDER BY f.name COLLATE NOCASE ASC
        """,
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "created_at": r[2], "note_count": r[3]} for r in rows]


def count_uncategorized_notes(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM custom_notes WHERE user_id = ? AND folder_id IS NULL",
        (user_id,),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


def count_all_custom_notes(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM custom_notes WHERE user_id = ?", (user_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def save_custom_notes(user_id, folder_id, topics, notes_data, pdf_bytes):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO custom_notes (user_id, folder_id, created_at, topics, notes_json, pdf)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            folder_id,
            datetime.now().isoformat(),
            json.dumps(topics),
            json.dumps(notes_data),
            pdf_bytes,
        ),
    )
    conn.commit()
    note_id = cursor.lastrowid
    conn.close()
    return note_id


def get_custom_notes_in_folder(user_id, folder_id):
    """folder_id=None lists the Uncategorized bucket."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if folder_id is None:
        cursor.execute(
            "SELECT id, created_at, topics FROM custom_notes WHERE user_id = ? AND folder_id IS NULL ORDER BY id DESC",
            (user_id,),
        )
    else:
        cursor.execute(
            "SELECT id, created_at, topics FROM custom_notes WHERE user_id = ? AND folder_id = ? ORDER BY id DESC",
            (user_id, folder_id),
        )
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "created_at": r[1], "topics": json.loads(r[2])} for r in rows]


def get_custom_notes_pdf(note_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT pdf FROM custom_notes WHERE id = ? AND user_id = ?",
        (note_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return row[0]


def delete_custom_note(note_id, user_id):
    """Returns False if no matching note exists for this user (already
    deleted, wrong id, or belongs to someone else), True otherwise."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM custom_notes WHERE id = ? AND user_id = ?",
        (note_id, user_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


# =============================================================================
# Custom Practice Papers -- flat list (no folders), shown under History ->
# Custom Papers.
# =============================================================================
def save_custom_paper(user_id, topics, counts, pdf_bytes):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO custom_papers (user_id, created_at, topics, counts, pdf)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, datetime.now().isoformat(), json.dumps(topics), json.dumps(counts), pdf_bytes),
    )
    conn.commit()
    paper_id = cursor.lastrowid
    conn.close()
    return paper_id


def get_all_custom_papers(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, created_at, topics, counts FROM custom_papers WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {"id": r[0], "created_at": r[1], "topics": json.loads(r[2]), "counts": json.loads(r[3])}
        for r in rows
    ]


def count_custom_papers(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM custom_papers WHERE user_id = ?", (user_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_custom_paper_pdf(paper_id, user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT pdf FROM custom_papers WHERE id = ? AND user_id = ?",
        (paper_id, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    if row is None:
        return None
    return row[0]


def delete_custom_paper(paper_id, user_id):
    """Returns False if no matching paper exists for this user (already
    deleted, wrong id, or belongs to someone else), True otherwise."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM custom_papers WHERE id = ? AND user_id = ?",
        (paper_id, user_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return changed


# =============================================================================
# THEME — centralized palette, defined once, reused by every PDF component
# (cover, headers, footers, headings, code blocks, cards, badges, dividers,
# page numbers). White + shades of green only; no blue/purple/orange/pink.
# =============================================================================
class Theme:
    WHITE = (255, 255, 255)           # primary background
    MINT = (243, 249, 244)            # secondary background / code blocks
    PALE_GREEN = (221, 242, 225)      # cards, callouts
    SAGE_BORDER = (213, 230, 216)     # borders, dividers
    SAGE = (134, 201, 154)            # secondary green accent
    EMERALD = (35, 150, 83)           # primary accent
    FOREST = (20, 83, 45)             # headings, strong emphasis
    CHARCOAL = (38, 51, 42)           # body text
    MUTED = (100, 117, 104)           # secondary text

    HEADING_FONT = "Helvetica"
    BODY_FONT = "Helvetica"
    CODE_FONT = "Courier"


T = Theme

_UNICODE_REPLACEMENTS = {
    "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-",
    "\u2010": "-", "\u2011": "-",
    "\u2012": "-", "\u2015": "-",
    "\u2212": "-",                     # minus sign (distinct from hyphen)
    "\u2026": "...",
    "\u2022": "-", "\u25cf": "-",
    "\u2192": "->", "\u2190": "<-",
    "\u00a0": " ",
    "\u2264": "<=", "\u2265": ">=", "\u2260": "!=",
    "\u00d7": "x", "\u00f7": "/", "\u22c5": "*",
    # Every other Unicode space variant some providers use around math
    # symbols/operators (narrow no-break space, thin space, en/em space,
    # etc.) - all collapse to a plain ASCII space.
    "\u2000": " ", "\u2001": " ", "\u2002": " ", "\u2003": " ",
    "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ",
    "\u2008": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ",
    "\u205f": " ", "\u3000": " ",
    "\u2061": "", "\u2062": "", "\u2063": "", "\u2064": "",  # invisible math operators
    # Greek letters as literal Unicode characters (as opposed to LaTeX
    # commands like "\Omega", already handled by _clean_math_text) --
    # common in CS/math notes (Big-O/Big-Omega/Big-Theta, summations,
    # angles, etc). fpdf2's core fonts (Helvetica/Courier) only cover
    # Latin-1, so these fall outside what can actually be drawn and were
    # previously silently turned into "?" by the final latin-1 encode
    # below. Spelling the name out keeps the notation in the PDF instead
    # of losing the symbol entirely.
    "\u0391": "Alpha", "\u0392": "Beta", "\u0393": "Gamma", "\u0394": "Delta",
    "\u0395": "Epsilon", "\u0396": "Zeta", "\u0397": "Eta", "\u0398": "Theta",
    "\u0399": "Iota", "\u039a": "Kappa", "\u039b": "Lambda", "\u039c": "Mu",
    "\u039d": "Nu", "\u039e": "Xi", "\u039f": "Omicron", "\u03a0": "Pi",
    "\u03a1": "Rho", "\u03a3": "Sigma", "\u03a4": "Tau", "\u03a5": "Upsilon",
    "\u03a6": "Phi", "\u03a7": "Chi", "\u03a8": "Psi", "\u03a9": "Omega",
    "\u03b1": "alpha", "\u03b2": "beta", "\u03b3": "gamma", "\u03b4": "delta",
    "\u03b5": "epsilon", "\u03b6": "zeta", "\u03b7": "eta", "\u03b8": "theta",
    "\u03b9": "iota", "\u03ba": "kappa", "\u03bb": "lambda", "\u03bc": "mu",
    "\u03bd": "nu", "\u03be": "xi", "\u03bf": "omicron", "\u03c0": "pi",
    "\u03c1": "rho", "\u03c2": "sigma", "\u03c3": "sigma", "\u03c4": "tau",
    "\u03c5": "upsilon", "\u03c6": "phi", "\u03c7": "chi", "\u03c8": "psi",
    "\u03c9": "omega",
    # Other common math symbols outside Latin-1.
    "\u221e": "infinity", "\u221a": "sqrt", "\u2211": "sum", "\u220f": "product",
    "\u222b": "integral", "\u2202": "d", "\u2207": "gradient",
    "\u2248": "~=", "\u2261": "===", "\u221d": "proportional to",
    "\u2208": "in", "\u2209": "not in", "\u2200": "for all", "\u2203": "there exists",
    "\u2282": "subset of", "\u2286": "subset of or equal to",
    "\u222a": "union", "\u2229": "intersect", "\u2205": "empty set",
    "\u2297": "x", "\u2295": "+",
}


def _break_long_tokens(text, max_len=40):
    def break_word(match):
        word = match.group(0)
        return " ".join(word[i:i + max_len] for i in range(0, len(word), max_len))
    return re.sub(r"\S{%d,}" % (max_len + 1), break_word, text)


def _pdf_safe(text):
    if text is None:
        return ""
    text = str(text)
    for bad, good in _UNICODE_REPLACEMENTS.items():
        text = text.replace(bad, good)
    text = text.encode("latin-1", errors="replace").decode("latin-1")
    return _break_long_tokens(text)


def _multi_cell(pdf, w, h, text, measure=False, **kwargs):
    if measure:
        # Non-destructive height measurement (no drawing, cursor untouched) -
        # used by _callout() to size a panel before drawing its background.
        align = kwargs.get("align", "L")
        try:
            return pdf.multi_cell(w, h, text, dry_run=True, output="HEIGHT", align=align)
        except FPDFException:
            return pdf.multi_cell(w, h, text, dry_run=True, output="HEIGHT", align=align, wrapmode="CHAR")
    kwargs.setdefault("new_x", "LMARGIN")
    kwargs.setdefault("new_y", "NEXT")
    kwargs.setdefault("align", "L")
    x, y = pdf.get_x(), pdf.get_y()
    try:
        pdf.multi_cell(w, h, text, **kwargs)
    except FPDFException:
        pdf.set_xy(x, y)
        pdf.multi_cell(w, h, text, wrapmode="CHAR", **kwargs)


def _format_date(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    hour12 = dt.strftime("%I").lstrip("0") or "12"
    return dt.strftime("%B %d, %Y") + f" at {hour12}" + dt.strftime(":%M %p")


# ----------------------------------------------------------------- PDF base
class SolveStreakPDF(FPDF):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.section_title = ""
        self.chrome_enabled = False
        # fpdf2 calls footer() for the page that's ending right when
        # add_page() is invoked for the NEXT page -- so flipping
        # chrome_enabled back to True before that add_page() call (to turn
        # chrome on for the upcoming page) makes it leak onto the outgoing
        # page's footer too. This tracks a specific page number whose
        # footer should stay suppressed regardless of chrome_enabled, for
        # a one-off bare page (like the "Solutions" divider) sandwiched
        # between chrome-off and chrome-on content.
        self.footer_suppressed_page = None

    def header(self):
        if not self.chrome_enabled:
            return
        self.set_font(T.HEADING_FONT, "B", 9)
        self.set_text_color(*T.FOREST)
        self.set_xy(15, 8)
        self.cell(0, 6, "SOLVESTREAK", align="L")
        self.set_font(T.BODY_FONT, "", 9)
        self.set_text_color(*T.MUTED)
        self.set_xy(self.w - 15 - 70, 8)
        self.cell(70, 6, self.section_title, align="R")
        self.set_draw_color(*T.EMERALD)
        self.set_line_width(0.5)
        self.line(15, 15.5, self.w - 15, 15.5)
        self.set_y(22)
        self.set_text_color(*T.CHARCOAL)

    def footer(self):
        if not self.chrome_enabled or self.page_no() == self.footer_suppressed_page:
            return
        self.set_y(-15)
        self.set_draw_color(*T.SAGE_BORDER)
        self.set_line_width(0.3)
        self.line(15, self.get_y(), self.w - 15, self.get_y())
        self.set_y(-11)
        self.set_fill_color(*T.EMERALD)
        self.ellipse(self.w / 2 - 20, self.get_y() + 1.6, 1.6, 1.6, style="F")
        self.set_font(T.BODY_FONT, "", 8)
        self.set_text_color(*T.MUTED)
        self.cell(0, 8, f"Page {self.page_no()} of {{nb}}", align="C")
        self.set_text_color(*T.CHARCOAL)


_BADGE_PAD_X = 3
_BADGE_H = 6


def _badge_width(pdf, text, font_size=8.5):
    pdf.set_font(T.HEADING_FONT, "B", font_size)
    return pdf.get_string_width(text) + 2 * _BADGE_PAD_X


def _badge(pdf, text, bg=T.PALE_GREEN, fg=T.FOREST, font_size=8.5, border=False):
    w = _badge_width(pdf, text, font_size)
    h = _BADGE_H
    x, y = pdf.get_x(), pdf.get_y()
    pdf.set_fill_color(*bg)
    if border:
        pdf.set_draw_color(*T.SAGE_BORDER)
        pdf.rect(x, y, w, h, style="FD", round_corners=True, corner_radius=1.3)
    else:
        pdf.rect(x, y, w, h, style="F", round_corners=True, corner_radius=1.3)
    pdf.set_text_color(*fg)
    pdf.set_xy(x, y)
    pdf.cell(w, h, text, align="C")  # cell()'s default new_x=RIGHT already advances x by w
    pdf.set_text_color(*T.CHARCOAL)


def _badge_row(pdf, items, gap=3, row_gap=2.5, max_width=None):
    """
    Draws badges left to right, wrapping onto additional lines when the
    next one would run past the right margin -- previously a long badge
    list (e.g. many Topics badges on the Custom Notes/Paper cover page)
    just kept going straight off the edge of the page instead of wrapping,
    clipping the last few badges.

    Leaves the cursor at the START of whichever row ends up being last (x
    reset to the row's start_x, y untouched if everything fit on one row,
    same as before this changed) so every existing call site's fixed
    `pdf.ln(N)` after a _badge_row() call still gives the same visual
    clearance below the badges as it did when there was only ever one row.

    Also breaks onto a new PAGE, not just a new row, when a badge is drawn
    too close to the bottom margin -- checked and handled *before* drawing
    each badge, the same way _flowchart checks before drawing a box.
    _badge() draws a badge as two separate calls (its background rect(),
    then its text cell()), and fpdf2's own automatic page break can fire
    inside that second call if it isn't pre-empted here -- which silently
    starts a new page and resets y to the top margin for the TEXT only,
    stranding the just-drawn background rect() on the old page with no
    text in it. Worse, every badge after that one would still be
    positioned using this loop's own `y`, captured once above and never
    resynced with the page fpdf2 actually jumped to -- so each subsequent
    badge in the row repeats the same split, drifting one page further per
    badge instead of the whole row moving to the next page together (this
    produced the practice paper's near-blank pages: a topic badge's text on
    one page, its box left behind on the previous one, the type badge
    doing the same thing again a page later, then the question text itself
    picking up from that same stuck-at-the-bottom position).
    """
    if not items:
        return
    start_x = pdf.get_x()
    y = pdf.get_y()
    right_edge = (start_x + max_width) if max_width is not None else (pdf.w - pdf.r_margin)
    x = start_x
    first_in_row = True
    for text, bg, fg in items:
        w = _badge_width(pdf, text)
        if not first_in_row and x + w > right_edge:
            x = start_x
            y += _BADGE_H + row_gap
            first_in_row = True
        if y + _BADGE_H > pdf.page_break_trigger:
            pdf.add_page()
            # NOTE: not pdf.t_margin -- this app's header() callback (which
            # add_page() just invoked automatically) draws its own title
            # text/divider line and explicitly finishes with self.set_y(22),
            # well below t_margin. Resetting to the raw t_margin here ignores
            # that and places the next badge row back up inside the header's
            # own chrome, overlapping the "SOLVESTREAK" title/divider line.
            # Reading the cursor fpdf2 actually left us at avoids that.
            y = pdf.get_y()
            x = start_x
            first_in_row = True
        pdf.set_xy(x, y)
        _badge(pdf, text, bg=bg, fg=fg)
        x += w + gap
        first_in_row = False
    pdf.set_xy(start_x, y)


def _section_header(pdf, title):
    pdf.section_title = title
    pdf.add_page()
    pdf.set_fill_color(*T.EMERALD)
    pdf.rect(15, 24, 10, 1.2, style="F")
    pdf.set_y(28)
    pdf.set_font(T.HEADING_FONT, "B", 20)
    pdf.set_text_color(*T.FOREST)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*T.CHARCOAL)
    pdf.ln(4)


# ------------------------------------------------------- code highlighting
_PY_KEYWORDS = {
    "def", "class", "if", "elif", "else", "for", "while", "return", "import",
    "from", "as", "in", "is", "not", "and", "or", "True", "False", "None",
    "try", "except", "finally", "raise", "with", "lambda", "yield", "break",
    "continue", "pass", "global", "nonlocal", "assert", "del", "async", "await",
}

_TOKEN_RE = re.compile(
    r"(?P<comment>#.*)"
    r"|(?P<string>\"\"\".*?\"\"\"|'''.*?'''|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
    r"|(?P<number>\b\d+\.?\d*\b)"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<other>.)"
)


def _tokenize_line(line):
    tokens = []
    for m in _TOKEN_RE.finditer(line):
        kind = m.lastgroup
        text = m.group()
        if kind == "word" and text in _PY_KEYWORDS:
            kind = "keyword"
        tokens.append((kind, text))
    return tokens


_TOKEN_STYLE = {
    "comment": (T.MUTED, "I"),
    "string": (T.EMERALD, ""),
    "number": (T.FOREST, ""),
    "keyword": (T.FOREST, "B"),
    "word": (T.CHARCOAL, ""),
    "other": (T.CHARCOAL, ""),
}


def _auto_format_flat_code(text):
    """
    Deterministic fallback for when a model still hands back a whole
    multi-statement snippet crammed onto a single physical line (braces
    and semicolons used as the only structure, e.g. an entire mergeSort
    flattened into one line) despite the prompt asking for real multi-line,
    indented code -- this has turned out to not be reliable to get from the
    model alone for denser algorithms, even with explicit instructions and
    a worked example, so rather than keep re-prompting, the text itself is
    reformatted here using ordinary C-style brace/statement conventions:
    a new line (and one indent level deeper) after every '{', a new line
    back out one level for the matching '}', and a new line after every
    statement-ending ';' -- except a ';' inside the parentheses of
    something like a for(init; cond; update) header, which separates
    clauses, not statements, and must stay on the same line. A ';'/',' or
    any other character inside a quoted string literal is likewise left
    alone. Only ever applied when the text has no real line break of its
    own, so genuinely AI-authored multi-line formatting is never touched.
    """
    if "\n" in text or ("{" not in text and ";" not in text):
        return text

    lines = []
    depth = 0
    buf = []
    paren_depth = 0
    in_string = None

    def flush():
        piece = "".join(buf).strip()
        buf.clear()
        if piece:
            lines.append((depth, piece))

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_string:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = ch
            buf.append(ch)
        elif ch == "(":
            paren_depth += 1
            buf.append(ch)
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
            buf.append(ch)
        elif ch == "{":
            buf.append(ch)
            flush()
            depth += 1
        elif ch == "}":
            flush()
            depth = max(0, depth - 1)
            lines.append((depth, "}"))
            if i + 1 < n and text[i + 1] == ";":
                lines[-1] = (depth, "};")
                i += 1
        elif ch == ";" and paren_depth == 0:
            buf.append(ch)
            flush()
        else:
            buf.append(ch)
        i += 1
    flush()

    if len(lines) <= 1:
        return text
    return "\n".join(("    " * d) + t for d, t in lines)


def _normalize_code_text(text):
    """
    AI providers occasionally double-escape backslashes while generating
    code inside a JSON string, so after json.loads() the value still
    contains the literal two characters "\\" + "n" instead of a real line
    break (and likewise for tabs) -- rendered as-is that shows up as
    literal backslash-n text in the PDF instead of starting a new line.
    A real code/syntax block essentially never needs to display those two
    characters on purpose, so unescaping them here is safe. If the text
    still has no real line break after that (the model wrote genuinely
    flat, one-line code rather than an escaping artifact), fall back to
    reformatting it outright -- see _auto_format_flat_code.
    """
    if not text:
        return text
    text = text.replace("\r\n", "\n")
    text = text.replace("\\r\\n", "\n")
    text = text.replace("\\n", "\n")
    text = text.replace("\\t", "\t")
    text = _auto_format_flat_code(text)
    return text


def _wrap_code_line(pdf, line, font_size, avail_width):
    """
    Tokenizes one logical code line (via the existing Python-aware
    tokenizer) and greedily packs its tokens into one or more physical
    display rows that each fit avail_width -- a dense one-line statement
    (a whole nested for-loop crammed onto one line, which models like to
    produce for a "syntax" example) previously ran straight off the right
    edge of the page instead of wrapping. Keeping each row as its own list
    of (kind, tok) tuples preserves per-token syntax highlighting through
    the wrap. A single token wider than avail_width by itself (a very long
    string/identifier) is left alone on its own row rather than split
    mid-token. Returns [[]] for a blank line, so blank lines still occupy
    a row.
    """
    pdf.set_font(T.CODE_FONT, "", font_size)
    tokens = _tokenize_line(line)
    rows = []
    current, current_w = [], 0.0
    for kind, tok in tokens:
        tok_w = pdf.get_string_width(tok)
        if current and current_w + tok_w > avail_width:
            rows.append(current)
            current, current_w = [], 0.0
        current.append((kind, tok))
        current_w += tok_w
    if current or not rows:
        rows.append(current)
    return rows


def _code_block(pdf, text, font_size=9.5, line_h=5):
    """
    Renders a code block in a mint card with lightweight Python syntax
    highlighting (keywords/strings/numbers/comments colored within the
    green theme; everything else stays charcoal). Lines are drawn as
    individually-colored token runs via raw cell() placement rather than
    through multi_cell, since fpdf2 doesn't support per-token color inside
    a single wrapped/filled cell. Lines too long for the box are wrapped
    onto additional display rows at token boundaries (see _wrap_code_line)
    rather than running off the page edge.

    Code blocks that don't fit on one page are split into page-sized
    chunks, each with its own background card, rather than one gigantic
    box computed from the full line count - sizing a single box taller
    than a physical page previously made every per-token cell() call
    re-trigger fpdf2's automatic page break (since the manually-tracked
    y position stayed far past the page bottom for every token on every
    remaining line), producing one new page per token instead of per
    overflowing chunk.
    """
    text = _normalize_code_text(text)
    pad = 4
    box_w = pdf.w - pdf.l_margin - pdf.r_margin
    avail_width = box_w - 2 * pad

    # Pre-tokenize and word-wrap every logical line up front into display
    # rows, so the draw loop below just places already-decided tokens.
    lines = []
    for raw_line in text.split("\n"):
        lines.extend(_wrap_code_line(pdf, raw_line, font_size, avail_width))

    # Per-token cell() calls must never trigger fpdf2's own auto page
    # break (that's handled explicitly, per chunk, below instead).
    #
    # set_auto_page_break(False) with no margin arg resets b_margin (and
    # therefore page_break_trigger) to 0 -- fpdf2's default -- not just
    # "disabled". So pdf.page_break_trigger must be captured *before*
    # disabling, both to use as the real bottom boundary in the chunking
    # math below (using the live, now-zeroed pdf.page_break_trigger here
    # would let a chunk size itself all the way to the literal page edge,
    # under the footer) and to restore afterwards -- restoring via
    # `margin=pdf.b_margin` would restore 0, permanently collapsing the
    # margin to 0 for the rest of the document after any code block.
    was_auto = pdf.auto_page_break
    was_margin = pdf.b_margin
    trigger = pdf.page_break_trigger
    pdf.set_auto_page_break(False)
    try:
        i, n = 0, len(lines)
        while True:
            if pdf.get_y() + 2 * pad + line_h > trigger:
                pdf.add_page()
            box_x, box_y = pdf.get_x(), pdf.get_y()
            avail = trigger - box_y - 2 * pad
            max_lines = max(1, int(avail // line_h))
            chunk = lines[i:i + max_lines]
            chunk_h = 2 * pad + line_h * len(chunk)

            pdf.set_fill_color(*T.MINT)
            pdf.set_draw_color(*T.SAGE_BORDER)
            pdf.set_line_width(0.3)
            pdf.rect(box_x, box_y, box_w, chunk_h, style="FD", round_corners=True, corner_radius=1.5)

            ty = box_y + pad
            for row_tokens in chunk:
                tx = box_x + pad
                for kind, tok in row_tokens:
                    color, style = _TOKEN_STYLE.get(kind, (T.CHARCOAL, ""))
                    pdf.set_font(T.CODE_FONT, style, font_size)
                    pdf.set_text_color(*color)
                    pdf.set_xy(tx, ty)
                    pdf.cell(pdf.get_string_width(tok) + 0.01, line_h, tok, new_x="RIGHT", new_y="TOP")
                    tx += pdf.get_string_width(tok)
                ty += line_h

            i += len(chunk)
            if i >= n:
                pdf.set_xy(box_x, box_y + chunk_h)
                break
            pdf.add_page()
    finally:
        pdf.set_text_color(*T.CHARCOAL)
        pdf.set_auto_page_break(was_auto, margin=was_margin)


def _looks_like_code(text):
    """An "examples" entry is meant to be a short illustrative phrase, but
    models sometimes cram an entire multi-statement snippet (a whole class
    with a method, a loop with a body) onto one semicolon-separated line
    instead of real indented code -- braces, semicolons, or an embedded
    line break are the signal that an entry is actually code and deserves
    the monospaced, line-preserving treatment below rather than being
    flowed as one prose sentence (which is what made that kind of example
    look like unindented one-line code regardless of how well the prose
    word-wrapper worked)."""
    return bool(re.search(r"[{};]", text)) or "\n" in text


def _code_lines_rows(pdf, avail_width, text, font_size):
    text = _normalize_code_text(text)
    rows = []
    for raw_line in text.split("\n"):
        rows.extend(_wrap_code_line(pdf, raw_line, font_size, avail_width))
    return rows


def _code_lines_height(pdf, avail_width, text, font_size=9, line_h=4.6):
    return len(_code_lines_rows(pdf, avail_width, text, font_size)) * line_h


def _draw_code_lines(pdf, avail_width, text, font_size=9, line_h=4.6):
    """Draws a normalized, already-tokenized block of code starting at the
    current cursor position, preserving every embedded line break and
    leading-space indentation exactly (each run of leading spaces becomes
    its own run of single-space tokens via the existing tokenizer, so
    indentation lines up character-for-character), word-wrapping only a
    genuinely too-long line (see _wrap_code_line) instead of letting it run
    off the page. Mirrors _code_block's per-token draw loop but without its
    own background box, since this is meant to sit inside a callout panel
    that already draws one."""
    rows = _code_lines_rows(pdf, avail_width, text, font_size)
    start_x, y = pdf.get_x(), pdf.get_y()
    for row_tokens in rows:
        x = start_x
        for kind, tok in row_tokens:
            color, style = _TOKEN_STYLE.get(kind, (T.CHARCOAL, ""))
            pdf.set_font(T.CODE_FONT, style, font_size)
            pdf.set_text_color(*color)
            pdf.set_xy(x, y)
            pdf.cell(pdf.get_string_width(tok) + 0.01, line_h, tok, new_x="RIGHT", new_y="TOP")
            x += pdf.get_string_width(tok)
        y += line_h
    pdf.set_text_color(*T.CHARCOAL)
    pdf.set_xy(start_x, y)


def _divider(pdf, gap_before=2, gap_after=4):
    pdf.ln(gap_before)
    pdf.set_draw_color(*T.SAGE_BORDER)
    pdf.set_line_width(0.3)
    pdf.line(pdf.get_x(), pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(gap_after)


def _callout(pdf, render_fn):
    """
    A pale-green rounded panel. render_fn(pdf, inner_w, measuring) draws
    the panel's content using the given inner width. When measuring=True
    it must use _multi_cell(..., measure=True) for every line and return
    the total content height (a pure, non-drawing dry run) so the panel's
    background can be sized correctly *before* anything is drawn - a
    second, real pass (measuring=False) then draws the content for real.
    """
    pad = 3.5
    inner_w = pdf.w - pdf.l_margin - pdf.r_margin - 2 * pad

    content_h = render_fn(pdf, inner_w, measuring=True)
    box_h = content_h + 2 * pad

    if pdf.get_y() + box_h > pdf.page_break_trigger:
        pdf.add_page()
    start_x, start_y = pdf.get_x(), pdf.get_y()

    pdf.set_fill_color(*T.PALE_GREEN)
    pdf.set_draw_color(*T.SAGE_BORDER)
    pdf.set_line_width(0.3)
    pdf.rect(start_x, start_y, pdf.w - pdf.l_margin - pdf.r_margin, box_h, style="FD", round_corners=True, corner_radius=1.5)

    pdf.set_xy(start_x + pad, start_y + pad)
    render_fn(pdf, inner_w, measuring=False)
    pdf.set_xy(start_x, start_y + box_h)


# ------------------------------------------------- problem statement parser
_STATEMENT_LABELS = [
    "Input format", "Output format", "Example Input", "Example Output",
    "Example", "Explanation", "Input", "Output",
]
_DATA_LABELS = {"example input", "example output", "input", "output"}


def _render_statement(pdf, statement_text):
    lines = statement_text.split("\n")
    blocks = []
    current_label, current_lines = None, []
    for line in lines:
        stripped = line.strip().rstrip(":")
        hit = next((lab for lab in _STATEMENT_LABELS if stripped.lower() == lab.lower()), None)
        if hit:
            blocks.append((current_label, current_lines))
            current_label, current_lines = hit, []
        else:
            current_lines.append(line)
    blocks.append((current_label, current_lines))

    for label, body_lines in blocks:
        body_text = "\n".join(body_lines).strip()
        if label is None:
            if body_text:
                pdf.set_font(T.BODY_FONT, "", 10.5)
                pdf.set_text_color(*T.CHARCOAL)
                _multi_cell(pdf, 0, 5.5, _pdf_safe(body_text))
                pdf.ln(2)
            continue
        if not body_text:
            continue  # skip bare "Example:" dividers with nothing under them

        pdf.set_font(T.HEADING_FONT, "B", 9)
        pdf.set_text_color(*T.FOREST)
        _multi_cell(pdf, 0, 5, label.upper())
        pdf.set_text_color(*T.CHARCOAL)

        if label.lower() in _DATA_LABELS:
            pdf.set_font(T.CODE_FONT, "", 9.5)
            pdf.set_fill_color(*T.MINT)
            pdf.set_draw_color(*T.SAGE_BORDER)
            _multi_cell(pdf, 0, 5, _pdf_safe(body_text), fill=True, padding=3)
        else:
            pdf.set_font(T.BODY_FONT, "", 10.5)
            _multi_cell(pdf, 0, 5.5, _pdf_safe(body_text))
        pdf.ln(3)


# =============================================================================
# build_pdf
# =============================================================================
def build_pdf(run_data):
    pdf = SolveStreakPDF()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)

    # ---------------------------------------------------------- cover page
    pdf.chrome_enabled = False
    pdf.add_page()

    pdf.set_fill_color(*T.EMERALD)
    pdf.rect(15, 18, 14, 1.4, style="F")
    pdf.set_xy(15, 24)
    pdf.set_font(T.HEADING_FONT, "B", 15)
    pdf.set_text_color(*T.FOREST)
    pdf.cell(0, 8, "SolveStreak", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(15)
    pdf.set_font(T.BODY_FONT, "", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "AI Coding Coach", new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(15, 52)
    pdf.set_font(T.HEADING_FONT, "B", 24)
    pdf.set_text_color(*T.FOREST)
    pdf.cell(0, 12, "Practice Session Report", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(15)
    pdf.set_font(T.BODY_FONT, "", 11)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 7, f"Generated {_pdf_safe(_format_date(run_data['created_at']))}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    pdf.set_x(15)
    pdf.set_font(T.HEADING_FONT, "B", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "FOCUS TOPICS", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_x(15)
    _badge_row(pdf, [(_pdf_safe(t).upper(), T.PALE_GREEN, T.FOREST) for t in run_data["weak_topics"]])
    pdf.ln(12)

    pdf.set_x(15)
    pdf.set_font(T.HEADING_FONT, "B", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "WHAT'S INSIDE", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    contents = [
        ("Topic Notes", run_data.get("notes")),
        ("Practice Paper", run_data.get("paper")),
        ("Solutions", run_data.get("solutions")),
    ]
    for label, present in contents:
        pdf.set_x(15)
        dot_y = pdf.get_y() + 1.6
        if present:
            pdf.set_fill_color(*T.EMERALD)
            pdf.ellipse(pdf.get_x() + 1, dot_y, 3, 3, style="F")
        else:
            pdf.set_draw_color(*T.SAGE_BORDER)
            pdf.set_line_width(0.4)
            pdf.ellipse(pdf.get_x() + 1, dot_y, 3, 3, style="D")
        pdf.set_text_color(*(T.CHARCOAL if present else T.MUTED))
        pdf.set_font(T.BODY_FONT, "", 11)
        pdf.set_x(pdf.get_x() + 7)
        suffix = "" if present else "  (not generated)"
        pdf.cell(0, 6, label + suffix, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1.5)

    pdf.chrome_enabled = True

    # --------------------------------------------------------------- notes
    if run_data.get("notes"):
        _section_header(pdf, "Topic Notes")
        notes = run_data["notes"]["notes"]
        for i, note in enumerate(notes):
            pdf.set_font(T.HEADING_FONT, "B", 13)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 7, _pdf_safe(note["topic"]))
            pdf.set_text_color(*T.CHARCOAL)

            pdf.set_font(T.BODY_FONT, "", 10.5)
            _multi_cell(pdf, 0, 5.5, _pdf_safe(note["explanation"]))
            pdf.ln(3)

            patterns = note["key_patterns"]

            def render_look_for(pdf, inner_w, measuring, patterns=patterns):
                pdf.set_font(T.HEADING_FONT, "B", 9)
                if measuring:
                    total = _multi_cell(pdf, inner_w, 5, "LOOK FOR", measure=True)
                    pdf.set_font(T.BODY_FONT, "", 10)
                    for pattern in patterns:
                        total += _multi_cell(pdf, inner_w, 5, _pdf_safe(f"- {pattern}"), measure=True)
                    return total
                pdf.set_text_color(*T.FOREST)
                _multi_cell(pdf, inner_w, 5, "LOOK FOR")
                pdf.set_text_color(*T.CHARCOAL)
                pdf.set_font(T.BODY_FONT, "", 10)
                for pattern in patterns:
                    _multi_cell(pdf, inner_w, 5, _pdf_safe(f"- {pattern}"))

            _callout(pdf, render_look_for)
            pdf.ln(3)

            _code_block(pdf, _pdf_safe(note["code_skeleton"]))
            pdf.ln(5)

            if i < len(notes) - 1:
                _divider(pdf)
            else:
                pdf.ln(2)

    # --------------------------------------------------------------- paper
    if run_data.get("paper"):
        _section_header(pdf, "Practice Paper")
        problems = run_data["paper"]["problems"]
        for i, problem in enumerate(problems):
            pdf.set_font(T.HEADING_FONT, "B", 13)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 7, _pdf_safe(f"Problem {problem['id']}: {problem['title']}"))
            pdf.set_text_color(*T.CHARCOAL)
            pdf.ln(1)

            _badge_row(pdf, [
                (_pdf_safe(problem.get("topic", "")).upper(), T.PALE_GREEN, T.FOREST),
                (_pdf_safe(problem.get("difficulty", "")).upper(), T.PALE_GREEN, T.FOREST),
            ])
            pdf.ln(5)

            _render_statement(pdf, _pdf_safe(problem["statement"]))

            if i < len(problems) - 1:
                _divider(pdf)
            else:
                pdf.ln(2)

    # ----------------------------------------------------------- solutions
    if run_data.get("solutions"):
        _section_header(pdf, "Solutions")
        sols = run_data["solutions"]["solutions"]
        for i, s in enumerate(sols):
            pdf.set_font(T.HEADING_FONT, "B", 13)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 7, _pdf_safe(f"Solution {s['id']}"))
            pdf.set_text_color(*T.CHARCOAL)
            pdf.ln(1)

            pdf.set_font(T.HEADING_FONT, "B", 9)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 5, "APPROACH")
            pdf.set_text_color(*T.CHARCOAL)
            pdf.set_font(T.BODY_FONT, "", 10.5)
            _multi_cell(pdf, 0, 5.5, _pdf_safe(s["approach"]))
            pdf.ln(3)

            _code_block(pdf, _pdf_safe(s["code"]))
            pdf.ln(4)

            _badge_row(pdf, [(_pdf_safe(f"Complexity: {s['complexity']}").upper(), T.PALE_GREEN, T.FOREST)])

            if i < len(sols) - 1:
                _divider(pdf, gap_before=5)
            else:
                pdf.ln(2)

    return bytes(pdf.output())


# =============================================================================
# build_custom_paper_pdf -- the user-defined topics/MCQ/short/long paper,
# saved to custom_papers.db so it also shows up again under History ->
# Custom Papers. Within each of those three formats, a question can still
# optionally carry a "code" field (on the question and/or its solution) when
# it's a numerical or coding-flavored question -- see generate_custom_paper's
# prompt in solvestreak.py -- rendered as a proper code block regardless of
# which of the three types it's attached to.
# =============================================================================
_CUSTOM_TYPE_LABELS = {
    "mcq": "Multiple Choice",
    "short": "Short Answer",
    "long": "Long Answer",
}
_CUSTOM_TYPE_ORDER = ["mcq", "short", "long"]

# Same shape as solvestreak.py's own copy (duplicated rather than
# cross-imported, same convention as _CUSTOM_TYPE_LABELS/_CUSTOM_TYPE_ORDER
# above) -- which mark values a "short"/"long" bucket can be. "mcq" isn't
# broken down by marks on the form, it's worth a flat 1 mark each.
_CUSTOM_MARKS_BY_TYPE = {
    "short": [1, 2, 3],
    "long": [4, 5, 6, 7, 10],
}


def _iter_custom_buckets(counts):
    """
    Same logic as solvestreak.py's own copy -- yields (qtype, marks, n) for
    every non-zero bucket in `counts`, in a fixed order: mcq, then short
    1/2/3, then long 4/5/6/7/10. Handles both the current nested shape
    ({"mcq": 5, "short": {"1": 2, "3": 4}, ...}) and the flat shape from
    custom papers saved to History before the marks breakdown existed
    ({"mcq": 5, "short": 6, ...}), where a legacy "short"/"long" bucket
    comes back with marks=None since there's no mark value to recover.
    """
    mcq_n = counts.get("mcq")
    if isinstance(mcq_n, (int, float)) and mcq_n > 0:
        yield ("mcq", None, int(mcq_n))

    for qtype, allowed_marks in _CUSTOM_MARKS_BY_TYPE.items():
        bucket = counts.get(qtype)
        if isinstance(bucket, dict):
            for marks in allowed_marks:
                n = bucket.get(str(marks), bucket.get(marks))
                if isinstance(n, (int, float)) and n > 0:
                    yield (qtype, marks, int(n))
        elif isinstance(bucket, (int, float)) and bucket > 0:
            yield (qtype, None, int(bucket))


def _custom_paper_total_marks(counts):
    """Total marks across every bucket -- mcq counts as 1 mark each,
    short/long count as their bucket's mark value (or 1 for a legacy
    marks=None bucket, same convention as solvestreak.py)."""
    return sum((marks if marks is not None else 1) * n for _, marks, n in _iter_custom_buckets(counts))


def _duration_label(minutes):
    """
    Same formatting as solvestreak.py's own copy of this helper (duplicated
    rather than cross-imported, same as _CUSTOM_TYPE_LABELS/_CUSTOM_TYPE_ORDER
    above) -- turns a raw minute count into a human label for the paper's
    printed cover page, e.g. 90 -> "1 hour 30 minutes".
    """
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 60
    if minutes <= 0:
        minutes = 60
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if mins:
        parts.append(f"{mins} minute" + ("s" if mins != 1 else ""))
    return " ".join(parts) if parts else "0 minutes"


def format_custom_paper_composition(counts):
    """["5 Multiple Choice", "2 Short Answer (1 mark)", "1 Long Answer (10 marks)",
    ...] for display on the History -> Custom Papers list (templates can't
    reach _CUSTOM_TYPE_LABELS directly) and reused as-is for the cover
    page's COMPOSITION badges. A legacy marks=None bucket (a custom paper
    saved before the marks breakdown existed) renders the same plain way it
    always did, with no "(N marks)" suffix."""
    parts = []
    for qtype, marks, n in _iter_custom_buckets(counts):
        label = _CUSTOM_TYPE_LABELS[qtype]
        if marks is None:
            parts.append(f"{n} {label}")
        else:
            parts.append(f"{n} {label} ({marks} mark{'s' if marks != 1 else ''})")
    return parts

# A "long answer" question invites Gemini to write a full structured
# explanation -- headings, bullet points, inline LaTeX math -- even though
# nothing asked for markdown formatting. fpdf2 can't render LaTeX or
# markdown, so left alone that comes out as literal "#### 1. Core Concept"
# and "$O(m)$" text. These two helpers clean that up before it's drawn.
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*#*$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")

_LATEX_COMMANDS = {
    r"\ldots": "...", r"\dots": "...", r"\leq": "<=", r"\geq": ">=", r"\neq": "!=",
    r"\times": "x", r"\cdot": "*", r"\rightarrow": "->", r"\to": "->", r"\leftarrow": "<-",
    r"\pi": "pi", r"\alpha": "alpha", r"\beta": "beta", r"\gamma": "gamma",
    r"\infty": "infinity", r"\sum": "sum", r"\sqrt": "sqrt",
}


def _clean_math_text(text):
    if not text:
        return text
    for cmd, replacement in _LATEX_COMMANDS.items():
        text = text.replace(cmd, replacement)
    # \text{...}, \mathbb{...}, etc. -> just the inner content.
    text = re.sub(r"\\(?:text|mathbb|mathrm|mathcal|operatorname)\{([^}]*)\}", r"\1", text)
    # Subscript/superscript braces: P_{i} -> P_i, x^{2} -> x^2.
    text = re.sub(r"([_^])\{([^}]*)\}", r"\1\2", text)
    # Inline markdown emphasis/code spans -> plain text.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Drop the $ / $$ math delimiters themselves, keeping their contents.
    text = text.replace("$$", "").replace("$", "")
    # Any LaTeX command not special-cased above -- drop the backslash
    # rather than show it literally.
    text = re.sub(r"\\([A-Za-z]+)", r"\1", text)
    return text


def _render_rich_answer(pdf, text, font_size=10.5, line_h=5.5):
    """
    Renders a free-form AI-generated answer line by line: markdown-style
    heading lines (#, ##, ...) get a bold sub-heading treatment instead of
    showing literal hashes, "- "/"* " bullet lines keep the plain dash style
    used elsewhere in the app, and inline LaTeX math is cleaned to plain
    text throughout via _clean_math_text.
    """
    if not text:
        return
    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            pdf.ln(2)
            continue

        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            pdf.ln(1)
            pdf.set_font(T.HEADING_FONT, "B", font_size + 1)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, line_h + 0.5, _pdf_safe(_clean_math_text(heading_match.group(1))))
            pdf.set_text_color(*T.CHARCOAL)
            pdf.ln(0.5)
            continue

        bullet_match = _BULLET_RE.match(stripped)
        if bullet_match:
            pdf.set_font(T.BODY_FONT, "", font_size)
            _multi_cell(pdf, 0, line_h, _pdf_safe(f"- {_clean_math_text(bullet_match.group(1))}"))
            continue

        pdf.set_font(T.BODY_FONT, "", font_size)
        _multi_cell(pdf, 0, line_h, _pdf_safe(_clean_math_text(stripped)))


def _render_custom_question_body(pdf, q):
    """
    Renders just the body of one custom-paper question -- statement, mcq
    options, code block -- shared between a question and its OR
    alternative (see build_custom_paper_pdf) so both render identically
    without duplicating this block twice.
    """
    _render_rich_answer(pdf, q.get("question", ""))
    pdf.ln(2)

    if q.get("type") == "mcq" and q.get("options"):
        pdf.set_font(T.BODY_FONT, "", 10.5)
        for opt in q["options"]:
            _multi_cell(pdf, 0, 6, _pdf_safe(_clean_math_text(opt)))
        pdf.ln(1)

    # A coding-flavored question in any of the three formats (mcq, short,
    # long) can optionally carry a snippet to trace or debug -- see
    # generate_custom_paper_and_solutions' prompt -- so this renders
    # whenever it's present rather than being tied to a dedicated type.
    if q.get("code"):
        _code_block(pdf, _pdf_safe(q["code"]))
        pdf.ln(2)


_MCQ_LEADING_OPTION_RE = re.compile(r"^\s*(?:option\s*)?([A-D])\s*[\).:\-]?\s*", re.IGNORECASE)


def _resolve_mcq_answer(s):
    """
    Returns (correct_option, explanation) for an mcq solution, robust to the
    model putting everything into a single "answer" field (the shape every
    OTHER question type uses) instead of the separate "correct_option" /
    "explanation" fields the custom-paper prompt asks for specifically on
    mcq. This happens occasionally -- some providers key off the one worked
    example in the prompt's JSON template (which only shows "answer") more
    than the text instruction below it describing the mcq-specific shape --
    and without this, the PDF used to render a bare, answer-less
    "CORRECT ANSWER:" badge with nothing after the colon.

    Falls back, in order: the expected "correct_option"/"explanation" pair;
    then a leading option letter parsed out of "answer" (e.g. "B) because...")
    with the rest of that text as the explanation; then, if no letter can be
    found at all, no option letter but the full text still shown as the
    explanation, so nothing generated by the model is ever silently dropped.
    """
    correct_option = (s.get("correct_option") or "").strip()
    explanation = s.get("explanation", "")
    if correct_option:
        return correct_option, explanation

    fallback_answer = s.get("answer") or ""
    match = _MCQ_LEADING_OPTION_RE.match(fallback_answer)
    if match:
        return match.group(1).upper(), fallback_answer[match.end():].strip() or explanation
    return "", fallback_answer or explanation


def _render_custom_solution_body(pdf, q, s):
    """
    Renders just the body of one custom-paper solution -- mcq correct
    answer/explanation, or a free-form answer, plus an optional code block
    -- shared between a solution and its OR alternative's solution.
    """
    if q.get("type") == "mcq":
        correct_option, explanation = _resolve_mcq_answer(s)
        badge_text = f"Correct Answer: {correct_option}" if correct_option else "See explanation below"
        _badge_row(pdf, [(_pdf_safe(badge_text).upper(), T.PALE_GREEN, T.FOREST)])
        pdf.ln(4)
        _render_rich_answer(pdf, explanation)
    else:
        _render_rich_answer(pdf, s.get("answer") or s.get("explanation", ""))

    if s.get("code"):
        pdf.ln(2)
        _code_block(pdf, _pdf_safe(s["code"]))


def build_custom_paper_pdf(topics, counts, paper_data, solutions_data, difficulty="medium", duration_minutes=60, theory_percent=50):
    pdf = SolveStreakPDF()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)

    # ---------------------------------------------------------- cover page
    pdf.chrome_enabled = False
    pdf.add_page()

    pdf.set_fill_color(*T.EMERALD)
    pdf.rect(15, 18, 14, 1.4, style="F")
    pdf.set_xy(15, 24)
    pdf.set_font(T.HEADING_FONT, "B", 15)
    pdf.set_text_color(*T.FOREST)
    pdf.cell(0, 8, "SolveStreak", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(15)
    pdf.set_font(T.BODY_FONT, "", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "AI Coding Coach", new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(15, 52)
    pdf.set_font(T.HEADING_FONT, "B", 24)
    pdf.set_text_color(*T.FOREST)
    pdf.cell(0, 12, "Custom Practice Paper", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(15)
    pdf.set_font(T.BODY_FONT, "", 11)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 7, f"Generated {_pdf_safe(_format_date(datetime.now().isoformat()))}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    pdf.set_x(15)
    pdf.set_font(T.HEADING_FONT, "B", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "TOPICS", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_x(15)
    _badge_row(pdf, [(_pdf_safe(t).upper(), T.PALE_GREEN, T.FOREST) for t in topics])
    pdf.ln(12)

    pdf.set_x(15)
    pdf.set_font(T.HEADING_FONT, "B", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "COMPOSITION", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_x(15)
    composition_badges = [
        (_pdf_safe(part).upper(), T.PALE_GREEN, T.FOREST)
        for part in format_custom_paper_composition(counts)
    ]
    _badge_row(pdf, composition_badges)
    pdf.ln(12)

    pdf.set_x(15)
    pdf.set_font(T.HEADING_FONT, "B", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "EXAM FORMAT", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_x(15)
    try:
        theory_percent = int(theory_percent)
    except (TypeError, ValueError):
        theory_percent = 50
    theory_percent = max(0, min(100, theory_percent))
    total_marks = _custom_paper_total_marks(counts)
    _badge_row(pdf, [
        (_pdf_safe(f"Difficulty: {difficulty}").upper(), T.PALE_GREEN, T.FOREST),
        (_pdf_safe(f"Time Allowed: {_duration_label(duration_minutes)}").upper(), T.PALE_GREEN, T.FOREST),
        (_pdf_safe(f"{theory_percent}% Theory / {100 - theory_percent}% Practical").upper(), T.PALE_GREEN, T.FOREST),
        (_pdf_safe(f"Total Marks: {total_marks}").upper(), T.PALE_GREEN, T.FOREST),
    ])
    pdf.ln(12)

    pdf.chrome_enabled = True

    # ---------------------------------------------------------- questions
    _section_header(pdf, "Practice Paper")
    questions = paper_data["questions"]
    for i, q in enumerate(questions):
        pdf.set_font(T.HEADING_FONT, "B", 13)
        pdf.set_text_color(*T.FOREST)
        _multi_cell(pdf, 0, 7, _pdf_safe(f"Question {q['id']}"))
        pdf.set_text_color(*T.CHARCOAL)
        pdf.ln(1)

        marks = q.get("marks", 1)
        _badge_row(pdf, [
            (_pdf_safe(q.get("topic", "")).upper(), T.PALE_GREEN, T.FOREST),
            (_pdf_safe(_CUSTOM_TYPE_LABELS.get(q.get("type"), q.get("type", ""))).upper(), T.PALE_GREEN, T.FOREST),
            (_pdf_safe(f"{marks} Mark" + ("s" if marks != 1 else "")).upper(), T.PALE_GREEN, T.FOREST),
        ])
        pdf.ln(5)

        _render_custom_question_body(pdf, q)

        # OR alternatives -- see generate_custom_paper_and_solutions'
        # prompt: when the person turns this on, a short/long question also
        # comes with a same-marks alternative the student can answer
        # instead, like a standard exam paper's "Q5. ... OR ...". Shares
        # this question's own number/marks badge above, since both
        # versions are worth the same thing -- only the topic badge repeats
        # if the alternative actually leans on a different topic.
        # isinstance check guards against an off-spec AI response where
        # "or_alternative" came back as something other than an object (a
        # string, a list, null-ish) -- treat that the same as no alternative
        # at all rather than crashing the whole PDF build on a malformed
        # field from one question out of the whole paper.
        or_alt = q.get("or_alternative")
        if isinstance(or_alt, dict):
            pdf.set_x(15)
            pdf.set_font(T.HEADING_FONT, "B", 11)
            pdf.set_text_color(*T.MUTED)
            pdf.cell(0, 7, "OR", align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*T.CHARCOAL)
            pdf.ln(1)
            alt_topic = or_alt.get("topic")
            if alt_topic and alt_topic != q.get("topic"):
                pdf.set_x(15)
                _badge_row(pdf, [(_pdf_safe(alt_topic).upper(), T.PALE_GREEN, T.FOREST)])
                pdf.ln(5)
            alt_q = dict(or_alt)
            alt_q.setdefault("type", q.get("type"))
            _render_custom_question_body(pdf, alt_q)

        if i < len(questions) - 1:
            _divider(pdf)
        else:
            pdf.ln(2)

    # ------------------------------------------------- "Solutions" divider
    # A whole page with nothing on it but the word itself, centered, in a
    # huge font -- chrome (running header/footer) deliberately turned off
    # so this page really is just the one word, like a section title page
    # in a printed book.
    pdf.chrome_enabled = False
    pdf.add_page()
    pdf.footer_suppressed_page = pdf.page_no()
    pdf.set_font(T.HEADING_FONT, "B", 72)
    pdf.set_text_color(*T.FOREST)
    pdf.set_y(pdf.h / 2 - 18)
    pdf.cell(0, 36, "Solutions", align="C", new_x="LMARGIN", new_y="NEXT")

    # ----------------------------------------------------------- solutions
    pdf.chrome_enabled = True
    _section_header(pdf, "Solutions")
    # .get("id") rather than s["id"] -- same reasoning as the or_alternative
    # guard above: an occasional malformed solution entry (missing "id")
    # from the model shouldn't crash the whole paper when every other
    # solution is fine. It's simply dropped from sols_by_id; its question
    # then falls back to {} below, same as any other id that never matched.
    sols_by_id = {
        s["id"]: s for s in solutions_data["solutions"]
        if isinstance(s, dict) and s.get("id") is not None
    }
    for i, q in enumerate(questions):
        s = sols_by_id.get(q.get("id"), {})
        pdf.set_font(T.HEADING_FONT, "B", 13)
        pdf.set_text_color(*T.FOREST)
        _multi_cell(pdf, 0, 7, _pdf_safe(f"Solution {q['id']}"))
        pdf.set_text_color(*T.CHARCOAL)
        pdf.ln(1)

        _render_custom_solution_body(pdf, q, s)

        # Mirror the question side's OR alternative here (see the questions
        # loop above) so a solution for a question with an OR alternative
        # covers both versions the student could have answered.
        or_alt_q = q.get("or_alternative")
        or_alt_s = s.get("or_alternative") if isinstance(s, dict) else None
        or_alt_q = or_alt_q if isinstance(or_alt_q, dict) else None
        or_alt_s = or_alt_s if isinstance(or_alt_s, dict) else None
        if or_alt_q or or_alt_s:
            pdf.set_x(15)
            pdf.set_font(T.HEADING_FONT, "B", 11)
            pdf.set_text_color(*T.MUTED)
            pdf.cell(0, 7, "OR", align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(*T.CHARCOAL)
            pdf.ln(1)
            alt_q = dict(or_alt_q or {})
            alt_q.setdefault("type", q.get("type"))
            _render_custom_solution_body(pdf, alt_q, or_alt_s or {})

        if i < len(questions) - 1:
            _divider(pdf, gap_before=5)
        else:
            pdf.ln(2)

    return bytes(pdf.output())


# =============================================================================
# build_custom_notes_pdf -- Custom Notes: definitions/examples/syntax/
# mnemonics/tables/flowcharts, whichever ones the AI decided are genuinely
# useful for a given topic. Unlike Custom Practice Paper, every generation
# here is also saved to custom_notes.db (see save_custom_notes) so it can be
# re-downloaded later from History -> Custom Notes -> a subject folder.
# =============================================================================
def _flowchart(pdf, steps):
    """
    Renders an ordered list of plain step strings as a vertical sequence of
    connected boxes -- a simple top-to-bottom flowchart. Each box is sized
    to its own content with a non-destructive measure pass first (same
    pattern as _callout), labeled "STEP N" in bold emerald, with the step
    text underneath. A short emerald line + a small triangular arrowhead
    (drawn with polygon()) connects each box to the next. This deliberately
    only supports a straight sequence (no branches/diamonds) since the AI
    is only ever asked for a flat ordered list of steps -- that's the one
    shape that reliably lays out well on a printed page anyway.
    """
    if not steps:
        return
    pad = 4
    arrow_h = 8
    box_w = pdf.w - pdf.l_margin - pdf.r_margin
    inner_w = box_w - 2 * pad

    for i, step in enumerate(steps):
        label = f"STEP {i + 1}"
        step_text = _pdf_safe(_clean_math_text(step))

        pdf.set_font(T.HEADING_FONT, "B", 9)
        label_h = _multi_cell(pdf, inner_w, 5, label, measure=True)
        pdf.set_font(T.BODY_FONT, "", 10)
        body_h = _multi_cell(pdf, inner_w, 5.3, step_text, measure=True)
        box_h = label_h + body_h + 2 * pad

        is_last = i == len(steps) - 1
        needed = box_h + (0 if is_last else arrow_h)
        if pdf.get_y() + needed > pdf.page_break_trigger:
            pdf.add_page()

        box_x, box_y = pdf.get_x(), pdf.get_y()
        pdf.set_fill_color(*T.MINT)
        pdf.set_draw_color(*T.SAGE_BORDER)
        pdf.set_line_width(0.3)
        pdf.rect(box_x, box_y, box_w, box_h, style="FD", round_corners=True, corner_radius=1.5)

        pdf.set_xy(box_x + pad, box_y + pad)
        pdf.set_font(T.HEADING_FONT, "B", 9)
        pdf.set_text_color(*T.EMERALD)
        _multi_cell(pdf, inner_w, 5, label)
        pdf.set_text_color(*T.CHARCOAL)
        pdf.set_x(box_x + pad)
        pdf.set_font(T.BODY_FONT, "", 10)
        _multi_cell(pdf, inner_w, 5.3, step_text)

        box_bottom = box_y + box_h
        pdf.set_xy(box_x, box_bottom)

        if not is_last:
            arrow_x = box_x + box_w / 2
            tri_tip_y = box_bottom + arrow_h - 1.5
            pdf.set_draw_color(*T.EMERALD)
            pdf.set_line_width(0.7)
            pdf.line(arrow_x, box_bottom + 1, arrow_x, tri_tip_y - 2.6)
            pdf.set_fill_color(*T.EMERALD)
            pdf.polygon(
                [
                    (arrow_x - 2, tri_tip_y - 2.6),
                    (arrow_x + 2, tri_tip_y - 2.6),
                    (arrow_x, tri_tip_y),
                ],
                fill=True,
            )
            pdf.set_xy(box_x, box_bottom + arrow_h)


def build_custom_notes_pdf(topics, notes_data):
    pdf = SolveStreakPDF()
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)

    # ---------------------------------------------------------- cover page
    pdf.chrome_enabled = False
    pdf.add_page()

    pdf.set_fill_color(*T.EMERALD)
    pdf.rect(15, 18, 14, 1.4, style="F")
    pdf.set_xy(15, 24)
    pdf.set_font(T.HEADING_FONT, "B", 15)
    pdf.set_text_color(*T.FOREST)
    pdf.cell(0, 8, "SolveStreak", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(15)
    pdf.set_font(T.BODY_FONT, "", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "AI Coding Coach", new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(15, 52)
    pdf.set_font(T.HEADING_FONT, "B", 24)
    pdf.set_text_color(*T.FOREST)
    pdf.cell(0, 12, "Custom Notes", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(15)
    pdf.set_font(T.BODY_FONT, "", 11)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 7, f"Generated {_pdf_safe(_format_date(datetime.now().isoformat()))}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)

    pdf.set_x(15)
    pdf.set_font(T.HEADING_FONT, "B", 10)
    pdf.set_text_color(*T.MUTED)
    pdf.cell(0, 6, "TOPICS", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_x(15)
    _badge_row(pdf, [(_pdf_safe(t).upper(), T.PALE_GREEN, T.FOREST) for t in topics])
    pdf.ln(12)

    pdf.chrome_enabled = True

    # --------------------------------------------------------------- notes
    _section_header(pdf, "Custom Notes")
    notes = notes_data["notes"]
    for i, note in enumerate(notes):
        pdf.set_font(T.HEADING_FONT, "B", 15)
        pdf.set_text_color(*T.FOREST)
        _multi_cell(pdf, 0, 7.5, _pdf_safe(note.get("topic", "")))
        pdf.set_text_color(*T.CHARCOAL)
        pdf.ln(2)

        if note.get("definition"):
            pdf.set_font(T.HEADING_FONT, "B", 9)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 5, "DEFINITION")
            pdf.set_text_color(*T.CHARCOAL)
            pdf.set_font(T.BODY_FONT, "", 10.5)
            _multi_cell(pdf, 0, 5.5, _pdf_safe(_clean_math_text(note["definition"])))
            pdf.ln(3)

        if note.get("explanation"):
            pdf.set_font(T.HEADING_FONT, "B", 9)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 5, "EXPLANATION")
            pdf.set_text_color(*T.CHARCOAL)
            _render_rich_answer(pdf, note["explanation"])
            pdf.ln(3)

        if note.get("examples"):
            examples = note["examples"]

            def render_examples(pdf, inner_w, measuring, examples=examples):
                # _clean_math_text strips any backslash-plus-letters run (to
                # drop stray LaTeX commands), which would also eat a
                # mangled, double-escaped "\n" artifact -- turning it into a
                # bare "n" merged into the surrounding text -- before
                # _normalize_code_text (called inside _draw_code_lines) ever
                # gets a chance to turn that artifact back into a real line
                # break. So, same as the "syntax" field below, a code-like
                # example skips _clean_math_text entirely and is decided on
                # the raw text, not run through LaTeX cleanup it doesn't need.
                pdf.set_font(T.HEADING_FONT, "B", 9)
                if measuring:
                    total = _multi_cell(pdf, inner_w, 5, "EXAMPLES", measure=True)
                    for ex in examples:
                        if _looks_like_code(ex):
                            total += 1.5 + _code_lines_height(pdf, inner_w, _pdf_safe(ex)) + 1.5
                        else:
                            pdf.set_font(T.BODY_FONT, "", 10)
                            total += _multi_cell(pdf, inner_w, 5, _pdf_safe(f"- {_clean_math_text(ex)}"), measure=True)
                    return total
                pdf.set_text_color(*T.FOREST)
                _multi_cell(pdf, inner_w, 5, "EXAMPLES")
                pdf.set_text_color(*T.CHARCOAL)
                for ex in examples:
                    if _looks_like_code(ex):
                        pdf.ln(1.5)
                        _draw_code_lines(pdf, inner_w, _pdf_safe(ex))
                        pdf.ln(1.5)
                    else:
                        pdf.set_font(T.BODY_FONT, "", 10)
                        _multi_cell(pdf, inner_w, 5, _pdf_safe(f"- {_clean_math_text(ex)}"))

            _callout(pdf, render_examples)
            pdf.ln(3)

        if note.get("syntax"):
            pdf.set_font(T.HEADING_FONT, "B", 9)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 5, "SYNTAX")
            pdf.set_text_color(*T.CHARCOAL)
            pdf.ln(1)
            _code_block(pdf, _pdf_safe(note["syntax"]))
            pdf.ln(4)

        if note.get("table") and note["table"].get("headers") and note["table"].get("rows"):
            pdf.set_font(T.HEADING_FONT, "B", 9)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 5, "TABLE")
            pdf.set_text_color(*T.CHARCOAL)
            pdf.ln(1)
            table = note["table"]
            headings_style = FontFace(emphasis="BOLD", color=T.FOREST, fill_color=T.PALE_GREEN)
            safe_headers = [_pdf_safe(_clean_math_text(h)) for h in table["headers"]]
            safe_rows = [[_pdf_safe(_clean_math_text(cell)) for cell in row] for row in table["rows"]]
            with pdf.table(
                [safe_headers] + safe_rows,
                text_align="LEFT",
                headings_style=headings_style,
                borders_layout="ALL",
                cell_fill_color=T.MINT,
                cell_fill_mode="ROWS",
            ):
                pass
            pdf.ln(4)

        if note.get("mnemonics"):
            mnemonics = note["mnemonics"]

            def render_mnemonics(pdf, inner_w, measuring, mnemonics=mnemonics):
                pdf.set_font(T.HEADING_FONT, "B", 9)
                if measuring:
                    total = _multi_cell(pdf, inner_w, 5, "MNEMONICS", measure=True)
                    pdf.set_font(T.BODY_FONT, "", 10)
                    for m in mnemonics:
                        total += _multi_cell(pdf, inner_w, 5, _pdf_safe(f"- {_clean_math_text(m)}"), measure=True)
                    return total
                pdf.set_text_color(*T.FOREST)
                _multi_cell(pdf, inner_w, 5, "MNEMONICS")
                pdf.set_text_color(*T.CHARCOAL)
                pdf.set_font(T.BODY_FONT, "", 10)
                for m in mnemonics:
                    _multi_cell(pdf, inner_w, 5, _pdf_safe(f"- {_clean_math_text(m)}"))

            _callout(pdf, render_mnemonics)
            pdf.ln(3)

        if note.get("flowchart"):
            pdf.set_font(T.HEADING_FONT, "B", 9)
            pdf.set_text_color(*T.FOREST)
            _multi_cell(pdf, 0, 5, "FLOWCHART")
            pdf.set_text_color(*T.CHARCOAL)
            pdf.ln(1)
            _flowchart(pdf, note["flowchart"])
            pdf.ln(4)

        if i < len(notes) - 1:
            _divider(pdf)
        else:
            pdf.ln(2)

    return bytes(pdf.output())

