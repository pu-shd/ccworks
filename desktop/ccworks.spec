from importlib.metadata import version
import os
from pathlib import Path
import runpy
import sys

from PyInstaller.utils.hooks import copy_metadata

source = Path(SPECPATH)
macos = sys.platform == "darwin"
identity = os.environ.get("CCWORKS_MAC_SIGN_IDENTITY")
scripts = [str(source / "cli_entry.py")]
if not macos:
    scripts.append(str(source / "gui_entry.py"))  # Windows setup uses Tk.
update_binaries, update_data = [], []
if not macos:
    stage = runpy.run_path(str(source / "windows-update-data.py"))
    update_binaries, update_data = stage["prepare"](source.parent / "build" / "winsparkle")
analysis = Analysis(
    scripts, binaries=update_binaries, datas=copy_metadata("ccworks") + update_data
)
archive = PYZ(analysis.pure)
startup = analysis.scripts[:-len(scripts)]

cli = EXE(
    archive, startup + [analysis.scripts[-len(scripts)]], [],
    exclude_binaries=True, name="ccworks", console=True, upx=False,
    codesign_identity=identity,
)
executables = [cli]
if not macos:
    gui = EXE(
        archive, startup + [analysis.scripts[-1]], [],
        exclude_binaries=True, name="ccworks-desktop", console=False, upx=False,
    )
    executables.append(gui)
distribution = COLLECT(
    *executables, analysis.binaries, analysis.datas, name="ccworks-desktop", upx=False,
)
if macos:
    app = BUNDLE(
        distribution,
        name="ccworks Desktop.app",
        bundle_identifier="io.github.pu-shd.ccworks.desktop",
        icon=str(source.parent / "build" / "ccworks.icns"),
        version=version("ccworks"),
    )
