# RemixPro

Files for BeamworX, Inertial Explorer and Qinertia from the Canadian Lidar mobile mapper's recordings. A side tool of the Data Collection Systems plugins (PosPro, AlignPro, CloudPro). It is not a numbered stage: it reads raw recordings, publishes no handover and writes no shared facts. Needs DCS Core 1.5.0 (API 6).

Mapper 2.10 no longer makes these files; RemixPro makes them on the office PC with the mapper's own writers (from the RemixPro handover kit, 4 Nov 2026). Its output matches the mapper's byte for byte, apart from the program name in the IMR header and the RINEX `PGM / RUN BY / DATE` line.

| Output | Files (per recording) | From |
|---|---|---|
| BeamworX | `beamworx/<clraw name>.bwxraw` + `.xml`, one per `.clraw` | survey lines |
| Inertial Explorer | `inertial_explorer/<name>.imr`, `.obs`, `_rx2.obs`, `.nav`, `README_INERTIAL_EXPLORER.txt` | lines, navigation recordings, Certus logs |
| Qinertia | `qinertia/<name>_imu.qimu`, `.obs`, `_rx2.obs`, `.nav`, `README_QINERTIA.txt` | lines, navigation recordings, Certus logs |

Inputs: a mapper project folder (2.10 `lines/`, `nav/`), a USB copy, an unzipped download, captures from before 2.10, single `.clraw` / `.anpp` files, or a DCS project. Output folder: `Processing/<Name>/RemixPro/` in a DCS project, `<Project>/Processing/RemixPro/` for a mapper project, else `RemixPro/` beside the inputs.

## Layout

```
core/       no Qt: sources (find recordings), make (run once, work folder, cancel), beamworx (.clraw -> .bwxraw),
            inertial (ANPP -> IMR / $qimu / RINEX), notes (setup notes), bwxraw + surveyconfig (mapper writers)
ui/         window.py: the PluginWindow
cli.py      python -m RemixPro.cli PATH ... [--out DIR] [--beamworx] [--ie] [--qinertia] [--list] [--force]
tests/      sim210.py (a mapper 2.10 project written with the mapper's own .clraw writer, mapper_clraw.py),
            test_same_as_mapper.py (byte for byte against the kit), test_writers.py (the mapper's tests),
            test_sources_make.py, ui_test.py (real Qt, stand-in QGIS)
tools/      package.sh
```

## Tests

From `plugins/`:

```
python -m unittest discover -s RemixPro/tests -t .      # needs numpy, zstandard
python RemixPro/tests/ui_test.py                        # needs PyQt5
```

`test_same_as_mapper.py` needs the handover kit unzipped (`REMIXPRO_KIT=<folder with mapper_converters/>`); the "same as NavAQ" test needs Sean's 2024 recording `0100_20240815_135643_NoLine.0001.bwxraw` (`REMIXPRO_NAVAQ=<path>` or `tests/data/`). Both are skipped without them.

User guide: `python guides/build_guide.py remixpro` (figures: `python guides/remixpro/shots.py`).
