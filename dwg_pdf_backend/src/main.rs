use std::{env, path::Path};

fn run() -> Result<(), String> {
    let mut args = env::args().skip(1);
    let command = args.next().ok_or_else(|| {
        "Användning: dwg_pdf_backend.exe plot <manifest.tsv> [timeout-sekunder]".to_string()
    })?;
    if command != "plot" {
        return Err(format!("Okänt kommando: {command}"));
    }
    let manifest = args
        .next()
        .ok_or_else(|| "Kommandot plot kräver en manifestsökväg.".to_string())?;
    let timeout = args
        .next()
        .map(|value| {
            value
                .parse::<u64>()
                .map_err(|error| format!("Ogiltig timeout: {error}"))
        })
        .transpose()?
        .unwrap_or(dwg_pdf_backend::DEFAULT_TIMEOUT_SECONDS);
    if args.next().is_some() {
        return Err("För många argument.".into());
    }
    dwg_pdf_backend::plot_manifest(Path::new(&manifest), timeout)
}

fn main() {
    if let Err(error) = run() {
        eprintln!("FEL: {error}");
        std::process::exit(2);
    }
}
