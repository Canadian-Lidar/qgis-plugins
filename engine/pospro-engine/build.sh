#!/bin/sh
# Build the PosPro engine for Linux (gcc) and Windows x64 (zig cc).
set -e
cd "$(dirname "$0")"
DEFS="-DENAGLO -DENAGAL -DENAQZS -DENACMP -DENAIRN -DNFREQ=3 -DNEXOBS=3"
SRC="src/pp_main.c src/pp_util.c src/pp_mcap.c src/pp_extract.c src/pp_gnss.c src/pp_ins.c src/pp_tc.c src/pp_combine.c src/pp_export.c src/pp_navcheck.c"
RTK="rtklib/rtkcmn.c rtklib/rinex.c rtklib/ephemeris.c rtklib/preceph.c rtklib/sbas.c rtklib/ionex.c rtklib/rtkpos.c rtklib/ppp.c rtklib/ppp_ar.c rtklib/lambda.c rtklib/pntpos.c rtklib/postpos.c rtklib/options.c rtklib/solution.c rtklib/geoid.c rtklib/tides.c rtklib/datum.c rtklib/convkml.c rtklib/convgpx.c rtklib/rtcm.c rtklib/rtcm2.c rtklib/rtcm3.c rtklib/rtcm3e.c"
mkdir -p bin
OUT=${1:-all}
if [ "$OUT" = all ] || [ "$OUT" = linux ]; then
  gcc -O2 -std=gnu99 -w $DEFS -Isrc -Irtklib $SRC $RTK zstd/zstddeclib.c -o bin/pospro-engine -lm -lpthread
fi
if [ "$OUT" = all ] || [ "$OUT" = windows ]; then
  python3 -m ziglang cc -target x86_64-windows-gnu -O2 -std=gnu99 -w $DEFS -Isrc -Irtklib $SRC $RTK zstd/zstddeclib.c win/pospro.rc -o bin/pospro-engine.exe -lws2_32 -lwinmm -static
fi
# Hatanaka (compact RINEX) decompressor, RNXCMP (GSI Japan)
if [ "$OUT" = all ] || [ "$OUT" = linux ]; then
  gcc -O2 -w rnxcmp/crx2rnx.c -o bin/crx2rnx
fi
if [ "$OUT" = all ] || [ "$OUT" = windows ]; then
  python3 -m ziglang cc -target x86_64-windows-gnu -O2 -w rnxcmp/crx2rnx.c win/pospro.rc -o bin/crx2rnx.exe -static
fi
