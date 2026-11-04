# User guides of the DCS plugins

The PDF user guides shipped in each plugin's `docs/` folder are built from here. Keep this folder in
the repository next to `build_repo.py` (it is not part of any plugin zip).

```
guides/
  build_guide.py        builds one guide: cover, contents, running header, page numbers
  common/               chapters shared by all three guides (installing, the window, projects)
  pospro/ alignpro/ cloudpro/
    guide.html          the guide's text (chapters, tables, figure references)
    fig/                the figures: screenshots of the plugin's real windows
    shots.py            recreates every figure (see below)
  harness.py            runs the plugins outside QGIS for the screenshots
```

## Rebuild a guide after changing its text

```
pip install playwright pypdf
playwright install chromium
python guides/build_guide.py pospro        # or alignpro, cloudpro
```

The version comes from the plugin's `metadata.txt`; the PDF is written to the plugin's `docs/` as
`<Name>_User_Guide_<version>.pdf` and older guides there are removed. Raise the plugin version first,
then build, so the guide and the plugin match (the plugins open the guide named after their version).

In `guide.html`: `<!--#include install-->` (also `window`, `projects`) pulls in a shared chapter,
`<!--#vars {...}-->` fills its placeholders, `{N}` numbers chapters and figures, `{VERSION}` and
`{CORE_VERSION}` are filled in.

## Retake the screenshots

`shots.py` drives the real plugin (PyQt5 with a stand-in for QGIS) through its whole workflow on the
simulated two-pass test job and grabs each figure. It needs PyQt5, numpy and Pillow, the plugin folders
named as QGIS installs them (`pospro`, `alignpro`, `cloudpro`, `dcs_core`) beside this `guides` folder,
and the test job under `/jobs/<plugin>/` (made with AlignPro's `tools/simulate_patch.py`). Run it
after any change to a window the guide shows, then build the guide again.
