#!/bin/sh
# Build the CloudPro engine library for Linux x64 (glibc 2.17 and later) and Windows x64 with zig cc
# (pip install ziglang),
# straight into the plugin: plugins/CloudPro/bin/linux/libcpengine.so, plugins/CloudPro/bin/windows/cpengine.dll
# Baseline x86-64 (no -march=native) and no floating-point contraction, so the results match the Python code.
set -e
cd "$(dirname "$0")"
OUT=../../plugins/CloudPro/bin
FLAGS="-O2 -std=gnu99 -ffp-contract=off -fno-fast-math -Wall -Wno-unused-function"
mkdir -p $OUT/linux $OUT/windows
T=${1:-all}
if [ "$T" = all ] || [ "$T" = linux ]; then
  python3 -m ziglang cc -target x86_64-linux-gnu.2.17 $FLAGS -shared -fPIC -fvisibility=hidden src/cp_engine.c \
    -o $OUT/linux/libcpengine.so -lm
fi
if [ "$T" = all ] || [ "$T" = windows ]; then
  python3 -m ziglang cc -target x86_64-windows-gnu $FLAGS -shared src/cp_engine.c -o $OUT/windows/cpengine.dll
  rm -f $OUT/windows/*.lib $OUT/windows/*.pdb
fi
