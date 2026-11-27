"""Install the app's Python packages when they're missing or requirements.txt changed.

Runs every time the app starts or restarts itself after an update (git pull). The start shortcut also
installs, but it never runs when the app is already open: the app restarts on its own when its code
changes, so an update that adds a package would otherwise run without it.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIREMENTS = os.path.join(REPO_DIR, "requirements.txt")
MARKER = os.path.join(REPO_DIR, ".requirements-installed")
# package name in requirements.txt -> the name it's imported by
IMPORT_NAMES = {"flask": "flask", "requests": "requests", "python-dotenv": "dotenv", "openai": "openai",
                "anthropic": "anthropic"}


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def missing_packages(requirements_text):
    names = [re.split(r"[<>=!~\[ ;]", line.strip(), maxsplit=1)[0].lower()
             for line in requirements_text.splitlines() if line.strip() and not line.strip().startswith("#")]
    return [n for n in names if importlib.util.find_spec(IMPORT_NAMES.get(n, n.replace("-", "_"))) is None]


def install_if_needed(run=subprocess.run, log=print):
    """Install requirements.txt with this Python if a package is missing or the file changed since the
    last install. Returns True if it installed, False if nothing was needed, None if the install failed."""
    wanted = _read(REQUIREMENTS)
    if wanted is None:
        return False
    missing = missing_packages(wanted.decode("utf-8", "replace"))
    if not missing and _read(MARKER) == wanted:
        return False
    log("Installing updated requirements" + (f" ({', '.join(missing)})" if missing else "") + "...")
    base = [sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", REQUIREMENTS]
    for cmd in (base, base + ["--user"]):  # --user when Python's own folder can't be written to
        try:
            done = run(cmd, timeout=900)
        except (OSError, subprocess.SubprocessError) as e:
            log(f"Couldn't install requirements: {e}")
            return None
        if done.returncode == 0:
            shutil.copyfile(REQUIREMENTS, MARKER)
            importlib.invalidate_caches()
            return True
    log("Installing requirements failed. Close the app and double-click setup.bat in the job-search folder.")
    return None
