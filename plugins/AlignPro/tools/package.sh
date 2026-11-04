#!/bin/sh
# Package AlignPro for QGIS: alignpro-<version>.zip (Install from ZIP).
#
#   sh tools/package.sh [OUTDIR]        (OUTDIR defaults to the folder holding alignpro/)
#
# Refuses to build when docs/AlignPro_User_Guide_<version>.pdf is missing (make it with
# tools/build_guide.py), and removes older AlignPro_User_Guide_*.pdf from docs/ first.
set -eu

PLUGIN_DIR=$(cd "$(dirname "$0")/.." && pwd)
PARENT=$(dirname "$PLUGIN_DIR")
NAME=$(basename "$PLUGIN_DIR")
OUTDIR=${1:-$PARENT}
OUTDIR=$(cd "$OUTDIR" && pwd)

VERSION=$(sed -n 's/^version=//p' "$PLUGIN_DIR/metadata.txt" | head -n 1 | tr -d '[:space:]')
[ -n "$VERSION" ] || { echo "package: no version= in metadata.txt" >&2; exit 1; }

GUIDE="$PLUGIN_DIR/docs/AlignPro_User_Guide_$VERSION.pdf"
if [ ! -s "$GUIDE" ]; then
    echo "package: refusing to build AlignPro $VERSION: the user guide for this version is missing:" >&2
    echo "         $GUIDE" >&2
    echo "         Export the 'AlignPro — User Guide' doc to PDF, then run:" >&2
    echo "         python3 tools/build_guide.py RAW.pdf $VERSION" >&2
    exit 2
fi

for f in "$PLUGIN_DIR"/docs/AlignPro_User_Guide_*.pdf; do
    [ -e "$f" ] || continue
    if [ "$f" != "$GUIDE" ]; then
        echo "package: removing older guide $(basename "$f")"
        rm -f "$f"
    fi
done

find "$PLUGIN_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
ZIP="$OUTDIR/alignpro-$VERSION.zip"
rm -f "$ZIP"
(cd "$PARENT" && zip -qr "$ZIP" "$NAME" -x "*.pyc" "*/.DS_Store")
echo "$ZIP"
