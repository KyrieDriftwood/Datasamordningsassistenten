use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime},
};

pub const MAX_PLOT_WORKERS: usize = 3;
pub const DEFAULT_TIMEOUT_SECONDS: u64 = 300;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlotJob {
    pub drawing: PathBuf,
    pub output: PathBuf,
    pub expected_values: Vec<(String, String)>,
    pub verification_issue: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PlotResult {
    pub drawing: PathBuf,
    pub output: PathBuf,
    pub error: Option<String>,
    pub warning: Option<String>,
}

struct TemporaryDirectory(PathBuf);

impl TemporaryDirectory {
    fn create(parent: &Path) -> Result<Self, String> {
        static NEXT_ID: AtomicUsize = AtomicUsize::new(0);
        let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let path = parent.join(format!(
            "datasamordning-dwg-plot-{}-{id}",
            std::process::id()
        ));
        fs::create_dir(&path)
            .map_err(|error| format!("Kunde inte skapa tillfällig plotmapp: {error}"))?;
        Ok(Self(path))
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.0) {
            eprintln!(
                "VARNING: kunde inte städa tillfällig plotmapp {}: {error}",
                self.0.display()
            );
        }
    }
}

fn windows_short_path(path: &Path) -> Result<String, String> {
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let path_for_autocad = if path.exists() {
            path.to_path_buf()
        } else {
            let parent = path.parent().ok_or_else(|| {
                format!("DWG-sökvägen saknar överordnad mapp: {}", path.display())
            })?;
            let parent_short = windows_short_path(parent)?;
            return Ok(format!(
                "{}/{}",
                parent_short.trim_end_matches('/'),
                path.file_name()
                    .and_then(|name| name.to_str())
                    .ok_or_else(|| format!("Ogiltigt filnamn: {}", path.display()))?
            ));
        };
        let mut wide: Vec<u16> = path_for_autocad.as_os_str().encode_wide().collect();
        wide.push(0);
        let mut short = vec![0u16; 32768];
        let length =
            unsafe { GetShortPathNameW(wide.as_ptr(), short.as_mut_ptr(), short.len() as u32) };
        let path_string = if length == 0 || length as usize >= short.len() {
            path_for_autocad.to_string_lossy().into_owned()
        } else {
            short.truncate(length as usize);
            String::from_utf16_lossy(&short)
        };
        return Ok(path_string.replace('\\', "/"));
    }
    #[cfg(not(windows))]
    {
        Ok(path.to_string_lossy().replace('\\', "/"))
    }
}

#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetShortPathNameW(long_path: *const u16, short_path: *mut u16, buffer_len: u32) -> u32;
}

fn lisp_string(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len() + 2);
    encoded.push('"');
    for character in value.chars() {
        match character {
            '\\' => encoded.push_str("\\\\"),
            '"' => encoded.push_str("\\\""),
            character if character.is_ascii() => encoded.push(character),
            character => {
                let mut units = [0u16; 2];
                for unit in character.encode_utf16(&mut units).iter() {
                    encoded.push_str(&format!("\\U+{unit:04X}"));
                }
            }
        }
    }
    encoded.push('"');
    encoded
}

fn find_accoreconsole() -> Result<PathBuf, String> {
    if let Some(override_path) = std::env::var_os("DATASAMORDNING_ACCORECONSOLE") {
        let path = PathBuf::from(override_path);
        if path.is_file() {
            return Ok(path);
        }
        return Err(format!(
            "DATASAMORDNING_ACCORECONSOLE pekar på en fil som inte finns: {}",
            path.display()
        ));
    }

    let program_files = std::env::var_os("ProgramFiles")
        .map(PathBuf::from)
        .ok_or_else(|| "Windows-miljön saknar ProgramFiles.".to_string())?;
    let autodesk_root = program_files.join("Autodesk");
    let entries = fs::read_dir(&autodesk_root).map_err(|error| {
        format!(
            "Kunde inte söka efter AutoCAD under {}: {error}",
            autodesk_root.display()
        )
    })?;
    let mut candidates = Vec::new();
    for entry in entries {
        let entry =
            entry.map_err(|error| format!("Kunde inte läsa Autodesk-installation: {error}"))?;
        let product = entry.path();
        let product_name = product
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if product_name.contains("trueview") {
            continue;
        }
        let executable = product.join("accoreconsole.exe");
        if executable.is_file() {
            candidates.push((product_name, executable));
        }
    }
    candidates.sort_by(|a, b| b.0.cmp(&a.0));
    candidates
        .into_iter()
        .next()
        .map(|(_, path)| path)
        .ok_or_else(|| {
            format!(
                "Hittade ingen AutoCAD Core Console under {}.",
                autodesk_root.display()
            )
        })
}

fn validate_pdf(path: &Path) -> Result<(), String> {
    let bytes = fs::read(path)
        .map_err(|error| format!("Kunde inte läsa den skapade PDF-filen: {error}"))?;
    if !bytes.starts_with(b"%PDF-") || !bytes.windows(5).any(|window| window == b"%%EOF") {
        return Err("AutoCAD skapade inte en komplett PDF-fil.".into());
    }
    Ok(())
}

fn normalized_text(value: &str) -> String {
    value
        .chars()
        .filter(|character| !character.is_whitespace())
        .flat_map(char::to_lowercase)
        .collect()
}

fn metadata_mismatch_warning(
    path: &Path,
    expected_values: &[(String, String)],
    verification_issue: Option<&str>,
) -> Option<String> {
    let mut warnings = Vec::new();
    if let Some(issue) = verification_issue.filter(|issue| !issue.trim().is_empty()) {
        warnings.push(issue.to_string());
    }
    if expected_values.is_empty() && warnings.is_empty() {
        warnings.push("DWG-filen saknar ifyllda metadata att jämföra med PDF-texten.".into());
    }
    if !expected_values.is_empty() {
        match pdf_extract::extract_text(path) {
            Ok(extracted) => {
                let normalized_pdf = normalized_text(&extracted);
                let missing: Vec<String> = expected_values
                    .iter()
                    .filter(|(_, value)| {
                        !value.trim().is_empty()
                            && !normalized_pdf.contains(&normalized_text(value))
                    })
                    .map(|(label, value)| format!("{label}: {value}"))
                    .collect();
                if !missing.is_empty() {
                    warnings.push(format!(
                        "Värden hittades inte i PDF-texten: {}",
                        missing.join("; ")
                    ));
                }
            }
            Err(error) => warnings.push(format!(
                "PDF-texten kunde inte läsas för metadatajämförelse: {error}"
            )),
        }
    }
    (!warnings.is_empty()).then(|| warnings.join(" "))
}

fn drawing_signature(path: &Path) -> Result<(u64, SystemTime), String> {
    let metadata = fs::metadata(path)
        .map_err(|error| format!("Kunde inte läsa DWG-filen {}: {error}", path.display()))?;
    let modified = metadata
        .modified()
        .map_err(|error| format!("Kunde inte kontrollera ändringstid för DWG-filen: {error}"))?;
    Ok((metadata.len(), modified))
}

fn autocad_log_tail(path: &Path) -> String {
    let Ok(bytes) = fs::read(path) else {
        return String::new();
    };
    let text = if bytes.starts_with(&[0xff, 0xfe]) {
        let units = bytes[2..]
            .chunks_exact(2)
            .map(|chunk| u16::from_le_bytes([chunk[0], chunk[1]]))
            .collect::<Vec<_>>();
        String::from_utf16_lossy(&units)
    } else if bytes.len() % 2 == 0
        && bytes
            .iter()
            .skip(1)
            .step_by(2)
            .filter(|byte| **byte == 0)
            .count()
            > bytes.len() / 8
    {
        let units = bytes
            .chunks_exact(2)
            .map(|chunk| u16::from_le_bytes([chunk[0], chunk[1]]))
            .collect::<Vec<_>>();
        String::from_utf16_lossy(&units)
    } else {
        String::from_utf8_lossy(&bytes).into_owned()
    };
    text.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .rev()
        .take(12)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join(" | ")
}

fn collect_plot_style_matches(root: &Path, matches: &mut Vec<PathBuf>) {
    if !root.is_dir() {
        return;
    }
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.filter_map(Result::ok) {
        let path = entry.path();
        if path.is_dir() {
            collect_plot_style_matches(&path, matches);
        } else if path
            .file_name()
            .and_then(|name| name.to_str())
            .map(|name| {
                name.eq_ignore_ascii_case("monochrome.ctb")
                    || name.eq_ignore_ascii_case("monochrome.stb")
            })
            .unwrap_or(false)
        {
            matches.push(path);
        }
    }
}

fn resolve_monochrome_plot_style() -> String {
    if let Some(env_path) = std::env::var_os("DATASAMORDNING_PLOT_STYLE") {
        let path = PathBuf::from(env_path);
        if path.is_file() {
            return path.to_string_lossy().replace('\\', "/");
        }
        eprintln!(
            "VARNING: DATASAMORDNING_PLOT_STYLE pekar på en ogiltig fil: {}",
            path.display()
        );
    }

    let mut matches = Vec::new();
    for root in [
        PathBuf::from(r"C:\Users\Public\Documents\Autodesk"),
        PathBuf::from(r"C:\Program Files\Autodesk"),
    ] {
        collect_plot_style_matches(&root, &mut matches);
    }
    if let Some(path) = matches
        .into_iter()
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .map(|name| {
                    name.eq_ignore_ascii_case("monochrome.ctb")
                        || name.eq_ignore_ascii_case("monochrome.stb")
                })
                .unwrap_or(false)
        })
        .min()
    {
        return path.to_string_lossy().replace('\\', "/");
    }

    "monochrome.ctb".to_string()
}

fn build_publish_lisp(
    drawing: &Path,
    output_pdf: &Path,
    dsd_file: &Path,
    result_file: &Path,
) -> Result<String, String> {
    let drawing = windows_short_path(drawing)?;
    let output = windows_short_path(output_pdf)?;
    let output_dir = windows_short_path(output_pdf.parent().unwrap_or(output_pdf))?;
    let plot_style_sheet = resolve_monochrome_plot_style();
    let plot_style_name = Path::new(&plot_style_sheet)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("monochrome.ctb");
    let plot_style_name_lisp = lisp_string(plot_style_name);
    let dsd = lisp_string(&windows_short_path(dsd_file)?);
    let result = lisp_string(&windows_short_path(result_file)?);
    let lines = vec![
        "(vl-load-com)".to_string(),
        format!("(setq plotResultFile (open {result} \"w\"))"),
        "(setq plotError nil)".to_string(),
        "(setq layoutDictionary (dictsearch (namedobjdict) \"ACAD_LAYOUT\"))".to_string(),
        "(setq dictionaryHandle (cdr (assoc -1 layoutDictionary)))".to_string(),
        format!("(setq dsdFile (open {dsd} \"w\"))"),
        "(if (not dsdFile)".to_string(),
        "  (setq plotError \"Kunde inte skapa DSD-filen\")".to_string(),
        "  (progn".to_string(),
        "    (write-line \"[DWF6Version]\" dsdFile)".to_string(),
        "    (write-line \"Ver=1\" dsdFile)".to_string(),
        "    (write-line \"[DWF6MinorVersion]\" dsdFile)".to_string(),
        "    (write-line \"MinorVer=1\" dsdFile)".to_string(),
        "    (setq sheetCount 0)".to_string(),
        "    (setq item (dictnext dictionaryHandle T))".to_string(),
        "    (while item".to_string(),
        "      (setq layoutData (entget (cdr (assoc -1 item))))".to_string(),
        "      (setq sheetName (cdr (assoc 1 (member '(100 . \"AcDbLayout\") layoutData))))"
            .to_string(),
        "      (if (/= sheetName \"Model\")".to_string(),
        "        (progn".to_string(),
        "          (setq layoutObject (vlax-ename->vla-object (cdr (assoc -1 item))))"
            .to_string(),
        format!(
            "          (setq styleResult (vl-catch-all-apply 'vla-put-StyleSheet (list layoutObject {plot_style_name_lisp})))"
        ),
        "          (if (vl-catch-all-error-p styleResult)".to_string(),
        "            (setq plotError (strcat \"Kunde inte applicera plotstilen på layout \" sheetName \": \" (vl-catch-all-error-message styleResult)))"
            .to_string(),
        "            (progn".to_string(),
        "              (vla-put-PlotWithPlotStyles layoutObject :vlax-true)".to_string(),
        format!(
            "              (if (/= (strcase (vla-get-StyleSheet layoutObject)) (strcase {plot_style_name_lisp}))"
        ),
        "                (setq plotError (strcat \"AutoCAD behöll inte plotstilen på layout \" sheetName))"
            .to_string(),
        "              )".to_string(),
        "              (if (= (vla-get-PlotWithPlotStyles layoutObject) :vlax-false)"
            .to_string(),
        "                (setq plotError (strcat \"AutoCAD aktiverade inte plotstilar på layout \" sheetName))"
            .to_string(),
        "              )".to_string(),
        "            )".to_string(),
        "          )".to_string(),
        "          (write-line (strcat \"[DWF6Sheet:Plot-\" sheetName \"]\") dsdFile)".to_string(),
        format!("          (write-line (strcat \"DWG=\\\"{drawing}\\\"\") dsdFile)"),
        "          (write-line (strcat \"Layout=\" sheetName) dsdFile)".to_string(),
        "          (write-line \"Setup=\" dsdFile)".to_string(),
        "          (write-line \"PlotWithPlotStyles=TRUE\" dsdFile)".to_string(),
        format!("          (write-line \"PlotStyleSheet={plot_style_sheet}\" dsdFile)"),
        format!("          (write-line \"PlotStyleName={plot_style_name}\" dsdFile)"),
        format!("          (write-line (strcat \"OriginalSheetPath=\\\"{drawing}\\\"\") dsdFile)"),
        "          (write-line \"Has Plot Port=0\" dsdFile)".to_string(),
        "          (write-line \"Has3DDWF=0\" dsdFile)".to_string(),
        "          (setq sheetCount (1+ sheetCount))))".to_string(),
        "      (setq item (dictnext dictionaryHandle))".to_string(),
        "    )".to_string(),
        "    (write-line \"[Target]\" dsdFile)".to_string(),
        "    (write-line \"Type=6\" dsdFile)".to_string(),
        format!("    (write-line (strcat \"DWF={output}\") dsdFile)"),
        format!("    (write-line (strcat \"OUT={output_dir}\") dsdFile)"),
        "    (write-line \"PWD=\" dsdFile)".to_string(),
        "    (write-line \"[MRU Sheet List]\" dsdFile)".to_string(),
        "    (write-line \"MRU=2\" dsdFile)".to_string(),
        "    (write-line \"[PdfOptions]\" dsdFile)".to_string(),
        "    (write-line \"IncludeHyperlinks=TRUE\" dsdFile)".to_string(),
        "    (write-line \"CreateBookmarks=TRUE\" dsdFile)".to_string(),
        "    (write-line \"CaptureFontsInDrawing=TRUE\" dsdFile)".to_string(),
        "    (write-line \"ConvertTextToGeometry=FALSE\" dsdFile)".to_string(),
        "    (write-line \"VectorResolution=1200\" dsdFile)".to_string(),
        "    (write-line \"RasterResolution=400\" dsdFile)".to_string(),
        "    (close dsdFile)".to_string(),
        "    (if (= sheetCount 0)".to_string(),
        "      (setq plotError \"Ritningen saknar utskrivbara layouter\")".to_string(),
        "    )".to_string(),
        "  )".to_string(),
        ")".to_string(),
        "(if plotError".to_string(),
        "  (write-line (strcat \"ERROR:\" plotError) plotResultFile)".to_string(),
        "  (write-line \"OK\" plotResultFile)".to_string(),
        ")".to_string(),
        "(close plotResultFile)".to_string(),
        format!("(if (not plotError) (vl-catch-all-apply 'vl-cmdf (list \"_.-PUBLISH\" {dsd})))"),
    ];
    Ok(lines.join("\n") + "\n")
}

fn plot_one(job: &PlotJob, executable: &Path, timeout: Duration) -> PlotResult {
    let result = plot_one_inner(job, executable, timeout);
    match result {
        Ok(warning) => PlotResult {
            drawing: job.drawing.clone(),
            output: job.output.clone(),
            error: None,
            warning,
        },
        Err(error) => PlotResult {
            drawing: job.drawing.clone(),
            output: job.output.clone(),
            error: Some(error),
            warning: None,
        },
    }
}

fn plot_one_inner(
    job: &PlotJob,
    executable: &Path,
    timeout: Duration,
) -> Result<Option<String>, String> {
    if !job.drawing.is_file() {
        return Err(format!("DWG-filen finns inte: {}", job.drawing.display()));
    }
    let source_signature = drawing_signature(&job.drawing)?;
    let output_parent = job
        .output
        .parent()
        .ok_or_else(|| "PDF-sökvägen saknar målmapp.".to_string())?;
    fs::create_dir_all(output_parent)
        .map_err(|error| format!("Kunde inte skapa PDF-mappen: {error}"))?;

    let temporary = TemporaryDirectory::create(&std::env::temp_dir())?;
    let temporary_pdf = temporary.0.join("plot.pdf");
    let result_file = temporary.0.join("result.txt");
    let autocad_stdout = temporary.0.join("accoreconsole.stdout.log");
    let autocad_stderr = temporary.0.join("accoreconsole.stderr.log");
    let dsd_file = temporary.0.join("plot.dsd");
    let lisp_path = temporary.0.join("plot.lsp");
    let script_path = temporary.0.join("plot.scr");
    fs::write(
        &lisp_path,
        build_publish_lisp(&job.drawing, &temporary_pdf, &dsd_file, &result_file)?,
    )
    .map_err(|error| format!("Kunde inte skriva AutoLISP-plottskript: {error}"))?;
    let load_path = windows_short_path(&lisp_path)?;
    fs::write(
        &script_path,
        format!("(load {})\nQUIT\nN\n", lisp_string(&load_path)),
    )
    .map_err(|error| format!("Kunde inte skriva AutoCAD-skript: {error}"))?;

    let stdout_file = fs::File::create(&autocad_stdout)
        .map_err(|error| format!("Kunde inte skapa AutoCAD-logg: {error}"))?;
    let stderr_file = fs::File::create(&autocad_stderr)
        .map_err(|error| format!("Kunde inte skapa AutoCAD-fellogg: {error}"))?;
    let mut child = Command::new(executable)
        .arg("/i")
        .arg(&job.drawing)
        .arg("/s")
        .arg(&script_path)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .spawn()
        .map_err(|error| format!("Kunde inte starta AutoCAD Core Console: {error}"))?;
    let started = Instant::now();
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("Kunde inte kontrollera AutoCAD-processen: {error}"))?
        {
            if !status.success() {
                let diagnostics = autocad_log_tail(&autocad_stdout);
                return Err(format!(
                    "AutoCAD Core Console avslutades med status {status}. {diagnostics}"
                ));
            }
            break;
        }
        if started.elapsed() >= timeout {
            child.kill().map_err(|error| {
                format!("Plotten överskred tidsgränsen och kunde inte stoppas: {error}")
            })?;
            let _ = child.wait();
            return Err(format!(
                "AutoCAD Core Console överskred tidsgränsen på {} sekunder.",
                timeout.as_secs()
            ));
        }
        thread::sleep(Duration::from_millis(100));
    }

    let lisp_result = fs::read_to_string(&result_file).map_err(|error| {
        format!(
            "AutoCAD skapade inget plotresultat: {error}. {}",
            autocad_log_tail(&autocad_stdout)
        )
    })?;
    if let Some(detail) = lisp_result.trim().strip_prefix("ERROR:") {
        return Err(format!(
            "AutoCAD kunde inte plotta DWG-filen: {detail}. {}",
            autocad_log_tail(&autocad_stdout)
        ));
    }
    if lisp_result.trim() != "OK" {
        return Err(format!(
            "Oväntat plotresultat från AutoCAD: {}. {}",
            lisp_result.trim(),
            autocad_log_tail(&autocad_stdout)
        ));
    }

    validate_pdf(&temporary_pdf).map_err(|error| {
        format!(
            "{error} AutoCAD-logg: {}",
            autocad_log_tail(&autocad_stdout)
        )
    })?;
    if drawing_signature(&job.drawing)? != source_signature {
        return Err("DWG-filen ändrades under PDF-plotten; PDF-filen avvisades.".into());
    }
    let warning = metadata_mismatch_warning(
        &temporary_pdf,
        &job.expected_values,
        job.verification_issue.as_deref(),
    );
    replace_output(&temporary_pdf, &job.output)?;
    Ok(warning)
}

fn replace_output(source: &Path, destination: &Path) -> Result<(), String> {
    let backup = source
        .parent()
        .ok_or_else(|| "Den tillfälliga PDF-sökvägen saknar mapp.".to_string())?
        .join("previous.pdf");
    let had_previous = destination.exists();
    if had_previous {
        fs::copy(destination, &backup)
            .map_err(|error| format!("Kunde inte skydda befintlig PDF före byte: {error}"))?;
    }
    if let Err(error) = fs::copy(source, destination) {
        if had_previous {
            if let Err(restore_error) = fs::copy(&backup, destination) {
                return Err(format!(
                    "Kunde inte ersätta PDF ({error}) eller återställa tidigare PDF ({restore_error})."
                ));
            }
        } else if let Err(cleanup_error) = fs::remove_file(destination) {
            if cleanup_error.kind() != std::io::ErrorKind::NotFound {
                return Err(format!(
                    "Kunde inte publicera PDF ({error}) eller ta bort ofullständig målfil ({cleanup_error})."
                ));
            }
        }
        return Err(format!("Kunde inte publicera skapad PDF: {error}"));
    }
    Ok(())
}

pub fn decode_hex(value: &str) -> Result<String, String> {
    if value.len() % 2 != 0 {
        return Err("Manifestet innehåller en ofullständig hexsträng.".into());
    }
    let bytes = (0..value.len())
        .step_by(2)
        .map(|index| u8::from_str_radix(&value[index..index + 2], 16))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| "Manifestet innehåller ogiltig hexkodning.".to_string())?;
    String::from_utf8(bytes)
        .map_err(|error| format!("Manifestet innehåller ogiltig UTF-8: {error}"))
}

pub fn read_manifest(path: &Path) -> Result<Vec<PlotJob>, String> {
    let file = fs::File::open(path).map_err(|error| {
        format!(
            "Kunde inte öppna plotmanifestet {}: {error}",
            path.display()
        )
    })?;
    let mut jobs = Vec::new();
    for (line_number, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|error| format!("Kunde inte läsa manifestet: {error}"))?;
        if line.is_empty() {
            continue;
        }
        let parts: Vec<_> = line.split('\t').collect();
        if parts.len() < 3 {
            return Err(format!("Ogiltig manifestrad {}.", line_number + 1));
        }
        let drawing = PathBuf::from(decode_hex(parts[0])?);
        let output = PathBuf::from(decode_hex(parts[1])?);
        let verification_issue = if parts[2].is_empty() {
            None
        } else {
            Some(decode_hex(parts[2])?)
        };
        let expected_values = parts[3..]
            .iter()
            .map(|field| {
                let (label, value) = field.split_once(':').ok_or_else(|| {
                    "Manifestet innehåller ett ogiltigt metadatafält.".to_string()
                })?;
                Ok((decode_hex(label)?, decode_hex(value)?))
            })
            .collect::<Result<Vec<_>, String>>()?;
        jobs.push(PlotJob {
            drawing,
            output,
            expected_values,
            verification_issue,
        });
    }
    Ok(jobs)
}

fn execute_jobs<F, G>(
    jobs: &[PlotJob],
    requested_workers: usize,
    execute: F,
    on_result: G,
) -> Vec<PlotResult>
where
    F: Fn(&PlotJob) -> PlotResult + Sync,
    G: Fn(usize, &PlotResult) + Sync,
{
    let next_job = AtomicUsize::new(0);
    let results: Mutex<Vec<Option<PlotResult>>> = Mutex::new(vec![None; jobs.len()]);
    let worker_count = jobs
        .len()
        .min(requested_workers.max(1))
        .min(MAX_PLOT_WORKERS);

    thread::scope(|scope| {
        for _ in 0..worker_count {
            scope.spawn(|| loop {
                let index = next_job.fetch_add(1, Ordering::Relaxed);
                let Some(job) = jobs.get(index) else {
                    break;
                };
                let result = execute(job);
                results.lock().expect("plotresultatlås poisoned")[index] = Some(result.clone());
                on_result(index, &result);
            });
        }
    });
    results
        .into_inner()
        .expect("plotresultatlås poisoned")
        .into_iter()
        .flatten()
        .collect()
}

pub fn plot_manifest(path: &Path, timeout_seconds: u64) -> Result<(), String> {
    let jobs = read_manifest(path)?;
    if jobs.is_empty() {
        return Err("Plotmanifestet innehåller inga DWG-jobb.".into());
    }
    let executable = find_accoreconsole()?;
    let output_lock = Mutex::new(());
    let timeout = Duration::from_secs(timeout_seconds.max(1));
    let results = execute_jobs(
        &jobs,
        MAX_PLOT_WORKERS,
        |job| plot_one(job, &executable, timeout),
        |index, result| {
            let (status, message) = match (&result.error, &result.warning) {
                (Some(error), _) => ("ERROR", hex_encode(error.as_bytes())),
                (None, Some(warning)) => ("WARN", hex_encode(warning.as_bytes())),
                (None, None) => ("OK", String::new()),
            };
            let _guard = output_lock.lock().expect("utdatalås poisoned");
            println!("###RESULT\t{index}\t{status}\t{message}");
            let _ = std::io::stdout().flush();
        },
    );
    if results.len() != jobs.len() {
        return Err("Rust-backenden slutförde inte alla DWG-plotjobb.".into());
    }
    Ok(())
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut result = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        result.push(HEX[(byte >> 4) as usize] as char);
        result.push(HEX[(byte & 0x0f) as usize] as char);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_unicode_hex_values() {
        let encoded = hex_encode("Exempelö".as_bytes());
        assert_eq!(decode_hex(&encoded).unwrap(), "Exempelö");
    }

    #[test]
    fn rejects_invalid_hex_values() {
        assert!(decode_hex("abc").is_err());
        assert!(decode_hex("gg").is_err());
    }

    #[test]
    fn lisp_strings_escape_unicode_paths() {
        assert_eq!(
            lisp_string("C:/Projekt/Exempelö"),
            "\"C:/Projekt/Exempel\\U+00F6\""
        );
    }

    fn minimal_text_pdf(text: &str) -> Vec<u8> {
        let content = format!("BT /F1 12 Tf 72 720 Td ({text}) Tj ET");
        let objects = [
            "<< /Type /Catalog /Pages 2 0 R >>".to_string(),
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_string(),
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>".to_string(),
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_string(),
            format!(
                "<< /Length {} >>\nstream\n{}\nendstream",
                content.len(),
                content
            ),
        ];
        let mut pdf = b"%PDF-1.4\n".to_vec();
        let mut offsets = vec![0usize];
        for (index, object) in objects.iter().enumerate() {
            offsets.push(pdf.len());
            pdf.extend_from_slice(format!("{} 0 obj\n{}\nendobj\n", index + 1, object).as_bytes());
        }
        let xref_start = pdf.len();
        pdf.extend_from_slice(b"xref\n0 6\n0000000000 65535 f \n");
        for offset in offsets.iter().skip(1) {
            pdf.extend_from_slice(format!("{offset:010} 00000 n \n").as_bytes());
        }
        pdf.extend_from_slice(
            format!("trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n")
                .as_bytes(),
        );
        pdf
    }

    #[test]
    fn accepts_a_created_pdf_with_valid_structure() {
        let path = std::env::temp_dir().join(format!(
            "dwg-pdf-valid-{}-{}.pdf",
            std::process::id(),
            AtomicUsize::new(0).fetch_add(1, Ordering::Relaxed)
        ));
        fs::write(&path, minimal_text_pdf("Nykoping 56+110")).unwrap();

        assert!(validate_pdf(&path).is_ok());

        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn metadata_mismatch_is_a_warning_not_a_plot_failure() {
        let path =
            std::env::temp_dir().join(format!("dwg-pdf-mismatch-{}.pdf", std::process::id()));
        fs::write(&path, minimal_text_pdf("Nykoping 56+110")).unwrap();
        let expected = vec![
            ("Plats".to_string(), "Nykoping".to_string()),
            ("Station".to_string(), "56+111".to_string()),
        ];

        let warning = metadata_mismatch_warning(&path, &expected, None).unwrap();

        assert!(warning.contains("Station: 56+111"));
        assert!(!warning.contains("Plats: Nykoping"));
        fs::remove_file(&path).unwrap();
    }

    #[test]
    fn missing_metadata_produces_a_verification_warning() {
        let warning = metadata_mismatch_warning(Path::new("unused.pdf"), &[], None).unwrap();

        assert!(warning.contains("saknar ifyllda metadata"));
    }

    #[test]
    fn rejects_empty_and_malformed_pdf_files() {
        let path = std::env::temp_dir().join(format!("dwg-pdf-invalid-{}.pdf", std::process::id()));
        fs::write(&path, b"not a pdf").unwrap();

        assert!(validate_pdf(&path).is_err());
        fs::remove_file(&path).unwrap();
        assert!(validate_pdf(&path).is_err());
    }

    #[test]
    fn publish_script_balances_lisp_forms_and_includes_all_layouts() {
        let directory = std::env::temp_dir().join(format!("dwg-pdf-lisp-{}", std::process::id()));
        fs::create_dir_all(&directory).unwrap();
        let drawing = directory.join("source.dwg");
        fs::write(&drawing, b"temporary").unwrap();
        let output = directory.join("plot.pdf");
        let dsd = directory.join("plot.dsd");
        let result = directory.join("result.txt");
        let script = build_publish_lisp(&drawing, &output, &dsd, &result).unwrap();
        fs::remove_dir_all(&directory).unwrap();

        let mut depth = 0isize;
        let mut in_string = false;
        let mut escaped = false;
        for character in script.chars() {
            if in_string {
                if escaped {
                    escaped = false;
                } else if character == '\\' {
                    escaped = true;
                } else if character == '"' {
                    in_string = false;
                }
            } else if character == '"' {
                in_string = true;
            } else if character == '(' {
                depth += 1;
            } else if character == ')' {
                depth -= 1;
                assert!(depth >= 0);
            }
        }
        assert!(!in_string);
        assert_eq!(depth, 0);
        assert!(script.contains("(dictnext dictionaryHandle"));
        assert!(script.contains("(member '(100 . \"AcDbLayout\") layoutData)"));
        assert!(script.contains("_.-PUBLISH"));
        assert!(script.contains("[DWF6Sheet:"));
        assert!(script.contains("PlotWithPlotStyles=TRUE"));
        assert!(script.contains("PlotStyleSheet="));
        assert!(script.contains("PlotStyleName=monochrome.ctb") || script.contains("PlotStyleName=monochrome.stb"));
        assert!(script.contains("vla-put-StyleSheet"));
        assert!(script.contains("vla-put-PlotWithPlotStyles"));
        assert!(script.contains("vla-get-StyleSheet"));
        assert!(script.contains("vla-get-PlotWithPlotStyles"));
        assert!(script.contains("\"OUT="));
        assert!(!script.contains("OUT=\\\""));
        assert!(script.contains("\"DWF="));
        assert!(script.find("(close plotResultFile") < script.find("_.-PUBLISH"));
    }

    #[test]
    fn publishes_from_system_temp_without_staging_files_in_output_folder() {
        static NEXT_TEST_ID: AtomicUsize = AtomicUsize::new(0);
        let id = NEXT_TEST_ID.fetch_add(1, Ordering::Relaxed);
        let system_temp = std::env::temp_dir().join(format!("dwg-pdf-publish-test-{id}"));
        let output_folder = std::env::temp_dir().join(format!("dwg-pdf-output-test-{id}"));
        fs::create_dir_all(&system_temp).unwrap();
        fs::create_dir_all(&output_folder).unwrap();
        let source = system_temp.join("plot.pdf");
        let destination = output_folder.join("drawing.pdf");
        fs::write(&source, b"new pdf").unwrap();
        fs::write(&destination, b"previous pdf").unwrap();

        replace_output(&source, &destination).unwrap();

        assert_eq!(fs::read(&destination).unwrap(), b"new pdf");
        assert_eq!(
            fs::read_dir(&output_folder)
                .unwrap()
                .map(|entry| entry.unwrap().file_name())
                .collect::<Vec<_>>(),
            [std::ffi::OsString::from("drawing.pdf")]
        );
        fs::remove_dir_all(system_temp).unwrap();
        fs::remove_dir_all(output_folder).unwrap();
    }

    #[test]
    fn reads_manifest_paths_and_metadata_verification_values() {
        let temp = std::env::temp_dir().join(format!("dwg-pdf-manifest-{}", std::process::id()));
        let line = format!(
            "{}\t{}\t{}\t{}:{}\n",
            hex_encode(r"C:\ritning.dwg".as_bytes()),
            hex_encode(r"C:\ritning.pdf".as_bytes()),
            hex_encode(b""),
            hex_encode("Station".as_bytes()),
            hex_encode("56+110".as_bytes())
        );
        fs::write(&temp, line).unwrap();
        let jobs = read_manifest(&temp).unwrap();
        fs::remove_file(temp).unwrap();
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].drawing, PathBuf::from(r"C:\ritning.dwg"));
        assert_eq!(jobs[0].output, PathBuf::from(r"C:\ritning.pdf"));
        assert_eq!(
            jobs[0].expected_values,
            [("Station".into(), "56+110".into())]
        );
        assert_eq!(jobs[0].verification_issue, None);
    }

    #[test]
    fn plotting_worker_pool_never_exceeds_three_processes() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let active = AtomicUsize::new(0);
        let maximum = AtomicUsize::new(0);
        let jobs: Vec<_> = (0..12)
            .map(|index| PlotJob {
                drawing: PathBuf::from(format!("{index}.dwg")),
                output: PathBuf::from(format!("{index}.pdf")),
                expected_values: Vec::new(),
                verification_issue: None,
            })
            .collect();
        let results = execute_jobs(
            &jobs,
            12,
            |job| {
                let current = active.fetch_add(1, Ordering::SeqCst) + 1;
                maximum.fetch_max(current, Ordering::SeqCst);
                thread::sleep(Duration::from_millis(10));
                active.fetch_sub(1, Ordering::SeqCst);
                PlotResult {
                    drawing: job.drawing.clone(),
                    output: job.output.clone(),
                    error: None,
                    warning: None,
                }
            },
            |_, _| {},
        );
        assert_eq!(results.len(), jobs.len());
        assert!(maximum.load(Ordering::SeqCst) <= MAX_PLOT_WORKERS);
    }
}
