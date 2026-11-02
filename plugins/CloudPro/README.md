# CloudPro 1.2 – point cloud processing, stage 03 of the DCS chain

**Plugins ▸ Canadian Lidar ▸ Data Collection Systems ▸ CloudPro**

```
JobFolder/  (mapper captures, never changed)
  Processing/<Name>/<Name>.dcsproj      one project per job, shared by the three plugins
     01_PosPro/out/trajectory.json   ──┐   trajectory (SBET + SMRMSG at the Certus reference point)
     02_AlignPro/out/alignment.json  ──┤   calibrated mounting, Lidar lever arm, latency
     03_CloudPro/                      ▼
        cloudpro.json  filters/  work/ (point store, rebuildable)  log/
        out/  <Name>.las  <Name>_ground.tif  cloud_report.txt  cloud.json
```

CloudPro opens the job's shared project and reads the .clraw captures recorded in it. It georeferences every Lidar return at its own firing time, using the trajectory PosPro published and the mounting, lever arm and latency AlignPro published. It then gives fast manual and automatic cleaning, and publishes the delivered cloud with `cloud.json`.

## Install

Install **DCS Core first**, then PosPro, AlignPro and CloudPro: **Plugins ▸ Manage and Install Plugins ▸ Install from ZIP**, one zip at a time, then restart QGIS once. Without DCS Core, CloudPro shows a message asking for it.

It needs QGIS 3.34 or later with NumPy and SciPy. Zstandard for reading .clraw is bundled for Windows Python 3.12 and 3.13; elsewhere CloudPro uses the zstd library that ships with QGIS/GDAL.

## Working on a job

1. **Open the job.** Use the project button in the header (next to MENU) ▸ *Open project…* on the job's `.dcsproj`, or click CloudPro in another plugin's chain bar. *New job project…* creates the shared project when CloudPro is the first plugin used. If the captures have moved (a new drive letter, a copied folder), CloudPro asks for the job folder once and relocates.
2. **Georeference.** The dialog shows what will be used:
   - **Trajectory:** PosPro's published trajectory. Until PosPro has run, the Certus real-time track is used, with a warning.
   - **Mounting:** AlignPro's calibration (mounting, Lidar lever arm, latency). Without AlignPro, the mounting recorded in the capture is used and every result says **no calibration**.
   - **Points:** returns, range gate, thinning, optional geoid grid.
3. **Clean and classify** with the views, the manual tools and the filter groups (below).
4. **Update georeferencing.** When PosPro or AlignPro publish again, the chain bar shows CloudPro ⚠ out of date and the header shows an *Update* button. The points are rebuilt with the new inputs, and every manual and filter edit is carried over, matched by firing time, laser and return.
5. **Publish.** This writes `<Name>.las` (LAS 1.4) and optionally the ground grid to `03_CloudPro/out/`, all or nothing. `cloud.json` is written last. It records:
   - the trajectory and alignment ids the points were really georeferenced with, so CloudPro shows out of date if they changed;
   - "no calibration" when AlignPro was not used;
   - the filter groups applied with their settings;
   - point and class counts, the output files, CRS and heights.

   `cloud_report.txt` has the same information in readable form.

**Rules of the chain:**
- The raw captures are never changed.
- `work/` can be deleted at any time; *Georeference* rebuilds it.
- While a job's cloud is open for editing, a second QGIS refuses to edit it (`.edit.lock`).
- Imports, whole-project filter runs and publishing hold the stage lock.
- Each run writes a log in `log/`.
- *Save the QGIS project in the project folder* writes `<Name>.qgz` next to the `.dcsproj`.

Older stand-alone projects (`<name>.cloudpro.json`) still open, without the chain.

## The window

```
 logo · MENU · PROJECT · PROPERTIES ..................... chain bar · inputs · Update
 tools: hand · selection shape · above/below line · ruler · i | action · inside/outside · class · line | undo · redo · rejected · zoom
 ┌──────────── PLAN ────────────┬───────────── 3D ──────────────┐
 │ project grid, drive lines,   │ orbit, pan, fly, eye-dome     │
 │ Inspection Area              │ lighting                      │
 ├──────────── SLICE ───────────┼── AUTOMATIC FILTERS · LINES ──┤
 │ cross-section or profile,    │   CLASSES · INFO · HISTORY    │
 │ slice bar on top             │                               │
 └──────────────────────────────┴───────────────────────────────┘
```

- **MENU:** Georeference / update, Publish, Export, Save, Pick the Inspection Area on the QGIS map, add the drive lines and Inspection Area to QGIS, add other .clraw files, remove all points, keys and mouse.
- **PROPERTIES** opens a window that can stay open (or be minimised) while you work:
  - *Project information:* files and folders, created / saved, survey start, end, time recording, number of lines, survey length, points, extent, centre in latitude / longitude.
  - *Geodetics:* horizontal CRS (Select…), trajectory frame and epoch, ellipsoidal or orthometric heights with a geoid grid; optionally the job's shared CRS. Applying re-georeferences and keeps every edit; the raw captures are never touched.
  - *Survey configuration:* the hardware recorded in the captures (Lidar, Certus, connections, lever arm, mounting), PosPro's trajectory and AlignPro's calibration, and the values CloudPro georeferences with. Source can be the chain default (AlignPro if published, else the recorded mounting), the recorded mounting, or values you enter (mounting, Lidar lever arm, latency), all kept in the project, apart from the raw files.
  - *Project files:* one row per .clraw file with Points and Track tick boxes (show the file's points in the views, show its drive line in the Plan). Clicking a row selects that file's points and highlights its drive line.
- **Tool bar.** Drag the grip to move it or float it. Hover any icon for its name and key. The selection button cycles rectangle → polygon → lasso → brush when clicked (right click to choose); the line button cycles above / below a drawn line. The action button sets what the shape does: Reject, Accept, Select, Classify (with the class next to it) or Filter (runs the current filter group on the shape); the button next to it switches between the points *inside* or *outside* the shape.
- **Colours** are set per view (Height, Intensity, Classification, Line, **File**, Return, Laser, Time, Range…). Rejected points are red; in a preview, orange = will be rejected and green = re-accepted; selected points are white.

| View | What it shows |
| --- | --- |
| **Plan** | The whole project as a grid (mean, max, min height, span, hits, rejects, intensity, or none) with hill shading, the drive lines with direction arrows, and the points of the Inspection Area on top. With the Pan tool, drag the area's edge to move it, a corner to resize it, the round handle to rotate it; Shift+drag draws a new one. Right click: place the Inspection Area here, **Inspection Area properties…** (exact centre, size, bearing, steps, overlap, follow a line, go to E/N), place the slice here, align with the nearest line, zoom to the area or the project. |
| **Slice** | A cross-section across the Inspection Area or a profile along it (the across / along button, or rotate 90°), with an exact thickness and step, previous / next, a position slider and *whole area* for a profile of the full width. When a cross-section walks off the end of the area the area moves on with it, so a corridor can be walked one slice at a time. Drive-line positions are marked. |
| **3D** | The Inspection Area or just the slice, with top / along / across / oblique views and eye-dome lighting. |

## Navigation (precise moves)

- **Inspection Area properties** (right click in the Plan): exact centre, length, width and bearing; steps forward / back / left / right by a set distance or the length minus an overlap %; rotation by set degrees; follow a drive line (the area steps along its chainage and turns with it).
- **Slice:** PgUp / PgDn or ] / [ step it (Shift = 5 steps).
- **2-D views:** arrow keys pan by the set step, the wheel zooms at the cursor, Shift+wheel sets vertical exaggeration; the scale bar and grid labels are in project coordinates.
- **3-D:** W/S/A/D/Q/E fly by the set step; 1 top, 2 along, 3 across, 4 oblique, O orthographic; double click sets a new rotation centre.
- **Pick on map** (MENU): click the QGIS map to centre the Inspection Area there.

## Manual editing

Keys: Pan (Space), rectangle (G), polygon (P), lasso (L), brush (B), above line (U), below line (J), measure (M: distance, bearing, dH, dV, slope), info (I). Reject (X), Accept (V), Select (S), Classify (K), Filter (F). In 3D, right-drag draws a lasso or rectangle.

With **Select**, Shift adds to the selection; right click a view for *reject / accept / classify / run the filter group on the selection*; Delete rejects it, Ctrl+D clears it. Edits can be limited to one line. **H** shows or hides rejected points. Every edit is written to disk at once, undo/redo has no fixed limit (up to 60 M changed points are kept) and the history is logged in the project. Rejections keep their reason (manual, automatic, original), so **Reset modifications** can re-accept only, say, the automatic ones.

## Automatic filters

Choose a **group** from the list (its "?" explains it; the ☰ button adds, copies, renames, imports, exports or deletes groups) and a **filter** of the group from the second list: its settings fill the rest of the panel, and its "?" gives what it does, every setting and the source paper. Filters run in order; + adds one, − removes, the arrows reorder, the switch turns it off. **Live preview** recomputes the result in the background whenever a setting changes; **Pause** stops it; **Accept preview** commits it, and that can be undone. **Run** applies the group to the Inspection Area, the slice, the selected lines, or the entire project. Project-wide runs go tile by tile, with a buffer so neighbourhood filters see across tile edges.

Each filter can be limited to some classes and lines. Mode *Individually* filters each line on its own.

**Classic filters:** Reset modifications · Clip absolute height · Clip intensity (absolute / relative) · Clip range & sensor geometry · Clip laser channel / return · Clip time · Clip reference surface (a QGIS raster) · 95 % confidence level · Low hit count (2-D or 3-D) · Clip mean height · Lonely cells · Reducer (voxel) · Reducer (scan based) · Slope / spike · Statistical outlier (SOR) · Vertical plane · Surface fit · Isolated cluster · Inverted cloth (CSF) · Edit classification · Shift height.

**Added from the literature (see docs/REFERENCES.md):**

| Filter | Use it for |
| --- | --- |
| **Snow / rain / dust – DROR, LIOR, DIOR** | Winter and wet-weather noise near the sensor. DIOR (intensity gate + range-adaptive radius) removes snow while keeping signs, markings and thin structures. |
| **SMRF**, **PMF** ground | Fast ground classification in towns and on roads. The robust minimum surface means a few multipath "low points" no longer spoil it. |
| **Height above ground** | Rejecting low noise below the road; classifying low, medium and high vegetation (classes 3/4/5) or any height band. |
| **Geometric features** (eigenvalues) | Scattered (noise, vegetation), linear (wires, poles, edges), planar vertical (walls) and planar horizontal (ground, roofs). |
| **Vertical plane + behind** | Facades as class 6; reflections through glass and interior returns behind a facade as seen from the Lidar. |
| **Moving objects / ghosts** | Multi-pass: cars, cyclists and pedestrians seen in one pass and seen *through* in another. |
| **Vehicle self-hits** | A box around the Lidar in the vehicle frame. |

**Feature extraction (from the literature):**

| Filter | Finds | Class | Truth test (simulated street) |
| --- | --- | --- | --- |
| **Road markings** | Painted lines and symbols: Otsu threshold of intensity per distance band from the drive line | 68 | recall 99.9 %, precision 97.5 % |
| **Curbs** | 8–30 cm steps with a steep face, at least 2 m long | 69 | 87 % of curb faces; every point found within 0.3 m of a curb |
| **Poles** | Small isolated sections stacked ≥ 1.5 m in voxel layers; tree trunks under a crown left alone | 65 | 12 of 12 poles, recall 94 %, precision 99.6 % |
| **Traffic signs** | Retro-reflective, planar, upright, sign-sized clusters 1.2–7 m up, with the whole plate | 67 | 2 of 2, recall 100 %, precision 94 % |
| **Power lines** | High points with free space above and below, clustered, catenary fit | 14 | recall 94 %, precision 100 % |
| **Manhole covers** | Round blobs of different intensity in the road's intensity image, 0.4–1.2 m | 70 | 3 of 3, recall 94 %, precision 96 % |
| **Guardrails** | A straight, continuous band 0.4–1.2 m above ground along the road, empty above | 71 | 1 of 1, recall 91 %, precision 99 % |

On the same street without these objects (walls, parked cars, trees, snow) they found nothing. Run a ground filter first (the ready-made group starts with SMRF).

Default groups: *Mobile mapping – standard clean*, *Snow and rain*, *Ground and vegetation classes*, *Moving objects (multi-pass)*, *Feature extraction (road and street furniture)* and *Road surface QA*. Projects from earlier versions get the new ready-made groups added. Groups are saved with the project (`filters/groups.json`) and can be exported and imported as JSON to share between projects.

## Batch processing (no GUI)

```
python -m cloudpro.cli import  D:/Job/Processing/Street/Street.dcsproj            (PosPro + AlignPro inputs)
python -m cloudpro.cli update  D:/Job/Processing/Street/Street.dcsproj            (re-georeference, keep edits)
python -m cloudpro.cli filter  D:/Job/Processing/Street/Street.dcsproj --group "Mobile mapping - standard clean"
python -m cloudpro.cli publish D:/Job/Processing/Street/Street.dcsproj --dem 0.5
python -m cloudpro.cli info    D:/Job/Processing/Street/Street.dcsproj            (chain state + what is out of date)
```

Run these in the OSGeo4W shell from the QGIS plugins folder, which holds `dcs_core` and `cloudpro`.

## Point store

`03_CloudPro/work/store/tiles/t_i_j.cpt` holds 50 m tiles of 33-byte records: x/y/z in mm, UTC time, intensity, return, laser, class, status bits, line, range, azimuth and the applied height shift. Edits are written through a memory map. `work/store/grids/` holds the overview grids, `work/lines/` the sensor tracks at 20 Hz, and `filters/groups.json` the filter groups.

## Verified so far (simulation)

These tests used a simulated street from `tools/simulate_clraw.py`, written as real .clraw (zstd MCAP, Hesai dual-return packets at 5,000/s, ANPP 200 Hz) with a true SBET and a label for every return:

- **Georeferencing.** Ground-plane residual 6.7 mm RMS, which is the simulated range noise. Pass-to-pass offsets on walls and ground were 0.1–0.3 mm. The slope was recovered exactly through the NB Stereographic projection.
- **Import speed.** About 3 M points/s on 2 cores; 10 s of capture at the real rate takes 1.6 s.
- **Filters** on a 60 × 40 m area at the real point rate (7.2 M points). Standard clean group: 99 % of snow, 99.6 % of below-ground noise and 100 % of airborne noise removed, with 0 % of ground, buildings and poles lost and 3 % of the sparse tree crowns. Moving objects: 92 % of the moving car removed, 0 % of static objects and 0.9 % of vegetation. Ground filters: Type I 0.00 %, Type II 2.4–3.2 % (wall and pole bases).
- **Window (1.2).** The Plan / Slice / 3D / filters layout, Properties window, file toggles, colour by file, selection, Inspection Area properties and feature extraction run in headless QGIS 3.34 (`tests/qgis_chain_ui_test.py`). Under a hostile look (Qt "Windows" style, light palette, white application style sheet) all 121 visible widgets of the window stay dark (`tests/qgis_theme_audit.py`).
- **QGIS.** Loads in QGIS 3.34 next to DCS Core 1.0.1, PosPro 1.2.1 and AlignPro 1.1.0: one shared menu, and the chain bar opens the other plugins. Exported LAS opens in laspy with the EPSG:2953 WKT.
- **Chain** (`tests/dcs_chain_test.py`, `tests/qgis_chain_ui_test.py`):
  - georeference on PosPro's trajectory → publish (no calibration);
  - AlignPro publishes → CloudPro out of date → update keeps all 5,010 edits → publish (calibration applied);
  - PosPro re-runs → out of date;
  - publishing without updating stays out of date;
  - an older stand-alone project still opens.

## Limits to know

- **Not yet run on a real capture or a real PosPro / AlignPro run.** Check the first real job against the same capture in other software.
- Windows and QGIS 4 / Qt6: the code is written for both but has been tested on QGIS 3.34 under Linux only (the theme audit imitates the Windows style).
- Feature extraction has been tuned on simulated XT-32 data only. Expect to adjust intensity thresholds (signs, markings, manholes) on the first real job; the "?" of each filter explains every setting.
- The 3D view is software rendered, so it works on any PC without OpenGL drivers. It thins the display while you move and stays fluid up to about 2–3 M points on a typical laptop.
- The LAS scan-angle field is 0. The laser id goes in *user data* and the line in *point source id*.
- DROR's α has to match the azimuth step of the data: 0.18° for the XT-32 at 600 rpm, ×N when keeping every Nth packet.
- The .clraw reader and Lidar decoding are still CloudPro's own copy. The spec moves them into DCS Core later, together with AlignPro's.
