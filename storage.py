"""Crash-safe JSON file helpers for the dashboard's data files.

The dashboard runs through power outages, so a write can be cut off at any
moment. Writes go to a temp file that atomically replaces the target, so a
reader sees either the old or the new content, never a truncated file.
"""
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

DATA_DIR = "data"


def data_path(name):
    """Default location of a data file: data/<name> when the data folder exists
    (in Docker it's the mounted volume, so files survive rebuilds and deploys),
    else <name> next to the app as before (plain installs)."""
    return os.path.join(DATA_DIR, name) if os.path.isdir(DATA_DIR) else name


def atomic_write_json(path, data, **dump_kwargs):
    """Write data as JSON to path atomically (temp file + fsync + rename).

    The temp file is unique per process and thread, so two threads saving the
    same file at once can't write into each other's temp file; the later rename wins.
    """
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_json(path, default):
    """Load JSON from path; return default if the file is missing or unreadable.

    A corrupt file (e.g. from a crash before writes were atomic) is moved aside
    to <path>.corrupt so the feature recovers instead of failing on every read.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("Unreadable JSON in %s, moving it to %s.corrupt and starting fresh", path, path)
        try:
            os.replace(path, f"{path}.corrupt")
        except OSError:
            logger.exception("Could not move aside %s", path)
        return default
