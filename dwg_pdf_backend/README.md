# DWG to PDF backend

The Rust CLI runs one AutoCAD Core Console process per selected DWG, with a
hard limit of three concurrent plotting processes. It creates a single
multi-page PDF per DWG containing every non-Model layout, using each layout's
saved plot settings. All backend staging files are created under the operating
system temporary directory, not beside the user's drawings. After validation,
only the final PDF is copied to the drawing's folder. The source DWG is not
written by the backend.

Before publishing the PDF, the backend checks that the generated file has a
PDF header and end marker. It does not extract PDF text or compare contents
with DWG metadata. A PDF is reported as created after successful publication;
the source DWG is never modified.

Build and test from the repository root:

```powershell
cargo test --manifest-path dwg_pdf_backend\Cargo.toml
cargo build --release --manifest-path dwg_pdf_backend\Cargo.toml
```

The repository Cargo configuration selects the `windows_legacy` entropy
backend for the Windows GNU target. This avoids generating a `bcryptprimitives`
import library with `dlltool`; the selected backend uses Windows'
`RtlGenRandom` API.

AutoCAD is located under `%ProgramFiles%\Autodesk`. To select a different
installation, set `DATASAMORDNING_ACCORECONSOLE` to its `accoreconsole.exe`
path. The Python UI calls the optimized executable at
`dwg_pdf_backend\target\release\dwg_pdf_backend.exe`.

The UI stores successful creation signatures in the hidden project-root file
`.datasamordning_pdf_status.json`. A status becomes stale when either file's
size or modification time changes; "synced" means a PDF was successfully
created for the recorded DWG signature, not that its content was compared
with DWG metadata. Plotting is always explicit and does not run automatically
after a DWG edit.
