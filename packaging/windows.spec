# G/Y: Windows packaging, user decision 2026-10-04, outside TB.
import os
from pathlib import Path

root = Path(SPECPATH).parent
native_root = Path(os.environ.get("DATASAMORDNING_BUILD_NATIVE_ROOT", str(root)))
reader_dir = native_root / "rust_dwg_extractor" / "target" / "release"
pdf_dir = native_root / "dwg_pdf_backend" / "target" / "release"

required = [
    reader_dir / "rust_dwg_extractor.exe",
    reader_dir / "libredwg-0.dll",
    pdf_dir / "dwg_pdf_backend.exe",
]
missing = [str(path) for path in required if not path.is_file()]
if missing:
    raise SystemExit("Missing native build prerequisites:\n" + "\n".join(missing))

binaries = [
    (str(required[0]), "rust_dwg_extractor/target/release"),
    (str(required[2]), "dwg_pdf_backend/target/release"),
]
for directory, destination in [
    (reader_dir, "rust_dwg_extractor/target/release"),
    (pdf_dir, "dwg_pdf_backend/target/release"),
]:
    binaries.extend((str(path), destination) for path in directory.glob("*.dll"))

a = Analysis(
    [str(root / "packaging" / "windows_entry.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=[(str(root / "app" / "ui" / "theme_assets"), "app/ui/theme_assets")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DatasamordningsAssistent",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DatasamordningsAssistent",
)
