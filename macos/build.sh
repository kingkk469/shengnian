#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]]
stage="${1:-all}"
case "$stage" in all|dependencies|test|decoder|resources|package|verify|dmg) ;; *) exit 2 ;; esac
if [[ "$stage" == all || "$stage" == dependencies ]]; then
python -m pip install --no-cache-dir -r macos/requirements-resolved.txt
python -m pip install --no-cache-dir pyinstaller==6.22.2 pytest
python -m pip check
fi
if [[ "$stage" == all || "$stage" == test ]]; then
python -m pytest -q
fi
if [[ "$stage" == all || "$stage" == decoder ]]; then
python macos/build_ffmpeg.py
fi
if [[ "$stage" == all || "$stage" == resources ]]; then
python macos/prepare_bundle.py
fi
if [[ "$stage" == all || "$stage" == package ]]; then
python -m PyInstaller --noconfirm --clean macos/shengnian.spec
fi
app="$PWD/dist/声年.app"
exe="$app/Contents/MacOS/Shengnian"
export VOICE_JOURNAL_DATA_ROOT="$PWD/.test-data/frozen-mac"
unset VOICE_JOURNAL_CONFIG || true
if [[ "$stage" == all || "$stage" == verify ]]; then
"$exe" --role mac-self-test --mode basic
"$exe" --role mac-self-test --mode models
"$exe" --role mac-self-test --mode ui
codesign --verify --deep --strict --verbose=2 "$app"
fi
if [[ "$stage" == all || "$stage" == dmg ]]; then
mkdir -p release dist/dmg-source
ditto "$app" "dist/dmg-source/声年.app"
ln -s /Applications dist/dmg-source/Applications
cp macos/INSTALL.txt dist/dmg-source/安装说明.txt
hdiutil create -volname "声年" -srcfolder dist/dmg-source -format UDZO -ov release/shengnian-macos-arm64.dmg
hdiutil verify release/shengnian-macos-arm64.dmg
mountpoint="$PWD/.test-data/dmg-mount"
mkdir -p "$mountpoint"
hdiutil attach -readonly -nobrowse -mountpoint "$mountpoint" release/shengnian-macos-arm64.dmg
trap 'hdiutil detach "$mountpoint"' EXIT
test -x "$mountpoint/声年.app/Contents/MacOS/Shengnian"
test -f "$mountpoint/安装说明.txt"
test "$(readlink "$mountpoint/Applications")" = /Applications
codesign --verify --deep --strict --verbose=2 "$mountpoint/声年.app"
hdiutil detach "$mountpoint"
trap - EXIT
shasum -a 256 release/shengnian-macos-arm64.dmg > release/shengnian-macos-arm64.dmg.sha256
fi
