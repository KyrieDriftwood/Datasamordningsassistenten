//! TB-verktygets CLI: validerar argument och delegerar filhantering och diff
//! till biblioteket. Håll utdata för `--diff` stabil eftersom `/TB` återger den.

use std::{
    env,
    path::{Path, PathBuf},
    process, thread,
    time::Duration,
};
use tb_sync::{file_signature, project_paths, render_diff_report, sync, TbError};

fn usage() {
    println!(
        "tb-sync [--check | --watch | --diff] [--odt PATH]\n\n\
             --check  Kontrollera utan att skriva (exit 1 om speglingen är inaktuell)\n\
             --watch  Bevaka ODT genom polling och synkronisera efter ändring\n\
             --diff   Synkronisera och skriv full diff samt genererad åtgärdsplan\n\
             --odt    Sökväg till ODT-filen"
    );
}

fn run() -> Result<i32, TbError> {
    let (mut odt, markdown, plan, requirements, state) = project_paths();
    let mut check = false;
    let mut watch = false;
    let mut show_diff = false;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--help" | "-h" => {
                usage();
                return Ok(0);
            }
            "--check" => check = true,
            "--watch" => watch = true,
            "--diff" => show_diff = true,
            "--odt" => {
                odt = PathBuf::from(
                    args.next()
                        .ok_or_else(|| TbError("--odt kräver en sökväg".into()))?,
                )
            }
            _ => {
                return Err(TbError(format!(
                    "Okänt argument: {arg}\nAnvänd --help för hjälp."
                )))
            }
        }
    }
    if (check && watch) || (show_diff && watch) {
        return Err(TbError(
            "--check och --watch kan inte kombineras; --diff och --watch kan inte kombineras."
                .into(),
        ));
    }
    if watch {
        return watch_file(&odt, &markdown, &plan, &requirements, &state);
    }
    let result = sync(&odt, &markdown, &plan, &requirements, &state, !check)?;
    describe(&result);
    if show_diff {
        println!("\n{}", render_diff_report(&result.diff));
        if let Some(content) = &result.action_plan {
            println!("\nÅTGÄRDSPLAN\n\n{content}");
        }
    }
    if check && result.changed {
        eprintln!("Speglingen är inaktuell. Kör 'tb-sync' för att uppdatera den.");
        return Ok(1);
    }
    Ok(0)
}

fn describe(result: &tb_sync::SyncResult) {
    if result.markdown_was_edited {
        println!(
            "VARNING: {} hade redigerats för hand och återställs från ODT-källan.",
            result
                .markdown_path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("Markdown-speglingen")
        );
    }
    if result.markdown_was_missing {
        println!(
            "Ingen tidigare spegling fanns - skapade {}.",
            result
                .markdown_path
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("Markdown")
        );
    }
    if result.changed {
        println!("TB har ändrats: {}", result.diff.summary());
        for change in &result.diff.changes {
            let label = match change.kind.as_str() {
                "added" => "ny",
                "removed" => "borttagen",
                "modified" => "ändrad",
                _ => "flyttad",
            };
            println!("  - {}: {label}", change.code);
        }
        if result.plan_was_written {
            println!("Åtgärdsplan skriven till docs/TB-atgardsplan.md.");
        }
    } else {
        println!("TB är oförändrad - ingen åtgärd krävs.");
    }
    if !result.diff.duplicate_codes.is_empty() {
        println!(
            "AFC.27 - dubblerade TB-koder i handlingen: {}",
            result.diff.duplicate_codes.join(", ")
        );
    }
}

fn watch_file(
    odt: &Path,
    markdown: &Path,
    plan: &Path,
    requirements: &Path,
    state: &Path,
) -> Result<i32, TbError> {
    if !odt.is_file() {
        return Err(TbError(format!("hittar inte {}", odt.display())));
    }
    println!(
        "Bevakar {} (Ctrl+C för att avsluta).",
        odt.file_name().and_then(|s| s.to_str()).unwrap_or("ODT")
    );
    println!("Synkroniserar en gång direkt för att fastställa utgångsläget...");
    run_watch_once(odt, markdown, plan, requirements, state);
    let mut previous = file_signature(odt);
    loop {
        thread::sleep(Duration::from_millis(400));
        let current = file_signature(odt);
        if current != previous {
            thread::sleep(Duration::from_millis(1500));
            let settled = file_signature(odt);
            if settled == current && settled.is_some() {
                run_watch_once(odt, markdown, plan, requirements, state);
                previous = settled;
            }
        }
    }
}

fn run_watch_once(odt: &Path, markdown: &Path, plan: &Path, requirements: &Path, state: &Path) {
    match sync(odt, markdown, plan, requirements, state, true) {
        Ok(result) => describe(&result),
        Err(err) => eprintln!("FEL: {err}"),
    }
}

fn main() {
    match run() {
        Ok(code) => process::exit(code),
        Err(error) => {
            eprintln!("FEL: {error}");
            process::exit(2);
        }
    }
}
