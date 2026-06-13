"""Tiny durable state for in-flight continuations + awaiting-input waits.

In-memory state is lost if the dispatcher stops (deploy, crash, reboot), which would strand work that
was mid-continuation or parked waiting on CI / a human reply. This persists both to a small SQLite file
— stdlib `sqlite3`, auto-created on first use, no server, no install step — so the dispatcher recovers
them on startup. Relocate with AGENT_STATE_DB.
"""
import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("AGENT_STATE_DB", os.path.expanduser("~/.agent-fleet/state.db"))
_lock = threading.Lock()


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS cont (issue_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS awaiting (issue_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    return c


def _put(table, issue_id, data):
    with _lock, _conn() as c:
        c.execute(f"INSERT OR REPLACE INTO {table}(issue_id,data) VALUES(?,?)",
                  (issue_id, json.dumps(data, default=str)))


def _del(table, issue_id):
    with _lock, _conn() as c:
        c.execute(f"DELETE FROM {table} WHERE issue_id=?", (issue_id,))


def _all(table):
    with _lock, _conn() as c:
        return {r[0]: json.loads(r[1]) for r in c.execute(f"SELECT issue_id,data FROM {table}")}


def save_cont(issue_id, data):      _put("cont", issue_id, data)
def del_cont(issue_id):             _del("cont", issue_id)
def save_awaiting(issue_id, data):  _put("awaiting", issue_id, data)
def del_awaiting(issue_id):         _del("awaiting", issue_id)
def load():                         return _all("cont"), _all("awaiting")
