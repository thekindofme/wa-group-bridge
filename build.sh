#!/bin/sh
# Build the signed wag-sync binary: wag_sync.py + config.json are embedded at
# build time, so the only program with WhatsApp access can't be retargeted by
# editing files. Any rebuild changes its code hash, and macOS then requires you
# to re-grant it access in System Settings. That re-grant is the human checkpoint.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/config.json" ] || { echo "config.json missing: copy config.example.json to config.json and add your group" >&2; exit 1; }
python3 -c "import json; json.load(open('$HERE/config.json'))"
SCRIPT_B64=$(base64 < "$HERE/wag_sync.py" | tr -d '\n')
CONFIG_B64=$(python3 -c "import json,os,base64;c=json.load(open('$HERE/config.json'));c['out_dir']=os.path.expanduser(c['out_dir']);print(base64.b64encode(json.dumps(c).encode()).decode())")
sed -e "s|@SCRIPT_B64@|$SCRIPT_B64|" -e "s|@CONFIG_B64@|$CONFIG_B64|" \
  "$HERE/runner/main.swift.in" > "$HERE/runner/main.swift"
swiftc -O -o "$HERE/bin/wag-sync" "$HERE/runner/main.swift"
rm "$HERE/runner/main.swift"
codesign --force -s - --identifier com.yasith.wag-sync "$HERE/bin/wag-sync"
echo "built $HERE/bin/wag-sync ($(shasum -a 256 "$HERE/bin/wag-sync" | cut -c1-12))"
