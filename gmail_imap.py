"""Gmail IMAP access with hard safety limits.

Safety rules enforced here (not just by convention):

  * The only mailbox ever selected for reading or changing is INBOX.
    The single exception is undo: to put messages *this app moved out of INBOX*
    back into INBOX, it must open All Mail / Trash, find those exact messages
    by Gmail message id, and return them to INBOX. Nothing else is done there.
  * Nothing is ever permanently deleted:
      - no EXPUNGE, no CLOSE (CLOSE silently expunges \\Deleted messages),
        no DELETE, and the \\Deleted flag is never set.
      - "Trash" is an atomic UID MOVE into Gmail's Trash folder.
      - "Archive" is a UID MOVE from INBOX to All Mail, which in Gmail only
        drops the Inbox label (the message was already in All Mail).
  * After every archive/trash the app re-reads INBOX and reports only the
    messages that actually left.
  * Every UID command goes through `_uid()`, which rejects anything outside a
    short allow-list, so a coding mistake elsewhere fails loudly instead of
    touching mail.
"""

import imaplib
import re

import keyring

IMAP_SERVER = "imap.gmail.com"
KEYRING_SERVICE = "gmail_idx"
CHUNK = 500

# Header fields we download for each message.
HEADER_FIELDS = (
    "FROM TO CC DELIVERED-TO X-ORIGINAL-TO RECEIVED DATE SUBJECT MESSAGE-ID "
    "LIST-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST"
)


NETWORK_TIMEOUT = 60  # seconds without any reply from Gmail before giving up


class SafetyError(RuntimeError):
    pass


class Cancelled(Exception):
    """Raised between batches when the user pressed Stop."""


def get_password(account):
    pw = keyring.get_password(KEYRING_SERVICE, account)
    if not pw:
        raise RuntimeError(
            f"No app password stored for {account}. Run:  python app.py --setup"
        )
    return pw


def set_password(account, password):
    keyring.set_password(KEYRING_SERVICE, account, password)


def _chunks(seq, n=CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _uidset(uids):
    return ",".join(str(int(u)) for u in uids)


class GmailInbox:
    """Context manager: one IMAP connection, INBOX selected."""

    def __init__(self, account, readonly=True, password=None, cancel=None):
        self.account = account
        self._password = password  # used by setup to test a password before saving it
        self._cancel = cancel      # threading.Event: set it to stop between batches
        self.readonly = readonly
        self.conn = None
        self.selected = None
        self.uidvalidity = None
        self._special = None

    # ---- connection -------------------------------------------------------

    def __enter__(self):
        # A network timeout means a dropped connection fails with an error instead of
        # hanging forever. Every request we make normally answers within a few seconds.
        self.conn = imaplib.IMAP4_SSL(IMAP_SERVER, timeout=NETWORK_TIMEOUT)
        self.conn.login(self.account, self._password or get_password(self.account))
        typ, dat = self.conn.capability()  # Gmail lists its extensions only after login
        caps = set(dat[0].decode().upper().split()) if dat and dat[0] else set()
        if "X-GM-EXT-1" not in caps or "MOVE" not in caps:
            raise SafetyError("Server lacks Gmail extensions or MOVE; refusing to continue.")
        self._select("INBOX", self.readonly)
        return self

    def __exit__(self, *exc):
        # LOGOUT only. Never CLOSE: CLOSE expunges \Deleted messages.
        try:
            self.conn.logout()
        except Exception:
            pass
        return False

    def _select(self, mailbox, readonly):
        if mailbox != "INBOX" and mailbox not in self._special_folders().values():
            raise SafetyError(f"Refusing to select {mailbox!r}")
        typ, data = self.conn.select(_quote(mailbox), readonly=readonly)
        if typ != "OK":
            raise RuntimeError(f"SELECT {mailbox} failed: {data}")
        self.selected = mailbox
        self.readonly = readonly
        typ, data = self.conn.response("UIDVALIDITY")
        uv = data[0] if data and data[0] else None
        self.uidvalidity = int(uv) if uv else None

    def _special_folders(self):
        """Find Gmail's Trash and All Mail by their SPECIAL-USE flags (names are localized)."""
        if self._special is None:
            found = {}
            typ, data = self.conn.list()
            for line in data or []:
                if not isinstance(line, bytes):
                    continue
                s = line.decode("utf-8", "replace")
                m = re.match(r'\((?P<flags>[^)]*)\)\s+"[^"]*"\s+(?P<name>.+)$', s)
                if not m:
                    continue
                name = m.group("name").strip()
                if name.startswith('"') and name.endswith('"'):
                    name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                flags = m.group("flags")
                if "\\Trash" in flags:
                    found["trash"] = name
                elif "\\All" in flags:
                    found["all"] = name
            if "trash" not in found or "all" not in found:
                raise SafetyError(f"Could not identify Gmail Trash/All Mail folders: {found}")
            self._special = found
        return self._special

    # ---- guarded UID command ---------------------------------------------

    def _uid(self, command, *args):
        cmd = command.upper()
        if cmd in ("SEARCH", "FETCH"):
            pass
        elif cmd == "COPY":
            # Only for undoing an archive: copying All Mail -> INBOX just re-adds the Inbox label.
            if self.readonly:
                raise SafetyError("COPY on read-only selection")
            if not (self.selected == self._special_folders()["all"] and args[1] == "INBOX"):
                raise SafetyError(f"COPY {self.selected} -> {args[1]} not allowed")
        elif cmd == "MOVE":
            if self.readonly:
                raise SafetyError("MOVE on read-only selection")
            dest = args[1]
            sp = self._special_folders()
            # INBOX -> All Mail is Gmail's archive (drops the Inbox label);
            # INBOX -> Trash is "move to Trash"; Trash -> INBOX is undo.
            ok = (self.selected == "INBOX" and dest in (_quote(sp["trash"]), _quote(sp["all"]))) or \
                 (self.selected == sp["trash"] and dest == "INBOX")
            if not ok:
                raise SafetyError(f"MOVE {self.selected} -> {dest} not allowed")
        else:
            raise SafetyError(f"UID {cmd} not allowed")
        typ, data = self.conn.uid(cmd, *args)
        if typ != "OK":
            raise RuntimeError(f"UID {cmd} failed: {data}")
        return data

    # ---- reading INBOX ---------------------------------------------------

    def _require_inbox(self):
        if self.selected != "INBOX":
            raise SafetyError("INBOX not selected")

    def all_uids(self):
        self._require_inbox()
        data = self._uid("SEARCH", None, "ALL")
        return [int(x) for x in (data[0] or b"").split()]

    def stop_requested(self):
        return self._cancel is not None and self._cancel.is_set()

    def _check_cancel(self):
        if self.stop_requested():
            raise Cancelled()

    def msgid_map(self, stoppable=True):
        """{gmail msgid: uid} for every message currently in INBOX."""
        self._require_inbox()
        out = {}
        uids = self.all_uids()
        for chunk in _chunks(uids, 2000):
            if stoppable:
                self._check_cancel()
            data = self._uid("FETCH", _uidset(chunk), "(X-GM-MSGID)")
            for item in data:
                if not isinstance(item, bytes):
                    continue
                u = re.search(rb"UID (\d+)", item)
                g = re.search(rb"X-GM-MSGID (\d+)", item)
                if u and g:
                    out[g.group(1).decode()] = int(u.group(1))
        return out

    def highest_modseq(self):
        """INBOX's current HIGHESTMODSEQ (CONDSTORE, RFC 7162), or None if unavailable.

        Gmail bumps this number whenever any message's flags or labels change, so
        remembering it lets the next refresh ask only for what changed since.
        """
        self._require_inbox()
        try:
            typ, data = self.conn.status("INBOX", "(HIGHESTMODSEQ)")
        except Exception:
            return None
        m = re.search(rb"HIGHESTMODSEQ (\d+)", data[0] or b"") if typ == "OK" and data else None
        return int(m.group(1)) if m else None

    def fetch_flags(self, uids, progress=None):
        """{uid: (flags, labels)} for the given UIDs — the full read-state check."""
        self._require_inbox()
        out = {}
        done = 0
        for chunk in _chunks(uids, 2000):
            self._check_cancel()
            data = self._uid("FETCH", _uidset(chunk), "(FLAGS X-GM-LABELS)")
            out.update(_parse_flags(data))
            done += len(chunk)
            if progress:
                progress(done, len(uids))
        return out

    def fetch_flags_changed_since(self, modseq):
        """{uid: (flags, labels)} for only the INBOX messages changed after `modseq`."""
        self._require_inbox()
        data = self._uid("FETCH", "1:*", "(FLAGS X-GM-LABELS)", f"(CHANGEDSINCE {int(modseq)})")
        return _parse_flags(data)

    def fetch_headers(self, uids, progress=None):
        """Yield dicts of metadata + raw header bytes for the given UIDs."""
        self._require_inbox()
        done = 0
        items = (
            "(UID X-GM-MSGID X-GM-THRID X-GM-LABELS FLAGS RFC822.SIZE INTERNALDATE "
            f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
        )
        for chunk in _chunks(uids):
            self._check_cancel()
            data = self._uid("FETCH", _uidset(chunk), items)
            for rec in parse_fetch(data):
                yield rec
            done += len(chunk)
            if progress:
                progress(done, len(uids))

    # ---- changing INBOX --------------------------------------------------

    def _targets(self, gm_msgids):
        """Resolve Gmail msgids to UIDs *currently in INBOX*. Anything else is ignored."""
        current = self.msgid_map()
        uids, found, missing = [], [], []
        for g in gm_msgids:
            if g in current:
                uids.append(current[g])
                found.append(g)
            else:
                missing.append(g)
        return uids, found, missing

    def _move_out(self, gm_msgids, dest, progress=None):
        """MOVE the given messages out of INBOX, then re-read INBOX to confirm they left.

        Returns (moved, still_in_inbox, missing): `moved` is only what actually left INBOX.
        Stop is honoured between batches of 500: batches already sent stay done, and the
        check afterwards still runs so the result (and the action log) is accurate.
        """
        if self.readonly or self.selected != "INBOX":
            raise SafetyError("needs INBOX opened read-write")
        uids, found, missing = self._targets(gm_msgids)  # may raise Cancelled: nothing moved yet
        done = 0
        for chunk in _chunks(uids):
            if self.stop_requested():
                break
            self._uid("MOVE", _uidset(chunk), _quote(dest))
            done += len(chunk)
            if progress:
                progress(done, len(uids))
        now = self.msgid_map(stoppable=False)
        still = [g for g in found if g in now]
        moved = [g for g in found if g not in now]
        return moved, still, missing

    def archive(self, gm_msgids, progress=None):
        """Gmail archive: MOVE INBOX -> All Mail, which only drops the Inbox label.

        (Removing \\Inbox via X-GM-LABELS while INBOX is selected is silently ignored by Gmail.)
        """
        return self._move_out(gm_msgids, self._special_folders()["all"], progress)

    def trash(self, gm_msgids, progress=None):
        """Move to Gmail Trash (recoverable for 30 days). Never a permanent delete."""
        return self._move_out(gm_msgids, self._special_folders()["trash"], progress)

    # ---- undo (the only time a non-INBOX folder is opened) ---------------

    def _find_in_selected(self, gm_msgids):
        uids = []
        for chunk in _chunks(list(gm_msgids), 50):
            self._check_cancel()  # only while searching; once undo starts moving it finishes
            crit = _or_msgids(chunk)
            data = self._uid("SEARCH", None, crit)
            uids += [int(x) for x in (data[0] or b"").split()]
        return uids

    def undo_archive(self, gm_msgids):
        """Put exactly these messages back in INBOX (COPY All Mail -> INBOX re-adds the Inbox label)."""
        self._select(self._special_folders()["all"], readonly=False)
        try:
            uids = self._find_in_selected(gm_msgids)
            for chunk in _chunks(uids):
                self._uid("COPY", _uidset(chunk), "INBOX")
            return len(uids)
        finally:
            self._select("INBOX", readonly=True)

    def undo_trash(self, gm_msgids):
        """Move exactly these messages from Trash back to INBOX."""
        self._select(self._special_folders()["trash"], readonly=False)
        try:
            uids = self._find_in_selected(gm_msgids)
            for chunk in _chunks(uids):
                self._uid("MOVE", _uidset(chunk), "INBOX")
            return len(uids)
        finally:
            self._select("INBOX", readonly=True)


# ---- helpers ---------------------------------------------------------------

def _quote(name):
    if name == "INBOX":
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _or_msgids(ids):
    ids = [str(int(i)) for i in ids]
    crit = f"X-GM-MSGID {ids[-1]}"
    for i in reversed(ids[:-1]):
        crit = f"OR X-GM-MSGID {i} {crit}"
    return f"({crit})"


def _paren(meta, key):
    """Extract the parenthesised list after `key` and split it into tokens."""
    i = meta.find(key + " (")
    if i < 0:
        return []
    i += len(key) + 2
    depth, j = 1, i
    in_q = False
    while j < len(meta) and depth:
        c = meta[j]
        if c == '"' and meta[j - 1] != "\\":
            in_q = not in_q
        elif not in_q:
            depth += c == "("
            depth -= c == ")"
        j += 1
    body = meta[i:j - 1]
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', body)
    return [(q.replace('\\"', '"').replace("\\\\", "\\") if q else b) for q, b in toks]


def _parse_flags(data):
    out = {}
    for item in data or []:
        if isinstance(item, tuple):
            item = item[0]
        if not isinstance(item, bytes):
            continue
        meta = item.decode("utf-8", "replace")
        u = re.search(r"UID (\d+)", meta)
        if u:
            out[int(u.group(1))] = (_paren(meta, "FLAGS"), _paren(meta, "X-GM-LABELS"))
    return out


def parse_fetch(data):
    """Turn imaplib FETCH output (tuples + trailing bytes) into records."""
    recs = []
    cur = None
    for item in data:
        if isinstance(item, tuple):
            cur = {"meta": item[0].decode("utf-8", "replace"), "header": item[1]}
            recs.append(cur)
        elif isinstance(item, bytes) and cur is not None:
            # Items the server sent after the header literal, e.g. b' FLAGS (\\Seen))'
            cur["meta"] += " " + item.decode("utf-8", "replace")
    out = []
    for r in recs:
        m = r["meta"]

        def num(key):
            x = re.search(key + r" (\d+)", m)
            return x.group(1) if x else None

        idate = re.search(r'INTERNALDATE "([^"]+)"', m)
        out.append({
            "uid": int(num("UID") or 0),
            "gm_msgid": num("X-GM-MSGID"),
            "gm_thrid": num("X-GM-THRID"),
            "size": int(num("RFC822.SIZE") or 0),
            "internaldate": idate.group(1) if idate else "",
            "flags": _paren(m, "FLAGS"),
            "labels": _paren(m, "X-GM-LABELS"),
            "header": r["header"],
        })
    return out
