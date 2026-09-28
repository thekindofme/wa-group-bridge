#!/usr/bin/env python3
"""PreToolUse hook for Claude Code and Codex: block agent access to WhatsApp's storage.

Agents may use the `wag` CLI (which reads only ~/wa-exposed). Any tool call whose
path, pattern or shell command points at WhatsApp's own data folders is refused.
Exit code 2 + stderr is the blocking protocol for both Claude Code and Codex.
"""
import json
import re
import sys

BLOCK = re.compile(
    r"net\.whatsapp"                      # Containers/net.whatsapp.*, group.net.whatsapp.*
    r"|ChatStorage\.sqlite"
    r"|Group\s*Containers.*whats\s*app"
    r"|Containers/[^\s'\"]*whatsapp"
    r"|Caches/[^\s'\"]*whatsapp",
    re.IGNORECASE)
# File *content* being written is not an access attempt; don't scan it.
SKIP_KEYS = {"content", "new_string", "old_string", "edits", "transcript_path", "cwd",
             "session_id", "hook_event_name", "permission_mode", "prompt"}


def strings(obj, key=None):
    if key in SKIP_KEYS:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from strings(v, key)


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    target = payload.get("tool_input", payload)
    for s in strings(target):
        if BLOCK.search(s):
            print("Blocked: WhatsApp's own storage is off-limits to agents. "
                  "Use the `wag` CLI to read the exposed group (run `wag --help`).",
                  file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
