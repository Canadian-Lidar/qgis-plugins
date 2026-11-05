#!/bin/sh
# Package RemixPro for QGIS: remixpro-<version>.zip (Install from ZIP).
#
#   sh tools/package.sh [OUTDIR]        (OUTDIR defaults to the folder holding remixpro/)
#
# Refuses to build when docs/RemixPro_User_Guide_<version>.pdf is missing (make it with
# tools/build_guide.py), and removes older RemixPro_User_Guide_*.pdf from docs/ first.
set -eu

PLUGIN_DIR=$(cd "$(dirname "$0")/.." && pwd)
PARENT=$(dirname "$PLUGIN_DIR")
NAME=$(basename "$PLUGIN_DIR")
OUTDIR=${1:-$PARENT}
OUTDIR=$(cd "$OUTDIR" && pwd)

VERSION=$(sed -n 's/^version=//p' "$PLUGIN_DIR/metadata.txt" | head -n 1 | tr -d '[:space:]')
[ -n "$VERSION" ] || { echo "package: no version= in metadata.txt" >&2; exit 1; }

GUIDE="$PLUGIN_DIR/docs/RemixPro_User_Guide_$VERSION.pdf"
if [ ! -s "$GUIDE" ]; then
    echo "package: refusing to build RemixPro $VERSION: the user guide for this version is missing:" >&2
    echo "         $GUIDE" >&2
    echo "         Build it with: python guides/build_guide.py remixpro" >&2

    exit 2
fi

for f in "$PLUGIN_DIR"/docs/RemixPro_User_Guide_*.pdf; do
    [ -e "$f" ] || continue
    if [ "$f" != "$GUIDE" ]; then
        echo "package: removing older guide $(basename "$f")"
        rm -f "$f"
    fi
done

find "$PLUGIN_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
ZIP="$OUTDIR/remixpro-$VERSION.zip"
rm -f "$ZIP"
(cd "$PARENT" && zip -qr "$ZIP" "$NAME" -x "*.pyc" "*/.DS_Store")
echo "$ZIP"
