"""
Builds a QGIS plugin repository from the plugin folders in ./plugins.

For every folder in ./plugins that contains a metadata.txt, this script:
  1. Zips the folder (the zip keeps the folder name at its top level, as QGIS expects).
  2. Adds an entry for it to plugins.xml.
Output goes to ./site, which the GitHub Action publishes to GitHub Pages.

Run locally:  python build_repo.py https://<user>.github.io/<repo>
"""
import configparser
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "plugins"
OUT = ROOT / "site"
SKIP_DIRS = {"__pycache__", ".git", ".github", ".idea", ".vscode", "test", "tests"}
SKIP_EXT = {".pyc", ".pyo", ".zip"}
Q = {'"': "&quot;"}  # escape quotes inside XML attributes

base_url = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REPO_BASE_URL", "")).rstrip("/")
if not base_url:
    sys.exit("Give the Pages URL, e.g. python build_repo.py https://user.github.io/repo")


REQUIRED = ("name", "version", "qgisMinimumVersion", "description", "author", "email")


def read_metadata(folder: Path, problems: list):
    """Returns the metadata dict, or None after adding plain-English problems to the list."""
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # keep key case
    where = f"{folder.name}/metadata.txt"
    try:
        cp.read(folder / "metadata.txt", encoding="utf-8")
    except configparser.DuplicateOptionError as e:
        problems.append(f"{where}: '{e.option}' appears more than once (line {e.lineno}). Delete the extra line.")
        return None
    except configparser.DuplicateSectionError as e:
        problems.append(f"{where}: the [{e.section}] heading appears more than once. Keep only one.")
        return None
    except configparser.MissingSectionHeaderError:
        problems.append(f"{where}: the first line must be [general].")
        return None
    except configparser.Error as e:
        problems.append(f"{where}: can't be read ({e.message.splitlines()[0]}).")
        return None
    except UnicodeDecodeError:
        problems.append(f"{where}: isn't saved as UTF-8. In Notepad use File > Save As > Encoding: UTF-8.")
        return None
    if not cp.has_section("general"):
        problems.append(f"{where}: has no [general] heading on the first line.")
        return None
    md = dict(cp["general"])
    missing = [k for k in REQUIRED if not md.get(k, "").strip()]
    if missing:
        problems.append(f"{where}: missing {', '.join(missing)}.")
        return None
    return md


def make_zip(folder: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            rel = path.relative_to(folder.parent)
            if any(part in SKIP_DIRS for part in rel.parts) or path.suffix in SKIP_EXT:
                continue
            if path.is_file():
                zf.write(path, rel.as_posix())


def xml_entry(folder: Path, md: dict, zip_name: str, today: str) -> str:
    def tag(name, value):
        return f"    <{name}>{escape(str(value))}</{name}>"

    lines = [
        f'  <pyqgis_plugin name="{escape(md["name"], Q)}" version="{escape(md["version"], Q)}" plugin_id="{escape(folder.name, Q)}">',
        tag("description", md["description"]),
        tag("about", md.get("about", "")),
        tag("version", md["version"]),
        tag("qgis_minimum_version", md["qgisMinimumVersion"]),
        tag("qgis_maximum_version", md.get("qgisMaximumVersion", "3.99")),
        tag("homepage", md.get("homepage", "")),
        tag("file_name", zip_name),
        tag("author_name", md["author"]),
        tag("download_url", f"{base_url}/{zip_name}"),
        tag("uploaded_by", md["author"]),
        tag("create_date", today),
        tag("update_date", today),
        tag("experimental", md.get("experimental", "False")),
        tag("deprecated", md.get("deprecated", "False")),
        tag("tracker", md.get("tracker", "")),
        tag("repository", md.get("repository", "")),
        tag("tags", md.get("tags", "")),
        "  </pyqgis_plugin>",
    ]
    return "\n".join(lines)


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries, rows, problems = [], [], []

    for folder in sorted(p for p in SRC.iterdir() if (p / "metadata.txt").exists()):
        md = read_metadata(folder, problems)
        if md is None:
            continue
        zip_name = f"{folder.name}.{md['version']}.zip"
        make_zip(folder, OUT / zip_name)
        entries.append(xml_entry(folder, md, zip_name, today))
        rows.append(f"<li><b>{escape(md['name'])}</b> {escape(md['version'])} - "
                    f"<a href='{zip_name}'>download zip</a></li>")
        print(f"Built {zip_name}")

    if problems:
        print("\nFix these, then commit and push again:")
        for msg in problems:
            print(f"  - {msg}")
        sys.exit(1)

    if not entries:
        sys.exit("No plugins found. Each plugin needs its own folder in ./plugins with a metadata.txt")

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<plugins>\n' + "\n".join(entries) + "\n</plugins>\n"
    (OUT / "plugins.xml").write_text(xml, encoding="utf-8")

    (OUT / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>QGIS Plugin Repository</title>"
        "<h1>QGIS Plugin Repository</h1>"
        f"<p>Add this URL in QGIS (Plugins &gt; Manage and Install Plugins &gt; Settings &gt; Add):</p>"
        f"<pre>{base_url}/plugins.xml</pre><ul>{''.join(rows)}</ul>",
        encoding="utf-8",
    )
    print(f"Done: {len(entries)} plugin(s). Repository URL: {base_url}/plugins.xml")


if __name__ == "__main__":
    main()
