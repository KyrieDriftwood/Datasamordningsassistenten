# Datasamordningsassistenten

Windows desktop application for document and DWG metadata coordination.

- PySide6 interface with editable DOCX/XLSX metadata tables.
- DWG attributes read through a separate Rust/LibreDWG process.
- DWG writes through AutoCAD Core Console, with backup and read-back checks.
- PDF preview, Office PDF export and selectable PDF watermarks.
- **AutoCAD…** lets users choose and save another Core Console installation.

## Run from source

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m app.main
```

Microsoft Office and AutoCAD Core Console are installed separately.
Rust build prerequisites are described in the respective native directories.
Private project documents, requirements documents, historical baseline code
and native third-party binaries are deliberately not part of this repository.

## Windows package

The local packaging scripts are available; **no downloadable release has
been published yet**. See [packaging/README.md](packaging/README.md) for build
instructions and the outstanding publication checks.

PyInstaller includes Python and Qt, but not Office or AutoCAD.
The native Rust outputs and their DLLs must be built/provisioned separately.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pip install ".[test]"
.\.venv\Scripts\python.exe -m pytest tests -q
```

Tests requiring private fixtures, historical baseline modules or native builds
are explicitly skipped when those prerequisites are absent. Preview tests use
synthetic PDFs. Passing the public suite does not prove live Office/AutoCAD
compatibility or parity with the private baseline.

## Licenses

The repository's [MIT license](LICENSE) covers first-party source only.
It does not relicense dependencies or authorize distributing their binaries.
LibreDWG is GPLv3-or-later, and PySide6/Qt has separate licensing conditions.
Binary publication remains on hold pending dependency notices, corresponding
source requirements and distribution review.
