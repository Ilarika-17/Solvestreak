"""
Picks the right daily-run scheduling backend for whichever OS Solvestreak
is actually running on, so app.py and the Settings page never need to care:

  - Windows (today -- running locally on your own machine): schedule_task.py,
    completely unchanged -- still points a Windows Scheduled Task
    ("SolvestreakDaily") at daily_run.py via schtasks, exactly as before.
    Nothing about your current local setup changes.

  - Anything else (a deployed Linux server, once that happens):
    server_schedule.py -- runs the daily job in-process on an APScheduler
    cron trigger instead, since schtasks doesn't exist there.

Both backends expose the same get_saved_time() / set_time(hhmm) functions
with the same signatures and return shapes, so app.py and settings.html
never branch on platform themselves -- this is the only file that does.
"""
import sys

if sys.platform == "win32":
    from schedule_task import get_saved_time, set_time

    def start():
        """No-op on Windows -- schtasks runs independently of this app
        process (that's the whole point of it), so there's nothing to kick
        off at app startup the way the server backend needs."""
        pass
else:
    from server_schedule import get_saved_time, set_time, start  # noqa: F401
