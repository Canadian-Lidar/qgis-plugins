# pospro-engine

Source of PosPro's processing engine (C). The plugin ships only the binaries, in
`plugins/PosPro/bin/linux/` and `plugins/PosPro/bin/windows/`; this folder is not part of any plugin zip.

Build both (Linux with gcc, Windows x64 with zig: `pip install ziglang`):

    ./build.sh            # or: ./build.sh linux | ./build.sh windows

then copy `bin/pospro-engine`, `bin/crx2rnx` to `plugins/PosPro/bin/linux/` and `bin/pospro-engine.exe`,
`bin/crx2rnx.exe` to `plugins/PosPro/bin/windows/`. Set `PP_VERSION` in `src/pp.h` to the plugin version first.

Vendored: RTKLIB-EX 2.5.1 (`rtklib/`), Zstandard decoder (`zstd/`), RNXCMP crx2rnx (`rnxcmp/`);
their licences are in `plugins/PosPro/LICENSE_*.txt`.
