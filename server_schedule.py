"""
Linux/server replacement for schedule_task.py's Windows-only scheduling.

schedule_task.py works by shelling out to `schtasks.exe` to create/update a
Windows Scheduled Task that runs daily_run.py as its own OS process -- that
only exists on Windows, so it can't work once Solvestreak is deployed to a
Linux server. This module is the replacement for that situation: instead of
an OS-level task, it runs the exact same daily job IN-PROCESS inside the
running Flask app, on a cron-style trigger, via APScheduler.

You don't pick between this and schedule_task.py yourself -- scheduler.py
does that automatically based on the OS Solvestreak is actually running on,
so nothing else in the app (app.py, settings.html) needs to know or care
which one is active.

Needs the `apscheduler` package: pip install apscheduler

One real behavior difference from the Windows version, worth knowing before
you deploy: schtasks fires daily_run.py as an independent OS process, so it
still runs even if the Solvestreak app itself isn't open (as long as
Windows is on). This module's job only fires while this Flask app process
is alive -- which is exactly the deployment model (an always-on server
process keeps running), so it's not a problem there, but it's why this
module is never used on Windows (see scheduler.py) -- it would silently
stop running the moment you closed the app.
"""
import json
import os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "automation_config.json")
DEFAULT_TIME = "06:00"
JOB_ID = "solvestreak_daily_run"

# Same config file schedule_task.py uses, so whichever backend is active the
# saved time (and what Settings displays) stays the same -- switching
# platforms (e.g. local Windows testing -> deployed Linux) doesn't lose or
# reset the time someone already chose.
def _load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_config(hhmm):
    config = _load_config()
    config["daily_run_time"] = hhmm
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass  # non-fatal -- the in-memory job is already scheduled


def get_saved_time():
    """Same contract as schedule_task.get_saved_time()."""
    return _load_config().get("daily_run_time", DEFAULT_TIME)


def _parse_time(hhmm):
    """Returns True if hhmm is a valid 24-hour "HH:MM" string."""
    try:
        hh, mm = hhmm.split(":")
        hh, mm = int(hh), int(mm)
        return 0 <= hh <= 23 and 0 <= mm <= 59
    except (ValueError, AttributeError):
        return False


_scheduler = None
_job = None


def _get_scheduler():
    global _scheduler
    if _scheduler is None:
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.start()
    return _scheduler


def _run_daily_job():
    """What actually fires at the scheduled time -- the exact same work as
    the Windows version's daily_run.py (every account with a LeetCode
    username set gets notes/paper/solutions generated and a PDF saved), just
    run in-process instead of as a separate OS process. One account's
    failure is already handled inside daily_run.run_for_user(), so there's
    nothing extra to do here beyond a last-resort safety net."""
    import daily_run
    try:
        daily_run.main()
    except Exception:
        # daily_run.main() already logs its own top-level errors to
        # logs/daily_run_YYYY-MM-DD.log; this print is just so a truly
        # unexpected failure can never crash the scheduler thread itself.
        import traceback
        traceback.print_exc()


def _schedule_job(hhmm):
    global _job
    from apscheduler.triggers.cron import CronTrigger

    hh, mm = hhmm.split(":")
    trigger = CronTrigger(hour=int(hh), minute=int(mm))
    scheduler = _get_scheduler()
    if _job is not None:
        _job = scheduler.reschedule_job(JOB_ID, trigger=trigger)
    else:
        _job = scheduler.add_job(
            _run_daily_job, trigger=trigger, id=JOB_ID, replace_existing=True,
        )


def set_time(hhmm):
    """Same contract as schedule_task.set_time(): returns (ok, message)."""
    if not _parse_time(hhmm):
        return False, f"'{hhmm}' doesn't look like a 24-hour HH:MM time, e.g. 06:00 or 19:30."

    try:
        _schedule_job(hhmm)
    except Exception as e:
        return False, f"Something went wrong scheduling the daily run: {e}"

    _save_config(hhmm)
    return True, f"Solvestreak's daily run has been updated — it will now run automatically every day at {hhmm}."


def start():
    """Call once at app startup (see app.py) so the job is scheduled from
    the saved (or default) time immediately, without waiting for someone to
    open Settings and re-save a time first."""
    _schedule_job(get_saved_time())
