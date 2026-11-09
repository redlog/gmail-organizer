"""Local data: the list of accounts, and per account a SQLite cache of INBOX headers
plus that account's action log.

Kept outside the project folder (so it is never committed or synced):
%LOCALAPPDATA%\\gmail_idx on Windows, ~/.gmail_idx elsewhere.

    config.json                      {"accounts": [...], "current": "..."}
    accounts/<address>.sqlite3       messages cache, meta, action log for one account

Each account has its own database file, so an action logged for one mailbox can
never be undone against another.
"""

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DATA_DIR = (Path(os.environ["LOCALAPPDATA"]) / "gmail_idx" if os.environ.get("LOCALAPPDATA")
            else Path.home() / ".gmail_idx")
CONFIG_PATH = DATA_DIR / "config.json"
ACCOUNTS_DIR = DATA_DIR / "accounts"
LEGACY_DB = DATA_DIR / "cache.sqlite3"  # single-account versions kept everything here

COLUMNS = [
    "gm_msgid", "gm_thrid", "uid", "date", "from_name", "from_addr", "from_domain",
    "recipient", "to", "cc", "subject", "list_id", "unsub_url", "unsub_mailto",
    "unsub_one_click", "size", "unread", "labels", "message_id",
]

_lock = threading.Lock()
_db_path = None       # database of the account in use
_account = ""


# ---- accounts ----------------------------------------------------------------

def _read_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("accounts", [])
    cfg.setdefault("current", "")
    return cfg


def _write_config(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def db_path_for(account):
    safe = re.sub(r"[^a-z0-9@._+-]", "_", account.lower())
    return ACCOUNTS_DIR / f"{safe}.sqlite3"


def _migrate_legacy():
    """Move a single-account cache.sqlite3 into accounts/<address>.sqlite3, keeping its action log."""
    if CONFIG_PATH.exists() or not LEGACY_DB.exists():
        return
    acct = ""
    c = sqlite3.connect(LEGACY_DB)
    try:
        r = c.execute("SELECT v FROM meta WHERE k='account'").fetchone()
        acct = (r[0] or "").lower() if r else ""
    except sqlite3.Error:
        pass
    finally:
        c.close()
    if not acct:
        return  # never set up; nothing worth keeping
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    LEGACY_DB.replace(db_path_for(acct))
    _write_config({"accounts": [acct], "current": acct})


def accounts():
    _migrate_legacy()
    return list(_read_config()["accounts"])


def current_account():
    _migrate_legacy()
    cfg = _read_config()
    cur = cfg["current"]
    if cur not in cfg["accounts"]:
        cur = cfg["accounts"][0] if cfg["accounts"] else ""
    return cur


def add_account(account):
    account = account.lower()
    cfg = _read_config()
    if account not in cfg["accounts"]:
        cfg["accounts"].append(account)
    cfg["current"] = account
    _write_config(cfg)


def set_current(account):
    cfg = _read_config()
    if account not in cfg["accounts"]:
        raise ValueError(f"Unknown account {account}")
    cfg["current"] = account
    _write_config(cfg)


def remove_account(account):
    """Forget an account: drop it from the list and delete its local cache and action log."""
    account = account.lower()
    cfg = _read_config()
    if account in cfg["accounts"]:
        cfg["accounts"].remove(account)
    if cfg["current"] == account:
        cfg["current"] = cfg["accounts"][0] if cfg["accounts"] else ""
    _write_config(cfg)
    p = db_path_for(account)
    if p.exists():
        p.unlink()


def use_account(account):
    """Point every function below at this account's database (creating it if needed)."""
    global _db_path, _account
    with _lock:
        _db_path = db_path_for(account)
        _account = account
    init()


def active_account():
    return _account


# ---- per-account database ----------------------------------------------------

@contextmanager
def _conn():
    """Open this account's database, commit on success, and always close it
    (an open file can't be deleted on Windows, which --remove needs)."""
    if _db_path is None:
        raise RuntimeError("No account selected")
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_db_path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def init():
    with _lock, _conn() as c:
        # The messages table is only a cache: if its columns are out of date, rebuild it
        # (the next refresh downloads everything again). The action log is kept.
        have = [r[1] for r in c.execute("PRAGMA table_info(messages)")]
        if have and have != COLUMNS:
            c.execute("DROP TABLE messages")
        cols = ", ".join(f'"{k}"' + (" TEXT PRIMARY KEY" if k == "gm_msgid" else "") for k in COLUMNS)
        c.execute(f"CREATE TABLE IF NOT EXISTS messages ({cols})")
        # Refresh looks messages up by IMAP UID. Without this index every lookup scanned the
        # whole table, so updating 30,000 messages took ~a billion comparisons (minutes).
        c.execute("CREATE INDEX IF NOT EXISTS messages_uid ON messages(uid)")
        c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, action TEXT, description TEXT, count INTEGER,
            gm_msgids TEXT, rows TEXT, undone_ts TEXT)""")


def get_meta(k, default=None):
    with _lock, _conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def set_meta(k, v):
    with _lock, _conn() as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, str(v)))


def all_messages():
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM messages")]


def cached_uid_map():
    """{uid: gm_msgid}"""
    with _lock, _conn() as c:
        return {int(r["uid"]): r["gm_msgid"] for r in c.execute("SELECT uid, gm_msgid FROM messages")}


def clear_messages():
    with _lock, _conn() as c:
        c.execute("DELETE FROM messages")


def upsert(rows):
    if not rows:
        return
    cols = ", ".join(f'"{k}"' for k in COLUMNS)
    qs = ", ".join("?" for _ in COLUMNS)
    with _lock, _conn() as c:
        c.executemany(f"INSERT OR REPLACE INTO messages ({cols}) VALUES ({qs})",
                      [[r.get(k) for k in COLUMNS] for r in rows])


def update_flags(updates):
    """updates: [(unread, labels, uid)] — only rows whose values actually changed are written."""
    with _lock, _conn() as c:
        c.executemany("UPDATE messages SET unread=?1, labels=?2 WHERE uid=?3 "
                      "AND (unread IS NOT ?1 OR labels IS NOT ?2)", updates)


def delete_uids(uids):
    with _lock, _conn() as c:
        c.executemany("DELETE FROM messages WHERE uid=?", [(u,) for u in uids])


def rows_for(gm_msgids):
    out = []
    with _lock, _conn() as c:
        for g in gm_msgids:
            r = c.execute("SELECT * FROM messages WHERE gm_msgid=?", (g,)).fetchone()
            if r:
                out.append(dict(r))
    return out


def remove_msgids(gm_msgids):
    with _lock, _conn() as c:
        c.executemany("DELETE FROM messages WHERE gm_msgid=?", [(g,) for g in gm_msgids])


def log_action(action, description, gm_msgids, rows):
    with _lock, _conn() as c:
        cur = c.execute(
            "INSERT INTO actions (ts, action, description, count, gm_msgids, rows) VALUES (?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), action, description,
             len(gm_msgids), json.dumps(gm_msgids), json.dumps(rows)))
        return cur.lastrowid


def actions():
    with _lock, _conn() as c:
        return [
            {k: r[k] for k in ("id", "ts", "action", "description", "count", "undone_ts")}
            for r in c.execute("SELECT * FROM actions ORDER BY id DESC")
        ]


def get_action(aid):
    with _lock, _conn() as c:
        r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None


def mark_undone(aid):
    with _lock, _conn() as c:
        c.execute("UPDATE actions SET undone_ts=? WHERE id=?",
                  (datetime.now().isoformat(timespec="seconds"), aid))
