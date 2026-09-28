#!/usr/bin/env python3
"""Copy allow-listed WhatsApp groups out of WhatsApp Desktop's local database.

This is the only program that reads WhatsApp's own storage. It opens
ChatStorage.sqlite read-only, selects messages for the group JIDs listed in
config.json and nothing else, and writes one self-contained SQLite file (plus
copied media) per group into the exposed directory. Agents only ever see that
output, through the `wag` CLI.

Every run rebuilds the output from scratch into a temp file and atomically
swaps it in, so edits and deletions in WhatsApp are reflected and a crashed
run never leaves a half-written database behind.
"""
import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import sys
import time

# Under the signed wag-sync binary this runs via `python3 -c`, with no __file__.
HERE = os.path.dirname(os.path.abspath(globals().get("__file__", ".")))
WA_ROOT = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared")
APPLE_EPOCH = 978307200  # 2001-01-01 in unix seconds
SCHEMA_VERSION = 1

# ZMESSAGETYPE values observed in WhatsApp Desktop's Core Data store.
KINDS = {
    0: "text", 1: "image", 2: "video", 3: "audio", 4: "contact",
    5: "location", 6: "system", 7: "link", 8: "document", 10: "system",
    11: "gif", 12: "system", 14: "deleted", 15: "sticker", 46: "poll",
    63: "event", 66: "poll", 75: "pin",
}
SYSTEM_KINDS = {"system"}


class SchemaError(RuntimeError):
    """WhatsApp's storage no longer looks like what this script understands."""


# --- minimal protobuf reader (WhatsApp stores several fields as protobuf) ---

def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def pb_fields(b):
    """Return {field_number: [values]} for a protobuf blob, or {} if malformed."""
    out = {}
    i = 0
    try:
        while i < len(b):
            key, i = _varint(b, i)
            field, wire = key >> 3, key & 7
            if wire == 0:
                v, i = _varint(b, i)
            elif wire == 2:
                n, i = _varint(b, i)
                v = bytes(b[i:i + n])
                i += n
            elif wire == 1:
                v = bytes(b[i:i + 8])
                i += 8
            elif wire == 5:
                v = bytes(b[i:i + 4])
                i += 4
            else:
                return {}
            out.setdefault(field, []).append(v)
    except IndexError:
        return {}
    return out


def pb_str(fields, n):
    v = fields.get(n)
    if not v or not isinstance(v[0], bytes):
        return None
    try:
        return v[0].decode("utf-8")
    except UnicodeDecodeError:
        return None


def parse_reply(metadata):
    """(quoted_stanza_id, quoted_author_jid, quoted_text) from ZWAMEDIAITEM.ZMETADATA."""
    if not metadata:
        return None, None, None
    f = pb_fields(metadata)
    stanza, author = pb_str(f, 5), pb_str(f, 6)
    text = None
    if f.get(19):
        text = pb_str(pb_fields(f[19][0]), 1)
    return stanza, author, text


def decode_member_name(first_name):
    """ZWAGROUPMEMBER.ZFIRSTNAME is '+' + base64(protobuf{1: push name})."""
    if not first_name:
        return None
    if not first_name.startswith("+"):
        return first_name
    try:
        return pb_str(pb_fields(base64.b64decode(first_name[1:])), 1)
    except (ValueError, TypeError):
        return None


# --- extraction ---

REQUIRED = {
    "ZWACHATSESSION": {"Z_PK", "ZCONTACTJID", "ZPARTNERNAME"},
    "ZWAMESSAGE": {"Z_PK", "ZCHATSESSION", "ZMESSAGEDATE", "ZMESSAGETYPE",
                   "ZISFROMME", "ZTEXT", "ZGROUPMEMBER", "ZMEDIAITEM",
                   "ZSTANZAID", "ZFROMJID", "ZSTARRED"},
    "ZWAMEDIAITEM": {"Z_PK", "ZMEDIALOCALPATH", "ZTITLE", "ZFILESIZE",
                     "ZMETADATA", "ZMEDIAURL"},
    "ZWAGROUPMEMBER": {"Z_PK", "ZCHATSESSION", "ZMEMBERJID", "ZFIRSTNAME",
                       "ZCONTACTNAME"},
    "ZWAPROFILEPUSHNAME": {"ZJID", "ZPUSHNAME"},
}


def check_schema(src):
    for table, cols in REQUIRED.items():
        have = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
        missing = cols - have
        if missing:
            raise SchemaError(f"{table} is missing {sorted(missing)}")


def open_source(path):
    if not os.path.exists(path):
        raise SchemaError(f"WhatsApp database not found at {path}")
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    src.row_factory = sqlite3.Row
    check_schema(src)
    return src


def names_for(src, chat_pk):
    """jid -> display name, restricted to members of this one chat."""
    names = {}
    rows = src.execute(
        "SELECT g.ZMEMBERJID jid, g.ZCONTACTNAME contact, g.ZFIRSTNAME first,"
        " p.ZPUSHNAME push FROM ZWAGROUPMEMBER g"
        " LEFT JOIN ZWAPROFILEPUSHNAME p ON p.ZJID = g.ZMEMBERJID"
        " WHERE g.ZCHATSESSION = ?", (chat_pk,))
    for r in rows:
        name = r["contact"] or r["push"] or decode_member_name(r["first"])
        if r["jid"]:
            names[r["jid"]] = (name or "").strip() or None
    return names


def fallback_name(jid):
    return "~" + (jid.split("@")[0][-4:] if jid else "unknown")


MENTION = re.compile(r"@(\d{6,})")


def extract(src, jid, me_name="me"):
    chat = src.execute(
        "SELECT Z_PK, ZPARTNERNAME FROM ZWACHATSESSION WHERE ZCONTACTJID = ?",
        (jid,)).fetchone()
    if chat is None:
        raise SchemaError(f"group {jid} not found in WhatsApp database")
    chat_pk = chat["Z_PK"]
    names = names_for(src, chat_pk)
    member_jid = {r[0]: r[1] for r in src.execute(
        "SELECT Z_PK, ZMEMBERJID FROM ZWAGROUPMEMBER WHERE ZCHATSESSION = ?",
        (chat_pk,))}
    # Push names for anyone quoted or mentioned who isn't a current member.
    push = {}

    def name_of(j):
        if not j:
            return None
        if names.get(j):
            return names[j]
        if j not in push:
            r = src.execute("SELECT ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID = ?",
                            (j,)).fetchone()
            push[j] = r[0] if r and r[0] else None
        return push[j] or fallback_name(j)

    def resolve_mentions(text):
        if not text:
            return text

        def sub(m):
            for suffix in ("@lid", "@s.whatsapp.net"):
                j = m.group(1) + suffix
                if j in names or j in push or src.execute(
                        "SELECT 1 FROM ZWAPROFILEPUSHNAME WHERE ZJID = ?", (j,)).fetchone():
                    return "@" + name_of(j)
            return m.group(0)
        return MENTION.sub(sub, text)

    rows = src.execute(
        "SELECT m.Z_PK, m.ZMESSAGEDATE, m.ZMESSAGETYPE, m.ZISFROMME, m.ZTEXT,"
        " m.ZGROUPMEMBER, m.ZSTANZAID, m.ZSTARRED,"
        " i.ZMEDIALOCALPATH, i.ZTITLE, i.ZFILESIZE, i.ZMETADATA, i.ZMEDIAURL"
        " FROM ZWAMESSAGE m LEFT JOIN ZWAMEDIAITEM i ON i.Z_PK = m.ZMEDIAITEM"
        " WHERE m.ZCHATSESSION = ? ORDER BY m.ZMESSAGEDATE, m.Z_PK", (chat_pk,))

    messages = []
    for r in rows:
        kind = KINDS.get(r["ZMESSAGETYPE"], "other")
        if r["ZISFROMME"]:
            sender_jid, sender = None, me_name
        else:
            sender_jid = member_jid.get(r["ZGROUPMEMBER"])
            sender = name_of(sender_jid) if sender_jid else None
        text = r["ZTEXT"]
        caption = None
        link_url = None
        if kind in ("image", "video", "gif", "document", "audio"):
            caption = r["ZTITLE"]
        if kind == "link":
            link_url = r["ZMEDIAURL"]
        if kind == "deleted":
            text = None
        if kind == "system" and text and re.fullmatch(r"\d+@(lid|s\.whatsapp\.net)", text):
            text = f"membership change (by {name_of(text)})"
        if kind == "event" and r["ZMETADATA"]:
            ev = pb_fields(r["ZMETADATA"]).get(100)
            title = pb_str(pb_fields(ev[0]), 1) if ev else None
            if title:
                text = title
        q_stanza, q_author, q_text = parse_reply(r["ZMETADATA"])
        media_path = r["ZMEDIALOCALPATH"]
        # Only accept media paths inside this group's own media folder.
        if media_path and not media_path.startswith(f"Media/{jid}/"):
            media_path = None
        messages.append({
            "id": r["Z_PK"],
            "stanza": r["ZSTANZAID"],
            "ts": int(r["ZMESSAGEDATE"] + APPLE_EPOCH) if r["ZMESSAGEDATE"] else None,
            "sender": sender if sender else ("system" if kind in SYSTEM_KINDS else None),
            "sender_jid": sender_jid,
            "from_me": 1 if r["ZISFROMME"] else 0,
            "kind": kind,
            "text": resolve_mentions(text),
            "caption": resolve_mentions(caption),
            "link_url": link_url,
            "media_path": media_path,
            "media_size": r["ZFILESIZE"] or None,
            "starred": 1 if r["ZSTARRED"] else 0,
            "reply_stanza": q_stanza,
            "reply_sender": name_of(q_author) if q_author else None,
            "reply_text": resolve_mentions(q_text),
        })
    return {"jid": jid, "subject": chat["ZPARTNERNAME"], "messages": messages,
            "members": sorted({n for n in names.values() if n})}


# --- output ---

OUT_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY, stanza TEXT, ts INTEGER, sender TEXT, sender_jid TEXT,
  from_me INTEGER, kind TEXT, text TEXT, caption TEXT, link_url TEXT,
  media_path TEXT, media_file TEXT, media_size INTEGER, starred INTEGER,
  reply_to INTEGER, reply_stanza TEXT, reply_sender TEXT, reply_text TEXT);
CREATE INDEX messages_ts ON messages(ts);
CREATE INDEX messages_stanza ON messages(stanza);
CREATE TABLE members (name TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE messages_fts USING fts5(
  body, sender, content='', tokenize='porter unicode61 remove_diacritics 2');
"""


def write_group(data, out_dir, wa_root, copy_media=True):
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, "group.sqlite")
    tmp = final + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript(OUT_SCHEMA)
    by_stanza = {m["stanza"]: m["id"] for m in data["messages"] if m["stanza"]}
    media_dir = os.path.join(out_dir, "media")
    copied = 0
    for m in data["messages"]:
        media_file = None
        if m["media_path"]:
            src_file = os.path.join(wa_root, "Message", m["media_path"])
            rel = m["media_path"].split("/", 2)[2]  # drop 'Media/<jid>/'
            dest = os.path.join(media_dir, rel)
            if os.path.isfile(src_file):
                if copy_media and (not os.path.exists(dest)
                                   or os.path.getsize(dest) != os.path.getsize(src_file)):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy2(src_file, dest)
                    copied += 1
                if os.path.exists(dest):
                    media_file = os.path.join("media", rel)
        reply_to = by_stanza.get(m["reply_stanza"]) if m["reply_stanza"] else None
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["id"], m["stanza"], m["ts"], m["sender"], m["sender_jid"],
             m["from_me"], m["kind"], m["text"], m["caption"], m["link_url"],
             m["media_path"], media_file, m["media_size"], m["starred"],
             reply_to, m["reply_stanza"], m["reply_sender"], m["reply_text"]))
        body = " ".join(x for x in (m["text"], m["caption"], m["link_url"]) if x)
        if body:
            db.execute("INSERT INTO messages_fts(rowid, body, sender) VALUES (?,?,?)",
                       (m["id"], body, m["sender"] or ""))
    db.executemany("INSERT INTO members VALUES (?)", [(n,) for n in data["members"]])
    meta = {
        "schema_version": SCHEMA_VERSION, "jid": data["jid"],
        "subject": data["subject"], "synced_at": int(time.time()),
        "message_count": len(data["messages"]),
    }
    db.executemany("INSERT INTO meta VALUES (?,?)",
                   [(k, str(v)) for k, v in meta.items()])
    db.commit()
    db.close()
    os.replace(tmp, final)
    return {"messages": len(data["messages"]), "media_copied": copied}


def load_config(path=None, inline=None):
    if inline is not None:
        cfg = json.loads(inline)
    else:
        with open(path) as f:
            cfg = json.load(f)
    cfg["out_dir"] = os.path.expanduser(cfg.get("out_dir", "~/wa-exposed"))
    for g in cfg["groups"]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", g["slug"]):
            raise ValueError(f"bad slug {g['slug']!r}")
        if not g["jid"].endswith("@g.us"):
            raise ValueError(f"{g['jid']} is not a group JID")
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--config-json", help=argparse.SUPPRESS)
    ap.add_argument("--wa-root", default=WA_ROOT, help=argparse.SUPPRESS)
    ap.add_argument("--no-media", action="store_true")
    ap.add_argument("--me", default=os.environ.get("WAG_ME", "me"),
                    help="display name for your own messages")
    args = ap.parse_args(argv)
    cfg = load_config(args.config, args.config_json)
    try:
        src = open_source(os.path.join(args.wa_root, "ChatStorage.sqlite"))
        for g in cfg["groups"]:
            data = extract(src, g["jid"], me_name=cfg.get("me", args.me))
            res = write_group(data, os.path.join(cfg["out_dir"], g["slug"]),
                              args.wa_root, copy_media=not args.no_media)
            print(f"{g['slug']}: {res['messages']} messages, "
                  f"{res['media_copied']} media files copied")
    except (SchemaError, sqlite3.Error) as e:
        print(f"wag-sync: FAILED: {e}", file=sys.stderr)
        if "authorization denied" in str(e):
            print("wag-sync: macOS blocked access. Add bin/wag-sync to System Settings → "
                  "Privacy & Security → Full Disk Access (see README).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
