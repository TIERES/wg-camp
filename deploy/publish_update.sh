#!/usr/bin/env bash
# Publishes a new kailleraclient.dll build for the client's self-updater
# (app/updates.py / common/n02_update.cpp) to pick up. Run on the server as
# root, after downloading the two DLLs from a GitHub Release:
#
#   sudo bash deploy/publish_update.sh v.TIERES.0.14 kailleraclient-x64.dll kailleraclient-x86.dll
#
# Just copies files into place and writes version.txt - no Flask/app
# context needed (matches this project's existing convention of avoiding
# the Flask CLI in production - see db.py's migrate_db() docstring).
set -euo pipefail

if [ "$#" -ne 3 ]; then
    echo "Usage: $0 <version> <x64.dll> <x86.dll>" >&2
    exit 1
fi

VERSION="$1"
X64_DLL="$2"
X86_DLL="$3"
DIR=/var/www/arena17/updates

for f in "$X64_DLL" "$X86_DLL"; do
    if [ ! -f "$f" ]; then
        echo "Not a file: $f" >&2
        exit 1
    fi
done

mkdir -p "$DIR"
cp "$X64_DLL" "$DIR/kailleraclient-x64.dll"
cp "$X86_DLL" "$DIR/kailleraclient-x86.dll"
printf '%s' "$VERSION" > "$DIR/version.txt"
chown -R arena17:arena17 "$DIR"
chmod 750 "$DIR"
chmod 640 "$DIR"/*.dll "$DIR/version.txt"

echo "Published $VERSION:"
ls -la "$DIR"
