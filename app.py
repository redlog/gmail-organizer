"""Gmail Organizer — a local web app for sorting and cleaning up a Gmail INBOX.

    python app.py --setup             add an account (or replace its app password)
    python app.py                     start the app and open it in your browser
    python app.py --remove ADDRESS    forget an account (password, cache, action log)

Nothing here runs on its own: the app only reads INBOX when you click Refresh
(and once at startup), and only changes mail when you confirm an action.
"""

import argparse
import getpass
import json
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urljoin

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import gmail_imap
import parsing
import store

HOST = "127.0.0.1"
PORT = 8765
PARSE_VERSION = "3"  # bump to force a full re-download after parsing changes
STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
FULL_FLAG_CHECK_SECONDS = 24 * 3600  # re-check every message's read state at least daily
BUILD = secrets.token_hex(4)  # changes every launch; used to bust browser caches

app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ---- background jobs ---------------------------------------------------------
# Every Gmail operation (refresh, archive, trash, undo, switching account, unsubscribe)
# runs as a background job, one at a time. Requests return immediately; the page polls
# /api/status and shows a blocking progress dialog with a Stop button. Because no request
# ever waits on Gmail, Exit and Ctrl+C always work.

_job_lock = threading.Lock()
_cancel = threading.Event()       # set by Stop / Exit; jobs check it between batches
_job_seq = 0
_job = {"id": 0, "kind": "", "state": "idle", "message": "", "done": 0, "total": 0,
        "started": 0.0, "finished": 0.0, "result": None, "error": ""}


def _set(**kw):
    _job.update(kw)


def job_running():
    return _job["state"] == "running"


def start_job(kind, fn, *args):
    """Run fn(*args) in a background thread as the one current job. Returns {"job": id}."""
    global _job_seq
    with _job_lock:
        if job_running():
            raise HTTPException(409, f"Another operation is still running ({_job['message']}). "
                                     "Wait for it to finish, or press Stop.")
        _job_seq += 1
        _cancel.clear()
        _job.update(id=_job_seq, kind=kind, state="running", message="Starting…", done=0, total=0,
                    started=time.time(), finished=0.0, result=None, error="")
        jid = _job_seq

    def run():
        try:
            _set(state="done", result=fn(*args))
        except gmail_imap.Cancelled:
            _set(state="cancelled", message="Stopped.")
        except HTTPException as e:
            _set(state="error", error=str(e.detail))
        except Exception as e:  # shown to the user in the dialog
            _set(state="error", error=f"{type(e).__name__}: {e}")
        finally:
            _set(finished=time.time())

    threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
    return {"job": jid}


def account():
    """The account the app is working on right now."""
    return store.active_account()


def activate(acct):
    """Point every part of the app at this account. Only called at startup or inside a job."""
    store.use_account(acct)
    store.set_current(acct)
    parsing.configure(acct)


def _check_account(acct):
    """Reject a change requested from a page that was showing a different account."""
    if acct.lower() != account():
        raise HTTPException(409, f"This page was showing {acct}, but the app has since switched to "
                                 f"{account()}. Reload the page and try again.")


# ---- local-only protection ---------------------------------------------------

@app.middleware("http")
async def local_only(request: Request, call_next):
    host = request.headers.get("host", "")
    if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
        return JSONResponse({"detail": "bad host"}, status_code=403)
    if request.url.path.startswith("/api/") and request.headers.get("x-token") != TOKEN:
        return JSONResponse({"detail": "bad token"}, status_code=403)
    response = await call_next(request)
    # Always check for a newer copy, so an updated app never runs with stale cached JS/CSS.
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # A gmail.com account has only one address, so the To address column/stats are hidden.
    show_to = "false" if parsing.is_gmail_address(account()) else "true"
    accts = json.dumps(store.accounts()).replace("</", "<\\/")
    # A new address for app.js/style.css on every launch, so a copy cached by an older
    # version of the app can never be used.
    for f in ("/static/app.js", "/static/style.css"):
        html = html.replace(f'"{f}"', f'"{f}?v={BUILD}"')
    return (html.replace("__TOKEN__", TOKEN).replace("__ACCOUNT__", account())
            .replace('"__SHOW_TO__"', show_to).replace('"__ACCOUNTS__"', accts))


@app.get("/api/status")
def get_status():
    job = dict(_job)
    job["elapsed"] = round((job["finished"] or time.time()) - job["started"], 1) if job["started"] else 0
    return {"job": job, "last_refresh": store.get_meta("last_refresh"), "account": account()}


@app.post("/api/cancel")
def cancel():
    """Stop button: the running job stops at its next safe point (between batches)."""
    if job_running() and not _cancel.is_set():
        _cancel.set()
        _set(message=_job["message"] + " (stopping…)")
    return {"stopping": job_running()}


@app.get("/api/messages")
def messages():
    return store.all_messages()


# ---- refresh -----------------------------------------------------------------

def refresh_inbox():
    """Bring the local cache in line with INBOX. Read-only on Gmail. Runs inside a job."""
    _set(message="Connecting…", done=0, total=0)
    with gmail_imap.GmailInbox(account(), readonly=True, cancel=_cancel) as g:
        if (store.get_meta("uidvalidity") != str(g.uidvalidity)
                or store.get_meta("parse_version") != PARSE_VERSION):
            store.clear_messages()
            store.set_meta("uidvalidity", g.uidvalidity)
            store.set_meta("parse_version", PARSE_VERSION)
            store.set_meta("modseq", "")
        # Read this *before* looking at messages, so anything that changes while
        # we work is still newer than the saved marker and gets picked up next time.
        modseq_now = g.highest_modseq()
        _set(message="Listing INBOX…")
        uids = set(g.all_uids())
        cached = store.cached_uid_map()
        store.delete_uids(set(cached) - uids)
        existing = sorted(uids & set(cached))

        # Read/unread state and labels of messages we already have. Normally only
        # the ones Gmail says changed since last time; a full check of every message
        # the first time, if Gmail gives no marker, and once a day as a safety net.
        last_modseq = store.get_meta("modseq") or ""
        last_full = float(store.get_meta("flags_full_ts") or 0)
        full = (not last_modseq.isdigit() or modseq_now is None
                or time.time() - last_full > FULL_FLAG_CHECK_SECONDS)
        if existing:
            if full:
                _set(message="Updating read state…", done=0, total=len(existing))
                fl = g.fetch_flags(existing, progress=lambda d, t: _set(done=d))
            else:
                _set(message="Checking for read-state changes…", done=0, total=0)
                fl = g.fetch_flags_changed_since(int(last_modseq))
            existing_set = set(existing)
            _set(message="Saving read state…", done=0, total=0)
            store.update_flags([
                (int("\\Seen" not in f), ", ".join(l for l in labels if l != "\\Inbox"), u)
                for u, (f, labels) in fl.items() if u in existing_set
            ])
        new = sorted(uids - set(cached))
        _set(message=f"Downloading {len(new):,} new headers…", done=0, total=len(new))
        batch = []
        for rec in g.fetch_headers(new, progress=lambda d, t: _set(done=d)):
            batch.append(parsing.to_row(rec))
            if len(batch) >= 500:
                store.upsert(batch)
                batch = []
        store.upsert(batch)
    # Only remember the marker once everything above succeeded (a Stop skips this,
    # so the next refresh simply picks up where this one left off).
    store.set_meta("modseq", modseq_now if modseq_now is not None else "")
    if full:
        store.set_meta("flags_full_ts", time.time())
    store.set_meta("last_refresh", time.strftime("%Y-%m-%d %H:%M:%S"))
    _set(message=f"Refreshed: {len(uids):,} messages in INBOX", done=0, total=0)
    return {"messages": len(uids)}


def _refresh_after_change():
    """After archive/trash/undo, re-read INBOX so the table matches Gmail. A Stop here
    only skips the re-read; the change itself has already been made and logged."""
    try:
        refresh_inbox()
        return True
    except gmail_imap.Cancelled:
        return False


@app.post("/api/refresh")
def refresh():
    return start_job("refresh", refresh_inbox)


# ---- accounts ----------------------------------------------------------------

class SwitchReq(BaseModel):
    account: str


@app.post("/api/switch")
def switch_account(req: SwitchReq):
    """Change which account the app works on (as a job, so it never overlaps other Gmail work)."""
    acct = req.account.lower()
    if acct not in store.accounts():
        raise HTTPException(404, f"{acct} hasn't been set up. Run:  python app.py --setup")

    def do_switch():
        activate(acct)
        try:
            refresh_inbox()
        except gmail_imap.Cancelled:
            pass  # switched; the refresh can be finished later with Refresh
        return {"account": acct}

    return start_job("switch", do_switch)


# ---- archive / trash (only on explicit, confirmed user request) -------------

class ActionReq(BaseModel):
    account: str
    action: str
    gm_msgids: list[str]
    description: str = ""


@app.post("/api/action")
def action(req: ActionReq):
    if req.action not in ("archive", "trash"):
        raise HTTPException(400, "unknown action")
    ids = [g for g in dict.fromkeys(req.gm_msgids) if g.isdigit()]
    if not ids:
        raise HTTPException(400, "no messages")
    _check_account(req.account)
    verb = "Archiving" if req.action == "archive" else "Moving to Trash"

    def do_action():
        rows = store.rows_for(ids)
        _set(message=f"{verb}: checking which messages are still in INBOX…")
        with gmail_imap.GmailInbox(account(), readonly=False, cancel=_cancel) as g:
            if store.get_meta("uidvalidity") != str(g.uidvalidity):
                raise HTTPException(409, "INBOX changed since last refresh; please refresh first")
            fn = g.archive if req.action == "archive" else g.trash

            def prog(d, t):
                _set(message=f"{verb}…", done=d, total=t)

            moved, still, missing = fn(ids, progress=prog)
            stopped = g.stop_requested()
        store.remove_msgids(moved)
        moved_set = set(moved)
        aid = store.log_action(req.action, req.description, moved,
                               [r for r in rows if r["gm_msgid"] in moved_set]) if moved else None
        result = {"done": len(moved), "still_in_inbox": len(still), "missing": len(missing),
                  "action_id": aid, "stopped": stopped}
        if not stopped:
            result["refreshed"] = _refresh_after_change()
        return result

    return start_job(req.action, do_action)


@app.get("/api/actions")
def list_actions():
    return store.actions()


class UndoReq(BaseModel):
    account: str
    action_id: int


@app.post("/api/undo")
def undo(req: UndoReq):
    _check_account(req.account)
    # The action log lives in this account's own database, so an action can only
    # ever be undone against the mailbox it was done in.
    a = store.get_action(req.action_id)
    if not a:
        raise HTTPException(404, "no such action")
    if a["undone_ts"]:
        raise HTTPException(400, "already undone")
    ids = json.loads(a["gm_msgids"])

    def do_undo():
        _set(message=f"Undo: finding {len(ids):,} message(s)…")
        with gmail_imap.GmailInbox(account(), readonly=True, cancel=_cancel) as g:
            n = g.undo_archive(ids) if a["action"] == "archive" else g.undo_trash(ids)
        store.mark_undone(a["id"])
        return {"restored": n, "of": len(ids), "refreshed": _refresh_after_change()}

    return start_job("undo", do_undo)


# ---- unsubscribe -------------------------------------------------------------

class UnsubReq(BaseModel):
    account: str
    gm_msgid: str


@app.post("/api/unsubscribe")
def unsubscribe(req: UnsubReq):
    """RFC 8058 one-click unsubscribe: a single POST to the sender's https URL."""
    _check_account(req.account)
    rows = store.rows_for([req.gm_msgid])
    if not rows or not rows[0]["unsub_one_click"] or not rows[0]["unsub_url"].startswith("https://"):
        raise HTTPException(400, "This message has no one-click unsubscribe link")
    url = rows[0]["unsub_url"]

    def do_unsub():
        _set(message="Sending unsubscribe request…")
        try:
            r = httpx.post(url, data={"List-Unsubscribe": "One-Click"}, timeout=20,
                           follow_redirects=False, headers={"User-Agent": "gmail_idx"})
        except httpx.HTTPError as e:
            return {"result": "failed", "status": None, "error": str(e), "url": url}
        if 200 <= r.status_code < 300:
            return {"result": "ok", "status": r.status_code, "url": url}
        if 300 <= r.status_code < 400:
            # RFC 8058 says senders must not redirect, so we can't tell whether it worked.
            # We never follow it ourselves; we only report where it points.
            raw = r.headers.get("location", "").strip()
            loc = urljoin(url, raw) if raw else ""
            if not loc.lower().startswith(("https://", "http://")):
                loc = ""
            return {"result": "redirect", "status": r.status_code, "url": url, "location": loc}
        return {"result": "failed", "status": r.status_code, "url": url}

    return start_job("unsubscribe", do_unsub)


# ---- exit --------------------------------------------------------------------

_server = None
STOP_WAIT_SECONDS = 15  # how long Exit / Ctrl+C give a running job to stop cleanly


def _wait_for_job_to_stop(seconds):
    """Ask the running job to stop and give it a few seconds to do so cleanly (so e.g. an
    archive still records what it already moved). Returns True if it stopped."""
    if not job_running():
        return True
    _cancel.set()
    deadline = time.time() + seconds
    while job_running() and time.time() < deadline:
        time.sleep(0.2)
    return not job_running()


@app.post("/api/exit")
def exit_app():
    """Always stops the app. A running job is asked to stop first (up to 15 s); then the
    server shuts down regardless. This request itself returns immediately."""
    was_running = job_running()

    def shutdown():
        _wait_for_job_to_stop(STOP_WAIT_SECONDS)
        if _server is not None:
            _server.should_exit = True

    threading.Thread(target=shutdown, daemon=True).start()
    return {"stopping": True, "was_running": was_running}


# ---- entry point -------------------------------------------------------------

def setup():
    known = store.accounts()
    if known:
        print("Accounts already set up: " + ", ".join(known))
        print("Enter one of them to replace its password, or a new address to add it.")
    acct = input("Gmail address: ").strip().lower()
    if "@" not in acct:
        sys.exit("Please enter a full email address.")

    saved = None
    try:
        saved = gmail_imap.keyring.get_password(gmail_imap.KEYRING_SERVICE, acct)
    except Exception:
        pass
    if not saved:
        print(APP_PASSWORD_HELP.format(acct=acct))

    for attempt in range(3):
        prompt = "App password (input hidden" + (", Enter to keep the saved one" if saved else "") + "): "
        pw = getpass.getpass(prompt).replace(" ", "").strip()
        if not pw and not saved:
            print("No password entered.")
            continue
        print("Testing login...")
        try:
            # Test first; a password is only saved once Gmail accepts it.
            with gmail_imap.GmailInbox(acct, readonly=True, password=pw or saved) as g:
                n = len(g.all_uids())
        except Exception as e:
            print(login_failure_help(acct, e))
            continue
        if pw:
            gmail_imap.set_password(acct, pw)
        store.add_account(acct)  # only remembered once the login works
        print(f"OK - {n} messages in INBOX. " + ("Password saved in your system keychain." if pw else "Using the saved password."))
        print(f"{acct} is now the account the app opens with. Now run:  python app.py")
        return
    sys.exit("Setup didn't finish. Nothing was saved; run  python app.py --setup  to try again.")


APP_PASSWORD_HELP = """
Gmail Organizer signs in with an *app password*, not your normal Google password.
To get one for {acct}:

  1. Sign in to Google as {acct}. If you use several Google accounts, check the
     profile picture in the top-right of the Google pages below and switch if needed.
  2. Turn on 2-Step Verification (Google only offers app passwords when it's on):
       https://myaccount.google.com/security  ->  2-Step Verification
  3. Create the app password:
       https://myaccount.google.com/apppasswords
     Type a name such as "Gmail Organizer", click Create, and copy the 16-character
     password. Google shows it only once.
  4. Make sure IMAP is on: Gmail -> Settings -> See all settings ->
     Forwarding and POP/IMAP -> Enable IMAP.

Then paste the password below (spaces don't matter).
"""


def login_failure_help(acct, err):
    msg = str(err)
    if "Application-specific password required" in msg:
        why = "That looks like your normal Google password. Gmail needs an app password here."
    elif "AUTHENTICATIONFAILED" in msg or "Invalid credentials" in msg:
        why = (f"Gmail rejected the password. Check you copied all 16 characters, and that you\n"
               f"  created it while signed in as {acct} (each account needs its own).")
    elif "IMAP" in msg and "disabled" in msg.lower():
        why = "IMAP is turned off for this account. Turn it on in Gmail's settings (Forwarding and POP/IMAP)."
    else:
        why = f"Couldn't sign in: {type(err).__name__}: {msg}"
    return (f"\n  Login failed. {why}\n"
            f"  Create an app password at https://myaccount.google.com/apppasswords\n")


def remove(acct):
    acct = acct.strip().lower()
    if acct not in store.accounts():
        sys.exit(f"{acct} isn't set up. Accounts: {', '.join(store.accounts()) or '(none)'}")
    print(f"This removes {acct} from Gmail Organizer: its saved app password, cached headers and")
    print("action log. Nothing in Gmail is changed. Close the app first if it's running.")
    if input("Type yes to continue: ").strip().lower() != "yes":
        sys.exit("Cancelled.")
    try:
        store.remove_account(acct)
    except PermissionError:
        sys.exit("Couldn't delete the cache file because the app is still running. Exit the app and try again.")
    try:
        gmail_imap.keyring.delete_password(gmail_imap.KEYRING_SERVICE, acct)
    except Exception:
        pass  # no saved password
    print(f"Removed {acct}.")


def main():
    ap = argparse.ArgumentParser(description="Gmail Organizer: sort, analyze and clean up your Gmail INBOX.")
    ap.add_argument("--setup", action="store_true", help="add an account, or replace an account's app password")
    ap.add_argument("--remove", metavar="ADDRESS", help="forget an account (its password, cache and action log)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab on start")
    args = ap.parse_args()
    if args.setup:
        return setup()
    if args.remove:
        return remove(args.remove)
    if not store.current_account():
        sys.exit("First run:  python app.py --setup")
    activate(store.current_account())
    start_job("refresh", refresh_inbox)  # the page shows its progress when it opens
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}/")).start()
    global _server
    # timeout_graceful_shutdown: never let a slow browser connection keep the app alive.
    _server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning",
                                            timeout_graceful_shutdown=3))
    print(f"Gmail Organizer is running at http://{HOST}:{PORT}/  (Ctrl+C or the Exit button to stop)")
    _server.run()
    # Ctrl+C or Exit. Background jobs are daemon threads and can't keep Python alive,
    # but give a running one a moment to stop cleanly first.
    if job_running():
        print(f"Stopping the current Gmail operation (up to {STOP_WAIT_SECONDS}s; Ctrl+C again to quit now)...")
        try:
            _wait_for_job_to_stop(STOP_WAIT_SECONDS)
        except KeyboardInterrupt:
            pass
    print("Gmail Organizer stopped.")


if __name__ == "__main__":
    main()
