#!/bin/sh
# Install wag + wag-sync on PATH and start the 5-minute background sync.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
printf '#!/bin/sh\nexec /usr/bin/python3 "%s/wag.py" "$@"\n' "$HERE" > "$BIN/wag"
"$HERE/build.sh"
rm -f "$BIN/wag-sync"  # syncing goes through launchd (see `wag refresh`), not agents
chmod +x "$BIN/wag"
PLIST="$HOME/Library/LaunchAgents/com.yasith.wag-sync.plist"
sed -e "s|@HERE@|$HERE|" -e "s|@HOME@|$HOME|g" "$HERE/launchd/com.yasith.wag-sync.plist.in" > "$PLIST"
launchctl bootout "gui/$(id -u)/com.yasith.wag-sync" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $BIN/wag, $HERE/bin/wag-sync, launchd job com.yasith.wag-sync (every 5 min)"
