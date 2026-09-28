"""End-to-end tests against a synthetic WhatsApp database with a decoy chat."""
import base64
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wag  # noqa: E402
import wag_sync  # noqa: E402

GROUP = "111@g.us"
DECOY = "999@g.us"
SECRET = "TOPSECRET-decoy-message"
NOW = time.time()


def pb_str(field, s):
    b = s.encode()
    return bytes([(field << 3) | 2, len(b)]) + b


def pb_bytes_long(field, b):
    key = (field << 3) | 2
    out = b""
    while key >= 0x80:
        out += bytes([(key & 0x7F) | 0x80])
        key >>= 7
    return out + bytes([key, len(b)]) + b


def build_source(root):
    os.makedirs(os.path.join(root, "Message", "Media", GROUP, "a"))
    os.makedirs(os.path.join(root, "Message", "Media", DECOY, "a"))
    with open(os.path.join(root, "Message", "Media", GROUP, "a", "pic.jpg"), "wb") as f:
        f.write(b"jpegbytes")
    with open(os.path.join(root, "Message", "Media", DECOY, "a", "secret.jpg"), "wb") as f:
        f.write(b"secret")
    db = sqlite3.connect(os.path.join(root, "ChatStorage.sqlite"))
    db.executescript("""
    CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
    CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INT, ZMESSAGEDATE REAL,
      ZMESSAGETYPE INT, ZISFROMME INT, ZTEXT TEXT, ZGROUPMEMBER INT, ZMEDIAITEM INT,
      ZSTANZAID TEXT, ZFROMJID TEXT, ZSTARRED INT);
    CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT,
      ZFILESIZE INT, ZMETADATA BLOB, ZMEDIAURL TEXT);
    CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INT, ZMEMBERJID TEXT,
      ZFIRSTNAME TEXT, ZCONTACTNAME TEXT);
    CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
    """)
    db.execute("INSERT INTO ZWACHATSESSION VALUES (1, ?, 'Test Group')", (GROUP,))
    db.execute("INSERT INTO ZWACHATSESSION VALUES (2, ?, 'Decoy Chat')", (DECOY,))
    alice_name = "+" + base64.b64encode(pb_str(1, "Alice") + b"\x10\x01").decode()
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (10, 1, '1000001@lid', ?, NULL)", (alice_name,))
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (11, 1, '2000002@lid', NULL, NULL)")
    db.execute("INSERT INTO ZWAGROUPMEMBER VALUES (12, 2, '3000003@lid', NULL, 'Mallory')")
    db.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '2000002@lid', 'Bob')")
    ts = lambda hours_ago: NOW - hours_ago * 3600 - wag_sync.APPLE_EPOCH
    reply_meta = (pb_str(5, "S1") + pb_str(6, "1000001@lid")
                  + pb_bytes_long(19, pb_str(1, "we should build a whiteboard tool")))
    db.execute("INSERT INTO ZWAMEDIAITEM VALUES (1, NULL, NULL, NULL, ?, NULL)", (reply_meta,))
    db.execute("INSERT INTO ZWAMEDIAITEM VALUES (2, ?, 'diagram of the plan', 9, NULL, NULL)",
               (f"Media/{GROUP}/a/pic.jpg",))
    db.execute("INSERT INTO ZWAMEDIAITEM VALUES (3, ?, 'decoy pic', 6, NULL, NULL)",
               (f"Media/{DECOY}/a/secret.jpg",))
    db.execute("INSERT INTO ZWAMEDIAITEM VALUES (4, NULL, NULL, NULL, NULL, 'https://example.com/x')")
    rows = [
        (1, 1, ts(50), 0, 0, "we should build a whiteboard tool", 10, None, "S1"),
        (2, 1, ts(49), 0, 0, "agreed @1000001 lets do it", 11, 1, "S2"),
        (3, 1, ts(5), 1, 0, None, 11, 2, "S3"),
        (4, 1, ts(2), 7, 1, "read this https://example.com/x", None, 4, "S4"),
        (5, 1, ts(1), 0, 0, "whiteboarding session tomorrow?", 10, None, "S5"),
        (6, 2, ts(1), 0, 0, SECRET, 12, 3, "D1"),
        (7, 1, ts(1), 6, 0, "1000001@lid", 11, None, "S6"),
    ]
    for r in rows:
        db.execute("INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,?,?,?,?,?,NULL,0)", r)
    db.commit()
    db.close()


def run_cli(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = wag.main(list(argv))
    return code, out.getvalue()


class WagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.src = os.path.join(cls.tmp, "wa")
        cls.out = os.path.join(cls.tmp, "exposed")
        build_source(cls.src)
        cfg = os.path.join(cls.tmp, "config.json")
        with open(cfg, "w") as f:
            f.write('{"out_dir": "%s", "me": "Yasith", "groups": [{"slug": "test", "jid": "%s"}]}'
                    % (cls.out, GROUP))
        with contextlib.redirect_stdout(io.StringIO()):
            assert wag_sync.main(["--config", cfg, "--wa-root", cls.src]) == 0
        wag.EXPOSED = cls.out

    def test_no_decoy_data_anywhere_in_output(self):
        for dirpath, _, files in os.walk(self.out):
            for fn in files:
                with open(os.path.join(dirpath, fn), "rb") as f:
                    data = f.read()
                for needle in (SECRET.encode(), b"Mallory", b"Decoy Chat", b"secret", DECOY.encode()):
                    self.assertNotIn(needle, data, f"{needle!r} leaked into {fn}")

    def test_only_group_media_copied(self):
        files = [f for _, _, fs in os.walk(os.path.join(self.out, "test", "media")) for f in fs]
        self.assertEqual(files, ["pic.jpg"])

    def test_names_replies_mentions(self):
        _, out = run_cli("recent", "--all")
        self.assertIn("Alice: we should build a whiteboard tool", out)
        self.assertIn('Bob (↩#1 Alice: "we should build a whiteboard tool"): agreed @Alice lets do it', out)
        self.assertIn("Yasith: read this https://example.com/x", out)
        self.assertIn("[image] diagram of the plan", out)
        self.assertNotIn("membership", out)  # system events hidden by default
        _, out = run_cli("recent", "--all", "--system")
        self.assertIn("membership change (by Alice)", out)

    def test_default_window_is_24h(self):
        _, out = run_cli("recent")
        self.assertIn("# 3 messages", out)
        self.assertNotIn("agreed", out)

    def test_search_stems_and_context(self):
        _, out = run_cli("search", "whiteboarding")
        self.assertIn("# 2 matches", out)  # 'whiteboard' and 'whiteboarding' both match
        _, out = run_cli("search", "diagram")  # captions are searchable
        self.assertIn(">#3", out)
        _, out = run_cli("search", "tomorrow", "-C", "1")
        self.assertIn(">#5", out)
        self.assertIn(" #4", out)

    def test_filters(self):
        _, out = run_cli("recent", "--all", "--from", "bob")
        self.assertIn("agreed", out)
        self.assertNotIn("Alice: we", out)
        _, out = run_cli("recent", "--all", "--from", "me")
        self.assertIn("read this", out)
        self.assertIn("# 1 messages", out)
        _, out = run_cli("recent", "--since", "3d", "--until", "24h")
        self.assertIn("agreed", out)
        self.assertNotIn("tomorrow", out)

    def test_thread_links_media_stats(self):
        _, out = run_cli("thread", "2")
        self.assertIn("thread of 2 messages rooted at #1", out)
        _, out = run_cli("links")
        self.assertIn("https://example.com/x", out)
        _, out = run_cli("media", "3")
        self.assertTrue(out.strip().endswith("media/a/pic.jpg"))
        code, _ = run_cli("media", "1")
        self.assertEqual(code, 2)
        _, out = run_cli("stats")
        self.assertIn("top senders:", out)

    def test_json_mode(self):
        import json
        _, out = run_cli("--json", "search", "agreed")
        rows = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(rows[0]["sender"], "Bob")
        self.assertEqual(rows[0]["reply_to"], 1)
        self.assertNotIn("sender_jid", rows[0])

    def test_schema_drift_fails_loudly(self):
        broken = os.path.join(self.tmp, "broken")
        os.makedirs(broken)
        db = sqlite3.connect(os.path.join(broken, "ChatStorage.sqlite"))
        db.execute("CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY)")
        db.close()
        with self.assertRaises(wag_sync.SchemaError):
            wag_sync.open_source(os.path.join(broken, "ChatStorage.sqlite"))

    def test_overview_and_groups(self):
        code, out = run_cli()
        self.assertEqual(code, 0)
        self.assertIn('test: "Test Group"', out)
        self.assertIn("wag search", out)
        _, out = run_cli("groups")
        self.assertIn("# 1 exposed group(s)", out)
        code, _ = run_cli("-g", "nope", "info")
        self.assertEqual(code, 2)

    def test_time_parsing(self):
        now = time.time()
        self.assertAlmostEqual(wag.parse_when("2h", now), now - 7200, delta=1)
        with self.assertRaises(wag.UserError):
            wag.parse_when("last tuesday")


if __name__ == "__main__":
    unittest.main()
