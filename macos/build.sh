#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]]
python -m pip install --no-cache-dir -r macos/requirements-resolved.txt
python -m pip install --no-cache-dir pyinstaller==6.22.2 pytest
python -m pip check
python -m pytest -q
python macos/build_ffmpeg.py
python macos/prepare_bundle.py
python -m PyInstaller --noconfirm --clean macos/shengnian.spec
app="$PWD/dist/声年.app"
exe="$app/Contents/MacOS/Shengnian"
export VOICE_JOURNAL_DATA_ROOT="$PWD/.test-data/frozen-mac"
unset VOICE_JOURNAL_CONFIG || true
"$exe" --role mac-self-test --mode basic
"$exe" --role mac-self-test --mode models
"$exe" --role mac-self-test --mode ui
codesign --verify --deep --strict --verbose=2 "$app"
mkdir -p release dist/dmg-source
ditto "$app" "dist/dmg-source/声年.app"
ln -s /Applications dist/dmg-source/Applications
cp macos/INSTALL.txt dist/dmg-source/安装说明.txt
hdiutil create -volname "声年" -srcfolder dist/dmg-source -format UDZO -ov release/shengnian-macos-arm64.dmg
shasum -a 256 release/shengnian-macos-arm64.dmg > release/shengnian-macos-arm64.dmg.sha256
