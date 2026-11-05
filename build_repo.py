"""
Builds a QGIS plugin repository from the plugin folders in ./plugins.

For every folder in ./plugins that contains a metadata.txt, this script:
  1. Zips the folder (the zip keeps the folder name at its top level, as QGIS expects).
  2. Adds an entry for it to plugins.xml.
Output goes to ./site, which the GitHub Action publishes to GitHub Pages.

Run locally:  python build_repo.py https://<user>.github.io/<repo>
"""
import ast
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


NOT_PLUGINS = {"guides"}       # folders in ./plugins that are not plugins


def check_layout(problems: list) -> None:
    """Folders a plugin's files can end up in by mistake (e.g. uploaded to 'cloudpro' while the plugin is
    'CloudPro'): such files never reach the zip, and the plugin crashes on a missing module."""
    names = {}
    for d in SRC.iterdir():
        if d.is_dir() and d.name not in NOT_PLUGINS and not d.name.startswith("."):
            names.setdefault(d.name.lower(), []).append(d.name)
            if not (d / "metadata.txt").exists():
                files = [str(f.relative_to(SRC)) for f in d.rglob("*") if f.is_file()][:5]
                problems.append(f"plugins/{d.name} has no metadata.txt, so it is not built (files: "
                                f"{', '.join(files) or 'none'}). Move them into the plugin's own folder.")
    for low, dirs in names.items():
        if len(dirs) > 1:
            problems.append(f"plugins/ has folders differing only in capitals: {', '.join(sorted(dirs))}. "
                            "Windows sees them as one; the repository builds only the one with metadata.txt.")


def _module_exists(mod: Path) -> bool:
    """A .py file, a package, or a compiled extension (.pyd / .so, any ABI tag)."""
    if mod.with_suffix(".py").exists() or (mod / "__init__.py").exists():
        return True
    return any(mod.parent.glob(mod.name + ".*.pyd")) or any(mod.parent.glob(mod.name + ".*.so")) or \
        mod.with_suffix(".pyd").exists() or mod.with_suffix(".so").exists()


def check_imports(folder: Path, problems: list) -> None:
    """Every relative import of the plugin (from . / from .. ) must point at a module in the folder.
    Vendored third-party code in ext/ is left alone (its optional back ends are tried and may be absent)."""
    for py in folder.rglob("*.py"):
        if any(part in SKIP_DIRS or part == "ext" for part in py.relative_to(folder).parts):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), str(py))
        except SyntaxError as e:
            problems.append(f"{py.relative_to(SRC)}: syntax error on line {e.lineno}.")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.level:
                continue
            base = py.parent
            for _ in range(node.level - 1):
                base = base.parent
            mod = base.joinpath(*node.module.split(".")) if node.module else base
            if node.module and not _module_exists(mod):
                problems.append(f"{py.relative_to(SRC)} line {node.lineno}: imports '{'.' * node.level}{node.module}', "
                                f"but {mod.relative_to(SRC)}.py is not in the plugin.")
            elif not node.module:
                for a in node.names:
                    if not (_module_exists(base / a.name) or (base / "__init__.py").exists()):
                        problems.append(f"{py.relative_to(SRC)} line {node.lineno}: imports '{a.name}', which is "
                                        "not in the plugin.")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries, rows, problems = [], [], []
    check_layout(problems)

    for folder in sorted(p for p in SRC.iterdir() if (p / "metadata.txt").exists()):
        md = read_metadata(folder, problems)
        if md is None:
            continue
        check_imports(folder, problems)
        guides = sorted(folder.glob("docs/*_User_Guide_*.pdf"))
        if guides and not any(g.name.endswith(f"_{md['version']}.pdf") for g in guides):
            problems.append(f"{folder.name}: no user guide for version {md['version']} in docs/ "
                            f"(found {', '.join(g.name for g in guides)}).")
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
