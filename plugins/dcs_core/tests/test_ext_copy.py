"""DCS Core 1.19.3: the bundled packages (ext/<platform><python>, zstandard) are loaded from a copy in the user's
cache folder, so Windows never locks the plugin folder and the plugin manager can upgrade DCS Core (a locked .pyd
left a half-deleted dcs_core without __init__.py: "module 'dcs_core' has no attribute 'classFactory'").

    python3 -m pytest dcs_core/tests/test_ext_copy.py      (from the plugins folder)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dcs_core import zstd  # noqa: E402


def test_copy_outside_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "cache"))
    src = tmp_path / "plugin" / "ext" / "win312"
    (src / "zstandard").mkdir(parents=True)
    (src / "zstandard" / "__init__.py").write_text("X = 1\n")
    (src / "zstandard" / "backend_c.pyd").write_bytes(b"\0" * 100)
    d1 = zstd.ext_copy(str(src))
    assert str(tmp_path / "cache") in d1 and os.path.isfile(os.path.join(d1, "zstandard", "backend_c.pyd"))
    assert zstd.ext_copy(str(src)) == d1                             # made once
    (src / "zstandard" / "backend_c.pyd").write_bytes(b"\1" * 100)   # a new version: a new copy, the old one removed
    d2 = zstd.ext_copy(str(src))
    assert d2 != d1 and not os.path.exists(d1) and os.path.isdir(d2)
    assert zstd.ext_copy(str(tmp_path / "missing")) is None
