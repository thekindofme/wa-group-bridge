# wa-group-bridge

Expose **one** WhatsApp group to coding agents (Claude Code, Codex) as a read-only,
token-efficient CLI, without giving them access to the rest of WhatsApp.

```
WhatsApp Desktop ─► ChatStorage.sqlite (all chats; macOS-protected, agents blocked)
                        │  read-only, only the allow-listed group JID
                        ▼
      bin/wag-sync  (signed binary; script + allow-list compiled in; launchd every 5 min)
                        ▼
      ~/wa-exposed/<slug>/group.sqlite + media/   (FTS5 index, this group only)
                        ▼
      wag  (read-only CLI, no send capability)  ◄── agents, via the whatsapp-groups skill
```

## Pieces
| File | Role |
|---|---|
| `wag_sync.py` | Extractor. The only code that reads WhatsApp's database. Filters by group JID in SQL, copies only that group's media, and rebuilds the output atomically each run. Fails loudly if WhatsApp's schema changes. |
| `config.example.json` → `config.json` | Allow-list of groups (`slug`, `jid`) plus your display name. Copy the example and fill it in; `config.json` is git-ignored. |
| `build.sh` → `bin/wag-sync` | Compiles a small Swift runner with `wag_sync.py` and `config.json` embedded, runs Python in isolated mode (`-I -S`), and ad-hoc signs it. |
| `launchd/` + `install.sh` | Installs the `wag` shim in `~/.local/bin` and a LaunchAgent that runs `bin/wag-sync` every 5 minutes. |
| `wag.py` | The CLI. Plain `wag` prints an agent-oriented overview; `groups info recent search show thread links media stats people refresh`, each with examples in `--help`. |
| `guard/wa_guard.py` | PreToolUse hook that refuses any agent tool call that points at WhatsApp's folders. |
| `skill/SKILL.md` | Agent instructions, symlinked into `~/.claude/skills` and `~/.codex/skills`. |

## Install
macOS with WhatsApp Desktop, Xcode command-line tools (`swiftc`) and `/usr/bin/python3`.
```
cp config.example.json config.json   # add your group's slug + JID (see "Adding a group")
./install.sh                         # builds bin/wag-sync, installs wag, starts the launchd job
```
Then do the one-time setup below.

## Security model
Three layers, from weakest to strongest:
1. **Agent hooks and deny rules** (`guard/wa_guard.py`, plus `permissions.deny` in
   `~/.claude/settings.json`). These block normal and accidental access, and injected
   instructions that name the path. They match text, so a determined agent that
   obfuscates the path could get past them.
2. **The `wag` CLI can't send.** Nothing in this repo can write to WhatsApp.
3. **macOS privacy protection (TCC).** This is the real boundary. WhatsApp's data folder is
   protected by macOS: a process can only read it if the *app that launched it* has been
   granted access. Only `bin/wag-sync` should hold that permission. Its allow-list is
   compiled in, and any rebuild changes its code hash, so macOS makes you grant it again.
   Agents can't retarget it by editing files.

### One-time setup (you, in System Settings)
1. **Grant the sync binary access.** Open System Settings → Privacy & Security → Full Disk
   Access, click **+**, press ⌘⇧G, and paste
   `<repo>/bin/wag-sync`. Then run `wag refresh`.
2. **Revoke agent access.** The Claude app (and any terminal or agent app you've let read
   other apps' data) currently can read WhatsApp. Reset those grants:
   ```
   tccutil reset SystemPolicyAppData com.anthropic.claudefordesktop
   ```
   Do the same for Codex, Orca, cmux and Terminal if you run agents there. Remove them
   from Full Disk Access too if they're listed. If macOS later asks whether one of those
   apps may "access data from other apps", click **Don't Allow**.

After a rebuild (`./build.sh`, e.g. after editing `config.json`), re-add `bin/wag-sync`
in Full Disk Access.

## Adding a group
Add `{"slug": "...", "jid": "...@g.us"}` to `config.json` (find a group's JID with `sqlite3` on WhatsApp's `ChatStorage.sqlite`, `ZWACHATSESSION.ZCONTACTJID`), run `./build.sh`, and re-grant.
Agents then select it with `wag -g <slug> …`.

## Tests
```
python3 -m unittest tests/test_wag.py
```
The tests build a synthetic WhatsApp database with a decoy chat and assert that no decoy
text, names or media reach the output. They also cover replies, mentions, filters,
search stemming, threads, JSON mode and schema-drift failure.
