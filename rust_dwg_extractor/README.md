# rust_dwg_extractor

Källkoden till den snabba, läsande DWG-attributextraktorn (Rust +
LibreDWG via bindgen). Detta är **aktiv, körande funktionalitet** —
inte historik — och ligger därför här i projektroten, inte under
`.old/`. Motsvarar `.old/python_v5/src/version_5_rust.py` som
klientkod.

## Status: ombyggd från källa och verifierad

- Extraktionen läser vanliga `ATTRIB.text_value`-värden och R2018+
  flerradiga attribut från det inbäddade `mtext.text`-fältet. Det senare
  behövs eftersom LibreDWG lagrar MText-attributets synliga värde separat
  från dess vanliga attributtext.
- MText-ändringen och prestandaändringarna (se nedan) är inbyggda i den
  aktuella release-binären.
- **Källkod:** `Cargo.toml`, `Cargo.lock`, `build.rs`, `wrapper.h`,
  `src/main.rs` — porterad från den tidigare implementationen och
  spåras i git. Externa LibreDWG-källor och runtime ingår inte i exporten.
- **Byggd binär:** `target/release/rust_dwg_extractor.exe` är nu
  **kompilerad från källa i detta repo** (inte längre bara en kopierad
  artefakt) med `cargo build --release` mot GNU-toolkedjan
  (`x86_64-pc-windows-gnu`).
- Verifierad genom att köra mot exempel-DWG i `Examples/` — identiskt
  utdataprotokoll (`###FILE:n`, attributrader/`###BLOCK_NOT_FOUND`,
  `###END`) som den tidigare kopierade binären, och genom att köra
  `src/version_5_rust.py:extract_dwg_attributes_fast()` från baslinjen
  mot den nybyggda binären via miljövariabeln
  `DATASAMORDNING_RUST_DWG_EXTRACTOR` — samma (förväntade) fel för
  fixture-filer utan `TRVJ_NAMNRUTA`-block som tidigare.
- `vendor/` (~215 MB, LibreDWG-headers/importbibliotek) och
  `.buildtools/` (~80 MB, vendorad `libclang.dll` för `bindgen`) är
  kopierade hit från källrepot och **gitignorade** (byggverktyg/
  tredjepartsbibliotek, inte projektkällkod) — se `.gitignore` i denna
  mapp.

## Verktyg som krävdes (redan installerade på maskinen)

- `rustup` med toolchain `stable-x86_64-pc-windows-gnu` som default
  (**kritiskt**: LibreDWG-importbiblioteket `vendor/libredwg-win64/lib/libredwg.dll.a`
  är i MinGW/GNU ar-format, inte MSVC `.lib` — GNU-target krävs, annars
  misslyckas länkningen).
- MinGW-w64 GCC (`gcc.exe`, WinLibs-distribution) på PATH, för länkning.
- Vendorad `libclang.dll` i `.buildtools/clang/native/`, pekas ut via
  miljövariabeln `LIBCLANG_PATH` vid bygge (behövs av `bindgen`).

## Köra det befintliga bygget

```powershell
.\rust_dwg_extractor\target\release\rust_dwg_extractor.exe "sökväg\till\fil.dwg"
```

Förväntat protokoll på stdout: `###FILE:<index>`, attributrader,
`###BLOCK_NOT_FOUND` eller attributdata, samt `###END`. Detta är samma
protokoll som `.old/python_v5/src/version_5_rust.py` redan tolkar.
Utdata strömmas och flushas efter varje `###END`.

### Worker-läge (`--serve`)

```powershell
.\rust_dwg_extractor\target\release\rust_dwg_extractor.exe --serve
```

Används av `dwg/rust_bridge.py` för att slippa process- och DLL-start per fil.

1. Processen skriver `###READY 1`.
2. Varje stdin-rad är en begäran `BLOCK<TAB>SÖKVÄG` (UTF-8).
3. Svaret är `###LEN <n>` följt av exakt n bytes i det vanliga protokollet
   (`###FILE:0` … `###END`). Felaktig begäran ger `###PROTOCOL_ERROR`.
4. Processen avslutas när stdin stängs.

Workern är skrivskyddad och håller inte DWG-filerna öppna mellan begäranden.
Bryggan startar om den efter 250 filer eftersom LibreDWG-strängar medvetet
läcker (se `get_utf8_field`). En äldre binär utan `--serve` upptäcks
automatiskt och ersätts av batchläget. Workers stängs av med
`DATASAMORDNING_RUST_DWG_WORKER=0`.

**Ombyggnad:** lediga workers låser `rust_dwg_extractor.exe`. Stäng appen och
pågående pytest-körningar innan `cargo build --release`.

### Prestanda

- Blocknamn slås upp en gång per blockdefinition, inte per INSERT.
- Fältnamn är statiska `CStr`-konstanter.
- Release-profil: `lto = true`, `codegen-units = 1`, `panic = "abort"`.
- Mätning (40 DWG-kopior, `extract_many_dwg_attributes_fast`): seriellt
  3,25 s → 2,44 s, 4 parallella 1,39 s → 1,00 s, 8 parallella 0,89 s → 0,69 s.
  En enskild fil via varm worker: ca 52 ms. Resterande tid är LibreDWG:s
  fullständiga `dwg_read_file`.

**Viktigt:** den byggda `.exe`-filen kräver DLL-erna
`libredwg-0.dll`, `libiconv-2.dll`, `libpcre2-8-0.dll` och
`libpcre2-16-0.dll` i samma mapp (`target/release/`). De kopieras dit
manuellt från `vendor/libredwg-win64/` efter varje `cargo build`
tills detta automatiseras i ett byggskript.

## Bygga om från källa

```powershell
cd rust_dwg_extractor
$env:LIBCLANG_PATH = "$(Get-Location)\.buildtools\clang\native"
cargo build --release
Copy-Item vendor\libredwg-win64\libredwg-0.dll target\release\ -Force
Copy-Item vendor\libredwg-win64\libiconv-2.dll target\release\ -Force
Copy-Item vendor\libredwg-win64\libpcre2-8-0.dll target\release\ -Force
Copy-Item vendor\libredwg-win64\libpcre2-16-0.dll target\release\ -Force
```

Detta beslutades och genomfördes i Steg 4, Fas 1 (se
[docs/plan.md](../docs/plan.md)) efter att ha verifierat att både
`rustup` (GNU-toolchain) och MinGW-w64 GCC redan fanns installerade på
utvecklingsmaskinen.
