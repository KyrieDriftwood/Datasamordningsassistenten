//! Snabb, skrivskyddad extrahering av TRVJ_NAMNRUTA-attribut ur DWG-filer
//! via LibreDWG (GPLv3), som ersättning för den mycket långsammare
//! accoreconsole-baserade extraheringen i `src/version_5.py`.
//!
//! Endast läsning stöds här: LibreDWGs skrivstöd är instabilt för DWG-format
//! nyare än R2004, så all skrivning (uppdatering av attribut) sker
//! fortfarande via AutoCAD/accoreconsole i den befintliga Python-modulen.
//!
//! Utdataformatet är medvetet identiskt med det AutoLISP-baserade
//! protokollet i `src/version_5.py` (`###FILE:<index>` / `tag\tvärde\tflaggor`
//! rader / `###END` / `###BLOCK_NOT_FOUND`) så att samma Python-parser
//! (`_parse_extract_output`) kan återanvändas oförändrad. `###BLOCK_NOT_FOUND`
//! skrivs för alla fel (inte bara ett faktiskt saknat block) för att hålla
//! sig till detta delade protokoll; sätt miljövariabeln `RDX_PERF=1` för att
//! även se den faktiska felorsaken på stderr vid felsökning.
//!
//! Sökvägar konverteras via `to_dwg_readable_path()` innan `dwg_read_file`
//! anropas, eftersom LibreDWGs C-API tar en smal (8-bitars) sökväg som
//! `fopen` tolkar enligt den lokala ANSI-kodsidan på Windows – en direkt
//! UTF-8-bytekopia av en sökväg med å/ä/ö (mycket vanligt i Trafikverkets
//! projektmappar, t.ex. "Exempelö") gav annars ett tyst I/O-fel som
//! felaktigt visades som att TRVJ_NAMNRUTA-blocket saknades i ritningen.
//!
//! `--serve` startar en långlivad, fortfarande skrivskyddad worker för
//! Python-bryggan. Den skriver `###READY 1`, läser en begäran per rad
//! (`BLOCK<TAB>SÖKVÄG`) och svarar med `###LEN <bytes>` följt av exakt så
//! många bytes i det vanliga `###FILE:0`-protokollet. Längdprefixet gör att
//! attributvärden aldrig kan desynkronisera svarsramarna.

#![allow(non_upper_case_globals, non_camel_case_types, non_snake_case, dead_code)]

use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::io::{self, BufRead, BufWriter, Write};
use std::os::raw::c_char;
use std::process::ExitCode;

mod bindings {
    include!(concat!(env!("OUT_DIR"), "/bindings.rs"));
}

use bindings::*;

const DEFAULT_BLOCK_NAME: &str = "TRVJ_NAMNRUTA";
const SERVE_PROTOCOL_VERSION: u32 = 1;

const BLOCK_HEADER_CLASS: &CStr = c"BLOCK_HEADER";
const ATTRIB_CLASS: &CStr = c"ATTRIB";
const NAME_FIELD: &CStr = c"name";
const TAG_FIELD: &CStr = c"tag";
const TEXT_VALUE_FIELD: &CStr = c"text_value";

const CP_ACP: u32 = 0;

#[link(name = "kernel32")]
extern "system" {
    fn GetShortPathNameW(lpszLongPath: *const u16, lpszShortPath: *mut u16, cchBuffer: u32) -> u32;
    fn WideCharToMultiByte(
        CodePage: u32,
        dwFlags: u32,
        lpWideCharStr: *const u16,
        cchWideChar: i32,
        lpMultiByteStr: *mut i8,
        cbMultiByte: i32,
        lpDefaultChar: *const i8,
        lpUsedDefaultChar: *mut i32,
    ) -> i32;
}

/// Bygger en `CString` som LibreDWGs (rent 8-bitars, ej brett teckensätt)
/// `dwg_read_file` faktiskt kan öppna filen med.
///
/// `path` kommer in som en vanlig Rust-`&str` (alltid UTF-8), men
/// `dwg_read_file` skickar bytesen vidare oförändrade till en C-nivå
/// `fopen`, som på Windows tolkar dem enligt den lokala ANSI-kodsidan – inte
/// UTF-8. En sökväg med å/ä/ö (t.ex. "Exempelö", mycket vanligt i
/// Trafikverkets projektmappar) gav därför tyst DWG_ERR_IOERROR (filen
/// "hittades" aldrig), vilket i sin tur felaktigt rapporterades som att
/// TRVJ_NAMNRUTA-blocket saknades i ritningen – trots att både blocket och
/// alla attribut fanns där (verifierat: AutoCAD/accoreconsole, som själv
/// använder bred sökvägshantering, läste samma fil utan problem).
///
/// Löses genom att först försöka slå upp Windows korta 8.3-sökvägsnamn
/// (`GetShortPathNameW`), som alltid är rent ASCII och därför aldrig kan bli
/// tvetydigt vid en smal konvertering. Om kortnamn skulle vara avstängt på
/// volymen faller vi tillbaka på att konvertera hela sökvägen till den
/// lokala ANSI-kodsidan (`WideCharToMultiByte(CP_ACP, ...)`), vilket
/// hanterar å/ä/ö korrekt på en svensk Windows-installation. Om båda stegen
/// oväntat skulle misslyckas faller vi till sist tillbaka på den gamla
/// direkta UTF-8-byte-kopian, så rent ASCII-sökvägar fortsätter fungera
/// exakt som innan.
fn to_dwg_readable_path(path: &str) -> Result<CString, String> {
    let mut wide: Vec<u16> = path.encode_utf16().collect();
    wide.push(0);

    let mut short_buf = vec![0u16; 32768];
    let short_len =
        unsafe { GetShortPathNameW(wide.as_ptr(), short_buf.as_mut_ptr(), short_buf.len() as u32) };
    let source_wide: Vec<u16> = if short_len > 0 && (short_len as usize) < short_buf.len() {
        short_buf[..short_len as usize].to_vec()
    } else {
        wide[..wide.len() - 1].to_vec()
    };

    let needed = unsafe {
        WideCharToMultiByte(
            CP_ACP,
            0,
            source_wide.as_ptr(),
            source_wide.len() as i32,
            std::ptr::null_mut(),
            0,
            std::ptr::null(),
            std::ptr::null_mut(),
        )
    };
    if needed <= 0 {
        return CString::new(path).map_err(|_| "Ogiltig filsökväg (null-byte).".to_string());
    }
    let mut buf = vec![0u8; needed as usize];
    let written = unsafe {
        WideCharToMultiByte(
            CP_ACP,
            0,
            source_wide.as_ptr(),
            source_wide.len() as i32,
            buf.as_mut_ptr() as *mut i8,
            buf.len() as i32,
            std::ptr::null(),
            std::ptr::null_mut(),
        )
    };
    if written <= 0 {
        return CString::new(path).map_err(|_| "Ogiltig filsökväg (null-byte).".to_string());
    }
    buf.truncate(written as usize);
    CString::new(buf).map_err(|_| "Ogiltig filsökväg (null-byte).".to_string())
}

/// Läser en C-sträng säkert; returnerar tom sträng för nullpekare.
unsafe fn c_str_to_string(ptr: *const c_char) -> String {
    if ptr.is_null() {
        String::new()
    } else {
        unsafe { CStr::from_ptr(ptr) }.to_string_lossy().into_owned()
    }
}

/// LibreDWG lagrar textfält internt som UTF-16 för DWG R2007 och nyare, men
/// exponerar dem via C-structen som en vanlig `char*` (typad `BITCODE_T`).
/// Att läsa fältet direkt ger därför trasig/avhuggen text för sådana filer.
/// `dwg_dynapi_entity_utf8text` gör rätt konvertering oavsett DWG-version,
/// så all textfältsläsning går via den i stället för direkt fältåtkomst.
unsafe fn get_utf8_field(
    obj_ptr: *mut std::os::raw::c_void,
    class_name: &CStr,
    fieldname: &CStr,
) -> String {
    let mut text_ptr: *mut c_char = std::ptr::null_mut();
    let mut is_new: i32 = 0;
    let ok = unsafe {
        dwg_dynapi_entity_utf8text(
            obj_ptr,
            class_name.as_ptr(),
            fieldname.as_ptr(),
            &mut text_ptr as *mut *mut c_char,
            &mut is_new as *mut i32,
            std::ptr::null_mut(),
        )
    };
    if !ok || text_ptr.is_null() {
        return String::new();
    }
    // Den ev. malloc:ade UTF-8-strängen (r2007+) frigörs inte: LibreDWG-DLL:en
    // kan använda en annan C-runtime än Rust-binären, och att frigöra över
    // CRT-gränser är odefinierat. Läckan är begränsad genom att blocknamn
    // cachas per blockdefinition och genom att Python-bryggan återstartar
    // `--serve`-workern efter ett begränsat antal filer.
    unsafe { c_str_to_string(text_ptr) }
}

/// R2018+ multiline ATTRIB values live in the embedded MText payload.
unsafe fn get_mtext_attribute_value(attrib: &Dwg_Entity_ATTRIB) -> String {
    if attrib.mtext_type == 0 {
        return String::new();
    }

    let text_ptr = attrib.mtext.text;
    if text_ptr.is_null() {
        return String::new();
    }

    // R2018+ stores embedded MText as UTF-16; the public field is still
    // typed BITCODE_T, so read its code units explicitly.
    let mut length = 0;
    let wide_ptr = text_ptr as *const u16;
    while unsafe { *wide_ptr.add(length) } != 0 {
        length += 1;
    }
    let units = unsafe { std::slice::from_raw_parts(wide_ptr, length) };
    String::from_utf16_lossy(units)
}

/// Läser namnet på en blockdefinition (BLOCK_HEADER), eller None om
/// objektet saknar den förväntade strukturen.
unsafe fn block_definition_name(block_obj_ptr: *mut Dwg_Object) -> Option<String> {
    let block_obj = unsafe { &*block_obj_ptr };
    // Dwg_Object.tio är { entity; object; }; ett BLOCK_HEADER är ett
    // "object" (tabellpost), vars egen tio-union i sin tur innehåller
    // det specifika BLOCK_HEADER-structet.
    let generic_object_ptr = unsafe { block_obj.tio.object };
    if generic_object_ptr.is_null() {
        return None;
    }
    let block_header_ptr = unsafe { (*generic_object_ptr).tio.BLOCK_HEADER };
    if block_header_ptr.is_null() {
        return None;
    }
    Some(unsafe {
        get_utf8_field(
            block_header_ptr as *mut std::os::raw::c_void,
            BLOCK_HEADER_CLASS,
            NAME_FIELD,
        )
    })
}

/// Extraherar attribut från en enskild DWG-fil. Returnerar en lista av
/// (tag, värde, flaggor)-tripplar, eller `Err` om filen inte kunde läsas
/// eller blocket saknas i ritningen.
fn extract_one(path: &str, requested_block: &str) -> Result<Vec<(String, String, i32)>, String> {
    let c_path = to_dwg_readable_path(path)?;

    // Dwg_Data måste vara nollställd innan dwg_read_file anropas.
    let mut dwg: Dwg_Data = unsafe { std::mem::zeroed() };

    let read_result = unsafe { dwg_read_file(c_path.as_ptr(), &mut dwg as *mut Dwg_Data) };
    // LibreDWG kan returnera en bitmask av icke-kritiska varningsflaggor
    // (t.ex. okända klasser) och ändå ha läst in en fullt användbar DWG.
    // Endast värden >= 128 (DWG_ERR_CLASSESNOTFOUND / DWG_ERR_CRITICAL i
    // dwg.h) betraktas som ett fatalt läsfel; makrot exporteras inte av
    // bindgen så tröskeln är hårdkodad här och speglar dwg.h exakt.
    const DWG_ERR_CRITICAL_THRESHOLD: i32 = 128;
    if read_result >= DWG_ERR_CRITICAL_THRESHOLD {
        unsafe { dwg_free(&mut dwg as *mut Dwg_Data) };
        return Err(format!(
            "LibreDWG kunde inte läsa filen (felkod {read_result})."
        ));
    }

    let mut found: Option<Vec<(String, String, i32)>> = None;
    // Många INSERT kan peka på samma blockdefinition. Namnuppslaget kostar
    // allokering och UTF-8-konvertering, så resultatet cachas per definition
    // (nyckel: objektpekaren, giltig så länge `dwg` inte frigjorts).
    let mut block_name_matches: HashMap<usize, bool> = HashMap::new();

    let num_objects = dwg.num_objects as usize;
    for i in 0..num_objects {
        let obj_ptr = unsafe { dwg.object.add(i) };
        if obj_ptr.is_null() {
            continue;
        }
        let obj = unsafe { &*obj_ptr };
        if obj.fixedtype != DWG_OBJECT_TYPE_DWG_TYPE_INSERT {
            continue;
        }

        let insert_ptr = unsafe { dwg_object_to_INSERT(obj_ptr as *mut Dwg_Object) };
        if insert_ptr.is_null() {
            continue;
        }
        let insert = unsafe { &*insert_ptr };

        // Slå upp blockets namn via block_header-handtaget.
        if insert.block_header.is_null() {
            continue;
        }
        let block_obj_ptr =
            unsafe { dwg_ref_object(&mut dwg as *mut Dwg_Data, insert.block_header) };
        if block_obj_ptr.is_null() {
            continue;
        }
        let is_requested_block = *block_name_matches
            .entry(block_obj_ptr as usize)
            .or_insert_with(|| {
                unsafe { block_definition_name(block_obj_ptr) }
                    .is_some_and(|name| name == requested_block)
            });
        if !is_requested_block {
            continue;
        }

        // Matchande INSERT hittad: läs alla bifogade ATTRIB-entiteter.
        let mut fields: Vec<(String, String, i32)> = Vec::new();
        let num_owned = insert.num_owned as usize;
        if !insert.attribs.is_null() {
            for j in 0..num_owned {
                let attrib_ref = unsafe { *insert.attribs.add(j) };
                if attrib_ref.is_null() {
                    continue;
                }
                let attrib_obj_ptr =
                    unsafe { dwg_ref_object(&mut dwg as *mut Dwg_Data, attrib_ref) };
                if attrib_obj_ptr.is_null() {
                    continue;
                }
                let attrib_ptr = unsafe { dwg_object_to_ATTRIB(attrib_obj_ptr) };
                if attrib_ptr.is_null() {
                    continue;
                }
                let attrib = unsafe { &*attrib_ptr };
                let tag = unsafe {
                    get_utf8_field(attrib_ptr as *mut std::os::raw::c_void, ATTRIB_CLASS, TAG_FIELD)
                };
                if tag.is_empty() {
                    continue;
                }
                let mut value = unsafe {
                    get_utf8_field(
                        attrib_ptr as *mut std::os::raw::c_void,
                        ATTRIB_CLASS,
                        TEXT_VALUE_FIELD,
                    )
                };
                let mtext_value = unsafe { get_mtext_attribute_value(attrib) };
                if !mtext_value.is_empty() {
                    value = mtext_value;
                }
                fields.push((tag, value, attrib.flags as i32));
            }
        }

        found = Some(fields);
        break;
    }

    unsafe { dwg_free(&mut dwg as *mut Dwg_Data) };

    match found {
        Some(fields) => Ok(fields),
        None => Err(format!("Hittade inget block med namnet {requested_block}.")),
    }
}

const USAGE: &str =
    "Användning: rust_dwg_extractor.exe [--block BLOCK] <fil1.dwg> [fil2.dwg ...]\n             rust_dwg_extractor.exe --serve";

/// Skriver en fils protokollsegment (`###FILE:index` ... `###END`).
/// Delas av CLI- och `--serve`-läget så att båda ger identisk utdata.
fn write_extraction<W: Write>(
    out: &mut W,
    index: usize,
    path: &str,
    requested_block: &str,
    show_perf: bool,
) -> io::Result<()> {
    let file_start = std::time::Instant::now();
    writeln!(out, "###FILE:{index}")?;
    match extract_one(path, requested_block) {
        Ok(fields) => {
            for (tag, value, flags) in fields {
                writeln!(out, "{tag}\t{value}\t{flags}")?;
            }
        }
        Err(message) => {
            if show_perf {
                eprintln!("[error] {path}: {message}");
            }
            // Protokollet skiljer avsiktligt inte på läsfel och saknat block.
            writeln!(out, "###BLOCK_NOT_FOUND")?;
        }
    }
    writeln!(out, "###END")?;
    if show_perf {
        eprintln!(
            "[perf] {} extraherad på {:.3} s",
            path,
            file_start.elapsed().as_secs_f64()
        );
    }
    Ok(())
}

/// Batchläge: alla filer i en process, strömmad utdata. Flush efter varje
/// `###END` så att anroparen kan rapportera förlopp per fil.
fn run_batch(requested_block: &str, paths: &[String], show_perf: bool) -> io::Result<()> {
    let total_start = std::time::Instant::now();
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    for (index, path) in paths.iter().enumerate() {
        write_extraction(&mut out, index, path, requested_block, show_perf)?;
        out.flush()?;
    }
    if show_perf {
        eprintln!(
            "[perf] totalt {} fil(er) på {:.3} s",
            paths.len(),
            total_start.elapsed().as_secs_f64()
        );
    }
    Ok(())
}

/// Worker-läge för Python-bryggan. Avslutas när stdin stängs.
fn run_serve(show_perf: bool) -> io::Result<()> {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    writeln!(out, "###READY {SERVE_PROTOCOL_VERSION}")?;
    out.flush()?;

    let mut input = stdin.lock();
    let mut line = String::new();
    let mut payload: Vec<u8> = Vec::with_capacity(4096);
    loop {
        line.clear();
        if input.read_line(&mut line)? == 0 {
            return Ok(());
        }
        let request = line.trim_end_matches(['\r', '\n']);
        if request.is_empty() {
            continue;
        }
        payload.clear();
        match request.split_once('\t') {
            Some((block, path)) if !block.is_empty() && !path.is_empty() => {
                write_extraction(&mut payload, 0, path, block, show_perf)?;
            }
            _ => payload.extend_from_slice(b"###PROTOCOL_ERROR\n"),
        }
        writeln!(out, "###LEN {}", payload.len())?;
        out.write_all(&payload)?;
        out.flush()?;
    }
}

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    // Prestandaloggning till stderr är valfri (RDX_PERF=1), så att den
    // maskinläsbara stdout-strömmen alltid förblir ren för Python-parsern.
    let show_perf = std::env::var("RDX_PERF").is_ok();

    if args.len() == 1 && args[0] == "--serve" {
        return match run_serve(show_perf) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("[error] serve: {error}");
                ExitCode::FAILURE
            }
        };
    }

    let requested_block = if args.first().is_some_and(|arg| arg == "--block") {
        if args.len() < 3 {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
        let block = args[1].clone();
        args.drain(0..2);
        block
    } else {
        DEFAULT_BLOCK_NAME.to_string()
    };
    if args.is_empty() {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    }

    match run_batch(&requested_block, &args, show_perf) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("[error] stdout: {error}");
            ExitCode::FAILURE
        }
    }
}
