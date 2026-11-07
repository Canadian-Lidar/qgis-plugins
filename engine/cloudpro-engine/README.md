# cloudpro-engine

Source of the CloudPro engine: a small C library with CloudPro's per-point loops. CloudPro loads it with ctypes
(`plugins/CloudPro/core/engine.py`); the calls release Python's lock, so CloudPro's worker threads run on every
core. Every function gives the same numbers as the Python code it replaces (`plugins/CloudPro/tests/test_engine.py`),
and CloudPro uses the Python code when the library is missing or `CLOUDPRO_NO_ENGINE=1` is set.

| Function | Replaces |
|---|---|
| `cp_georef_chunk` | `dcs_core.hesai.decode_packets` + `GeorefContext.points` + the record building in `importer.work` + `Store.bin` |
| `cp_mcap_payloads` | the message loop of `Recording.read_lidar_chunk` |
| `cp_grid_stats` | `Store.grid_stats` |
| `cp_ray_count` (+ `cp_table_*`) | the free-space ray loop of `filters.DynamicObjects` |
| `cp_cluster` | `features.cluster` (query_pairs + connected_components, same labels) |
| `cp_line_counts` | the RANSAC scores of `filters.VerticalPlane` |

Build both (zig: `pip install ziglang`), straight into the plugin:

    ./build.sh            # or: ./build.sh linux | ./build.sh windows

Linux x64 against glibc 2.17, Windows x64 against the Universal C Runtime (Windows 10 and later). Baseline x86-64
and `-ffp-contract=off`, so no fused multiply-add changes a result. When a function's arguments change, raise
`CP_ENGINE_VERSION` in `src/cp_engine.c` and `VERSION` in `engine.py` together: an older library is then not used.
