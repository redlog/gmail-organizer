"""Turn raw header bytes into the flat row shown in the UI."""

import email
import email.policy
import re
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

import tldextract

# Set by configure() from the account chosen in `python app.py --setup`.
MY_ADDRESS = ""   # e.g. you@example.com
MY_DOMAIN = ""    # e.g. example.com

GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def is_gmail_address(account):
    return (account or "").rsplit("@", 1)[-1].lower() in GMAIL_DOMAINS


def configure(account):
    global MY_ADDRESS, MY_DOMAIN
    MY_ADDRESS = (account or "").lower()
    MY_DOMAIN = MY_ADDRESS.rsplit("@", 1)[-1] if "@" in MY_ADDRESS else ""


def is_mine(addr):
    """Is this one of the account owner's addresses?

    On your own domain (e.g. Google Workspace) every address at that domain can
    deliver to you, so any of them counts. On gmail.com only your own address does.
    """
    if not addr or not MY_ADDRESS:
        return False
    local, _, dom = addr.partition("@")
    if dom != MY_DOMAIN:
        return False
    if not is_gmail_address(MY_ADDRESS):
        return True
    my_local = MY_ADDRESS.partition("@")[0]
    return local == my_local or local.startswith(my_local + "+")

# Use the public-suffix list bundled with tldextract; never fetch it from the network.
_extract = tldextract.TLDExtract(suffix_list_urls=())

_ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def decode(value):
    if value is None:
        return ""
    try:
        s = str(make_header(decode_header(str(value))))
    except Exception:
        s = str(value)
    return re.sub(r"\s+", " ", s).strip()


def base_domain(addr_or_domain):
    d = addr_or_domain.rsplit("@", 1)[-1].lower().strip(" >.")
    if not d:
        return ""
    ext = _extract(d)
    top = getattr(ext, "top_domain_under_public_suffix", None)
    if top is None:
        top = ext.registered_domain
    return top or d


def find_recipient(msg):
    """The To address: which of your addresses this message was actually delivered to.

    Look in delivery headers first (most reliable for BCC / mailing lists, where the
    To: header shows something else), then To/Cc, then Received "for <...>" clauses.
    If several of your addresses appear, prefer one other than your main address.
    """
    found = []
    for h in ("X-Original-To", "Delivered-To", "To", "Cc"):
        for v in msg.get_all(h, []) or []:
            for _, a in getaddresses([decode(v)]):
                found.append(a.lower())
    for v in msg.get_all("Received", []) or []:
        for a in re.findall(r"for\s+<?([\w.+-]+@[\w.-]+)>?", str(v)):
            found.append(a.lower())
    mine = [a for a in found if is_mine(a)]
    for a in mine:
        if a != MY_ADDRESS:
            return a
    return mine[0] if mine else ""


def parse_unsubscribe(value, post_value):
    """Return (https_url, mailto, one_click)."""
    value = decode(value)
    urls = (re.findall(r"<([^>]+)>", value) or [value]) if value else []
    https = next((u.strip() for u in urls if u.strip().lower().startswith("https://")), "")
    mailto = next((u.strip() for u in urls if u.strip().lower().startswith("mailto:")), "")
    one_click = bool(https) and "one-click" in decode(post_value).lower()
    return https, mailto, one_click


def parse_date(hdr, internaldate):
    for fn in (
        lambda: parsedate_to_datetime(hdr),
        lambda: datetime.strptime(internaldate, "%d-%b-%Y %H:%M:%S %z"),
    ):
        try:
            d = fn()
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
    return ""


def to_row(rec):
    msg = email.message_from_bytes(rec["header"], policy=email.policy.compat32)
    from_raw = decode(msg.get("From", ""))
    from_name, from_addr = parseaddr(from_raw)
    from_addr = from_addr.lower()
    if not from_addr:
        m = _ADDR_RE.search(from_raw)
        from_addr = m.group(0).lower() if m else from_raw
    https, mailto, one_click = parse_unsubscribe(
        msg.get("List-Unsubscribe"), msg.get("List-Unsubscribe-Post"))
    list_id = decode(msg.get("List-Id", ""))
    m = re.search(r"<([^>]+)>", list_id)
    return {
        "gm_msgid": rec["gm_msgid"],
        "gm_thrid": rec["gm_thrid"],
        "uid": rec["uid"],
        "date": parse_date(msg.get("Date"), rec["internaldate"]),
        "from_name": from_name or "",
        "from_addr": from_addr,
        "from_domain": base_domain(from_addr) if "@" in from_addr else "",
        "recipient": find_recipient(msg),
        "to": decode(msg.get("To", "")),
        "cc": decode(msg.get("Cc", "")),
        "subject": decode(msg.get("Subject", "")),
        "list_id": m.group(1) if m else list_id,
        "unsub_url": https,
        "unsub_mailto": mailto,
        "unsub_one_click": int(one_click),
        "size": rec["size"],
        "unread": int("\\Seen" not in rec["flags"]),
        "labels": ", ".join(l for l in rec["labels"] if l not in ("\\Inbox",)),
        "message_id": decode(msg.get("Message-ID", "")),
    }
