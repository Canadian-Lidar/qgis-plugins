# AlignPro – Lidar boresight calibration (DCS stage 02)

AlignPro calibrates the Lidar mounting angles of the Canadian Lidar mobile mapper from its `.clraw`
captures. It is stage 02 of the Data Collection Systems (DCS) chain:

    01 PosPro (trajectory.json)  ->  02 AlignPro (alignment.json)  ->  03 CloudPro (cloud.json + LAS)

Menu: **Plugins ▸ Canadian Lidar ▸ Data Systems ▸ AlignPro**, plus a toolbar button.

## Install

Install **DCS Core** first, then AlignPro (Plugins ▸ Manage and Install Plugins ▸ Install from ZIP, one
zip at a time), then restart QGIS once. Without DCS Core, AlignPro shows a message instead of opening.
QGIS 3.34 or later (QGIS 4 / Qt6 supported). No extra Python packages.

## User guide

`docs/AlignPro_User_Guide_<version>.pdf` ships in the plugin. It opens from the **?** button at the top
right of the panel, the project box menu (**User guide (this version)**) and **Help ▸ Plugins ▸ AlignPro
user guide**. The guide matching the installed version opens; if it is missing, the newest one in `docs/`.

## Window and docking

AlignPro and its Data View open in DCS Core's standard window: floating the first time, docked only when you
choose Dock right, left, bottom or top from the Window menu (the ⤢ button in the header, or a right-click on
the title bar). Dragging never docks it. Each window comes back as you left it, also after QGIS restarts;
Reset window returns to the default. The header is the same as in PosPro and CloudPro: the workflow boxes,
the project box, the Window button and the user guide.

## The shared project

AlignPro works on the job's processing project, created by whichever plugin is used first:

    JobFolder/                          mapper captures, never modified
      Processing/<Name>/<Name>.dcsproj  the shared project
        01_PosPro/  out/trajectory.json + SBET/SMRMSG at the Certus reference point
        02_AlignPro/alignpro.json       AlignPro's settings and results
                    work/               line caches, grids, screenshots (may be deleted)
                    out/                alignment.json, its data files, PDF/ASCII reports
                    log/                one log per run

- **Captures**: the ones recorded in the shared project (usually by PosPro) are used; their `.clraw`
  files are found in each capture folder. If AlignPro is used first, it records them.
- **Trajectory**: PosPro's published `trajectory.json` (GPS week and leap seconds from it). Until PosPro
  has run, the Certus real-time trajectory is used for a quick look, and the result says so.
- **Starting mounting**: from the capture's hardware record in the shared project. AlignPro records it
  as the shared fact `lidar_mounting`.
- **Publishing**: Apply (on the Calibration page, the Data View or Analyze) writes `alignment.json` for
  CloudPro with the applied mounting (with SDEV), Lidar lever arm, latency, the correction relative to the
  recorded mounting, quality, and the trajectory it was solved on. The corrected lever arm stays in
  `alignment.json` only; **Copy for mapper** puts the angles into the mapper's settings.
- **Out of date**: the chain bar under the header shows PosPro / AlignPro / CloudPro. AlignPro turns
  stale when PosPro publishes a new trajectory, a raw capture changes, or the starting mounting is edited;
  CloudPro turns stale when AlignPro publishes. Stale results stay usable; re-run when needed.
- **Locks**: calibrating and publishing hold the AlignPro stage lock, so a second QGIS cannot run it at the
  same time. **Raw data moved?** Project menu ▸ Relocate the job folder.
- Older stand-alone `.alignpro.json` projects still open (without the chain).

## Using it

1. **Project box ▸ Open project…** (the `.dcsproj` PosPro made) or **New project…** (job folder + name).
2. **Data** – captures, trajectory (PosPro's by default), lines and their quality.
3. **Installation** – lever arm, latency, mounting, with the vehicle drawing.
4. **Quality** – time lock, lost packets, positioning and attitude accuracy, residuals, ranges.
5. **Pairs** – pair types, planar surfaces, area of interest for a single target.
6. **Calibration ▸ Auto calibrate** – all lines plus leave-one-line-out combinations; advised angles
   with SDEV; **Apply advised** publishes them; **Copy for mapper**.
7. **Data View · Fine Tune** – plan, profile and section of up to 400 000 points, re-drawn live while
   roll / pitch / heading, lever arm and latency are nudged (about 0.1 s per step), with a grey ghost of the
   recorded, applied or advised values. **Apply to project** publishes.
8. **Analyze** – sweep latency, lever arm or an angle and plot the error.
9. **Report** – PDF and ASCII in `02_AlignPro/out`, history of earlier calibrations, corrected LAS export.

## Conventions

Mounting angles use the mapper's own convention: `M = Rz(heading) Ry(pitch) Rx(−roll) Z0`,
roll + starboard down, pitch + bow up, heading + clockwise; vehicle frame X forward, Y starboard, Z down
from the Certus reference point. Near pitch 90° (the upright mounting 0/90/180) the convention is singular:
AlignPro warns and also reports the correction about the vehicle axes with its own SDEV.

## Headless use and tests

```
python3 tools/simulate_patch.py OUTDIR                       # synthetic captures + truth trajectory
python3 tests/dcs_chain_test.py OUTDIR WORKDIR                # DCS chain without QGIS
QT_QPA_PLATFORM=offscreen python3 tests/qgis_ui_test.py PROJECT.dcsproj OUTDIR
python3 tools/alignpro_cli.py PROJECT_DIR NAME CAPTURES...    # stand-alone, without DCS
```
DCS Core must be importable beside the `alignpro` folder (as in the QGIS plugins folder).

## Release

1. Bump `version=` in `metadata.txt` (and `VERSION` in `core/project.py`), update the "AlignPro — User Guide" doc.
2. Export the doc to PDF, then `python3 tools/build_guide.py RAW.pdf` (needs reportlab and PyMuPDF) makes
   `docs/AlignPro_User_Guide_<version>.pdf` with its cover page and running header.
3. `sh tools/package.sh [OUTDIR]` makes `alignpro-<version>.zip`. It refuses to build without the guide for
   the current version and removes older guide PDFs from `docs/`.
