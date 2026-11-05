"""
Opens Solvestreak in your browser automatically, starting the Flask server
first if it isn't already running. Meant to be launched by the
SolvestreakWakeUp scheduled task (see wake_up_task.py) a few minutes before
you wake up, so the app is already loaded when you open your laptop.

Run it by hand any time with:
    python wake_up.py
"""
import os

# Make every relative path (app.py, etc.) resolve against THIS file's
# folder, no matter how/where this gets launched from (Task Scheduler's
# working directory is not this project folder).
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import socket
import subprocess
import sys
import time
import webbrowser

HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}/"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def is_server_up():
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


def start_server():
    # Launch app.py as its own detached background process so it keeps
    # running after this script exits, with no console window popping up.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [sys.executable, os.path.join(PROJECT_DIR, "app.py")],
        cwd=PROJECT_DIR,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def main():
    if not is_server_up():
        start_server()
        # Give Flask a few seconds to finish booting before pointing a
        # browser at it (check once a second, up to 30s).
        for _ in range(30):
            time.sleep(1)
            if is_server_up():
                break

    webbrowser.open(URL)


if __name__ == "__main__":
    main()
