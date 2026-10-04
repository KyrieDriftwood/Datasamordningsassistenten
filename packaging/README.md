# Windows distribution (G/Y)

User decision 2026-10-04, outside TB: prepare a Windows executable and a
source-only export for `KyrieDriftwood/Datasamordningsassistenten`.

## Local build

Use 64-bit Windows and Python 3.11 or newer:

```powershell
.\.venv\Scripts\python.exe -m pip install ".[build]"
.\scripts\Build-Windows.ps1
```

The VS Code task **Build Windows application** runs the same script.
The build requires these existing, trusted native outputs:

- `rust_dwg_extractor\target\release\rust_dwg_extractor.exe` and its DLLs,
  including `libredwg-0.dll`;
- `dwg_pdf_backend\target\release\dwg_pdf_backend.exe`.

Use `-NativeRoot C:\path\to\native-builds` to read this same directory
structure from a separate native build installation. Missing prerequisites
stop the build; DWG support is never silently omitted.

PyInstaller creates a **one-directory** package, not a single-file installer.
The script runs the frozen executable's `--self-test` before making a ZIP.
The self-test imports the UI and Qt PDF, loads the theme assets and launches
both native executables without project files, checking their expected
input-validation response. It does not validate Office or AutoCAD workflows.

Output:

- `dist\DatasamordningsAssistent\DatasamordningsAssistent.exe`
- `dist\DatasamordningsAssistent-<version>-windows-x64.zip`
- `build\windows-self-test.json` (local diagnostic, not part of the release).

Extract the **whole ZIP** and start the EXE. Keep `_internal` next to it.
Python is included. Microsoft Office and AutoCAD Core Console are **not**
included and are still required for the respective automation workflows.
The ZIP is unsigned; SmartScreen may show a warning.

## Another AutoCAD installation

Use the **AutoCAD…** button in the application to select another version's
`accoreconsole.exe`, including an installation outside Program Files. The
path is validated and saved in `QSettings` under
`autocad/accoreconsole_path`. Both DWG writers and the Rust PDF subprocess
use the shared `DATASAMORDNING_ACCORECONSOLE` override.

An externally configured override takes priority on startup. **Reset default**
restores that external override, or automatic discovery if none was supplied.
Automatic Python discovery uses `%ProgramFiles%\Autodesk`, not a hardcoded
C: installation. Invalid selections are reported rather than silently ignored.
Settings cannot be changed during background DWG loading; write/plot progress
dialogs are modal. No failed write is automatically repeated with another
version. Version-specific AutoCAD command/plot compatibility still needs
testing on each supported installation.

## Source export and GitHub

```powershell
.\scripts\Export-Source.ps1 -Destination C:\path\to\new-source-export
gh auth login
gh repo view KyrieDriftwood/Datasamordningsassistenten --json visibility,defaultBranchRef
```

Export is an explicit allowlist and never changes the parent repository,
creates commits, pushes, or overwrites an existing destination. It excludes
Examples, ErrorReports, TB/ODT and generated TB documents, `.old`, virtual
environments, native binaries, third-party vendor trees and local logs.
Review the exported code for credentials and confidential content before
uploading. A private repository does not replace this review.

Clone the target repository separately and integrate the reviewed export
there, preserving its existing files and history. Do not change the
`OfflineTunnel` remote of the parent repository. No automatic synchronization
or release workflow has been enabled.

The exported source does not include proprietary test fixtures or the frozen
baseline. Tests requiring these resources cannot all run from that export
alone. Native builds also require the separately provisioned LibreDWG headers,
import library and runtime. The export is not yet a self-contained source
distribution satisfying third-party license obligations.

## Publication gates

Before any GitHub Release:

1. Authenticate and verify the target repository's visibility and contents.
2. Review all exported material for confidentiality and credentials.
3. Decide the first-party source license with the owner; none is assumed.
4. Review GPLv3-or-later obligations for LibreDWG and the linked Rust reader,
   including corresponding source, build inputs and license notices.
5. Include required notices and fulfill Qt/PySide6 and other bundled dependency
   license requirements. Do not distribute Office, AutoCAD or font files.
6. Test the extracted package on a clean Windows machine, then test actual
   document and DWG workflows on a machine with Office/AutoCAD installed.
7. Sign the release if required by the organization, then upload the ZIP and
   its SHA256 checksum to GitHub Releases.

The current package is for **local evaluation only** until these gates pass.
GitHub Actions automation should follow a reproducible native build and the
license decision, rather than publishing local DLLs without provenance.
