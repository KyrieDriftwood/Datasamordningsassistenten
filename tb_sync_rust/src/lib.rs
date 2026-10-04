use quick_xml::{events::Event, Reader};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::{Cursor, Read},
    path::{Path, PathBuf},
    time::SystemTime,
};

pub const GENERATED_HEADER: &str = "<!-- GENERERAD FIL - REDIGERA INTE. -->\n<!-- Primarkalla: TB - Datasamordningsassistenten.odt -->\n<!-- Uppdateras med: python -m tb.sync -->\n";

#[derive(Debug)]
pub struct TbError(pub String);

impl std::fmt::Display for TbError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TbError {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Section {
    pub code: String,
    pub title: String,
    pub level: usize,
    pub body: String,
}

impl Section {
    pub fn heading(&self) -> String {
        let text = if self.title.is_empty() {
            self.code.clone()
        } else {
            format!("{} – {}", self.code, self.title)
        };
        format!("{} {}", "#".repeat(self.level), text)
    }

    pub fn fingerprint(&self) -> String {
        format!("{}\0{}\0{}", self.code, self.title, self.body)
    }
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TbDocument {
    pub preamble: String,
    pub sections: Vec<Section>,
}

impl TbDocument {
    pub fn section_map(&self) -> HashMap<String, Section> {
        let mut result = HashMap::new();
        for section in &self.sections {
            result
                .entry(section.code.clone())
                .or_insert_with(|| section.clone());
        }
        result
    }

    pub fn duplicate_codes(&self) -> Vec<String> {
        let mut seen = HashSet::new();
        let mut emitted = HashSet::new();
        let mut result = Vec::new();
        for section in &self.sections {
            if !seen.insert(section.code.clone()) && emitted.insert(section.code.clone()) {
                result.push(section.code.clone());
            }
        }
        result
    }
}

pub fn parse_heading(text: &str) -> Option<(String, String)> {
    let line = text.trim();
    if line.is_empty() || line.chars().count() > 130 || ".,:;".contains(line.chars().last()?) {
        return None;
    }
    let upper_len = line.chars().take_while(|c| c.is_ascii_uppercase()).count();
    if upper_len == 0 {
        return None;
    }
    for count in (1..=upper_len.min(4)).rev() {
        let letters: String = line.chars().take(count).collect();
        let rest: String = line.chars().skip(count).collect();
        let mut choices = Vec::new();
        if let Some(suffix) = rest.strip_prefix('.') {
            let digits = suffix.chars().take_while(|c| c.is_ascii_digit()).count();
            if (1..=3).contains(&digits) {
                let code = format!("{}.{}", letters, &suffix[..digits]);
                choices.push((code, suffix[digits..].to_string()));
            }
        }
        choices.push((letters, rest));
        for (code, remainder) in choices {
            let (separator, title) = split_heading_remainder(&remainder);
            let title = title.trim();
            if title.is_empty() {
                continue;
            }
            let has_dash = separator.chars().any(|c| matches!(c, '–' | '—' | '-'));
            let first_two: Vec<char> = title.chars().take(2).collect();
            let title_word = first_two.len() >= 2
                && matches!(first_two[0], 'A'..='Z' | 'Å' | 'Ä' | 'Ö')
                && matches!(first_two[1], 'a'..='z' | 'å' | 'ä' | 'ö' | 'é' | 'ü');
            if has_dash || title_word {
                return Some((code, title.to_string()));
            }
        }
    }
    None
}

fn split_heading_remainder(rest: &str) -> (String, String) {
    let trimmed_len = rest.len() - rest.trim_start().len();
    let leading = &rest[..trimmed_len];
    let remaining = &rest[trimmed_len..];
    if let Some(ch) = remaining.chars().next() {
        if matches!(ch, '–' | '—') {
            let separator_len = ch.len_utf8()
                + remaining[ch.len_utf8()..]
                    .chars()
                    .take_while(|c| c.is_whitespace())
                    .map(char::len_utf8)
                    .sum::<usize>();
            return (
                format!("{leading}{}", &remaining[..separator_len]),
                remaining[separator_len..].to_string(),
            );
        }
        if ch == '-' {
            let after = &remaining[1..];
            if let Some(space) = after.chars().next() {
                if space.is_whitespace() {
                    let spaces = after
                        .chars()
                        .take_while(|c| c.is_whitespace())
                        .map(char::len_utf8)
                        .sum::<usize>();
                    if !leading.is_empty() {
                        return (
                            format!("{leading}-{}", &after[..spaces]),
                            after[spaces..].to_string(),
                        );
                    }
                }
            }
        }
    }
    if !leading.is_empty() {
        return (leading.to_string(), remaining.to_string());
    }
    (String::new(), rest.to_string())
}

pub fn heading_level(code: &str) -> usize {
    let (letters, numbered) = code
        .split_once('.')
        .map_or((code, false), |(a, _)| (a, true));
    (1 + letters.chars().count() + if numbered { 1 } else { 0 }).min(6)
}

#[derive(Debug, Default)]
struct XmlNode {
    name: String,
    attrs: HashMap<String, String>,
    children: Vec<XmlPart>,
}

#[derive(Debug)]
enum XmlPart {
    Text(String),
    Node(XmlNode),
}

fn local_name(name: &[u8]) -> String {
    let name = String::from_utf8_lossy(name);
    name.rsplit(':').next().unwrap_or(&name).to_string()
}

fn parse_xml(xml: &[u8]) -> Result<XmlNode, TbError> {
    let mut reader = Reader::from_reader(xml);
    reader.config_mut().trim_text(false);
    let mut stack: Vec<XmlNode> = Vec::new();
    let mut root = None;
    loop {
        match reader.read_event() {
            Ok(Event::Start(e)) => {
                let mut node = XmlNode {
                    name: local_name(e.name().as_ref()),
                    ..Default::default()
                };
                for attr in e.attributes() {
                    let attr = attr.map_err(|e| TbError(format!("Ogiltig XML-attribut: {e}")))?;
                    let value = attr
                        .decode_and_unescape_value(reader.decoder())
                        .map_err(|e| TbError(format!("Ogiltigt XML-attributvärde: {e}")))?;
                    node.attrs
                        .insert(local_name(attr.key.as_ref()), value.into_owned());
                }
                stack.push(node);
            }
            Ok(Event::Empty(e)) => {
                let mut node = XmlNode {
                    name: local_name(e.name().as_ref()),
                    ..Default::default()
                };
                for attr in e.attributes() {
                    let attr = attr.map_err(|e| TbError(format!("Ogiltig XML-attribut: {e}")))?;
                    let value = attr
                        .decode_and_unescape_value(reader.decoder())
                        .map_err(|e| TbError(format!("Ogiltigt XML-attributvärde: {e}")))?;
                    node.attrs
                        .insert(local_name(attr.key.as_ref()), value.into_owned());
                }
                append_node(node, &mut stack, &mut root);
            }
            Ok(Event::Text(e)) => {
                let text = e
                    .unescape()
                    .map_err(|e| TbError(format!("Ogiltig XML-entitet: {e}")))?
                    .into_owned();
                if let Some(node) = stack.last_mut() {
                    node.children.push(XmlPart::Text(text));
                }
            }
            Ok(Event::CData(e)) => {
                let text = e
                    .decode()
                    .map_err(|e| TbError(format!("Ogiltig XML-text: {e}")))?
                    .into_owned();
                if let Some(node) = stack.last_mut() {
                    node.children.push(XmlPart::Text(text));
                }
            }
            Ok(Event::End(_)) => {
                let node = stack
                    .pop()
                    .ok_or_else(|| TbError("Ogiltig XML: obalanserad sluttagg".into()))?;
                append_node(node, &mut stack, &mut root);
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(e) => return Err(TbError(format!("Ogiltig XML: {e}"))),
        }
    }
    root.ok_or_else(|| TbError("Ogiltig XML: dokumentet saknar rot-element".into()))
}

fn append_node(node: XmlNode, stack: &mut [XmlNode], root: &mut Option<XmlNode>) {
    if let Some(parent) = stack.last_mut() {
        parent.children.push(XmlPart::Node(node));
    } else {
        *root = Some(node);
    }
}

fn child<'a>(node: &'a XmlNode, name: &str) -> Option<&'a XmlNode> {
    node.children.iter().find_map(|part| match part {
        XmlPart::Node(n) if n.name == name => Some(n),
        _ => None,
    })
}

fn element_text(node: &XmlNode) -> String {
    let mut result = String::new();
    for part in &node.children {
        match part {
            XmlPart::Text(text) => result.push_str(text),
            XmlPart::Node(child) => match child.name.as_str() {
                "s" => {
                    let count = child
                        .attrs
                        .get("c")
                        .and_then(|v| v.parse::<usize>().ok())
                        .unwrap_or(1);
                    result.push_str(&" ".repeat(count));
                }
                "tab" => result.push('\t'),
                "line-break" => result.push('\n'),
                "image" => {
                    let href = child
                        .attrs
                        .get("href")
                        .map(String::as_str)
                        .unwrap_or("bild");
                    let filename = href.rsplit('/').next().unwrap_or(href);
                    result.push_str(&format!("![{filename}]({href})"));
                }
                _ => result.push_str(&element_text(child)),
            },
        }
    }
    result
}

fn normalise(text: &str) -> String {
    text.split('\n')
        .map(|line| {
            line.chars()
                .collect::<Vec<_>>()
                .split(|c| matches!(c, ' ' | '\t' | '\u{a0}'))
                .filter(|p| !p.is_empty())
                .map(|p| p.iter().collect::<String>())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn render_table(table: &XmlNode) -> Vec<String> {
    let mut rows = Vec::<Vec<String>>::new();
    collect_table_rows(table, &mut rows);
    rows.retain(|row| !row.is_empty());
    if rows.is_empty() {
        return Vec::new();
    }
    let width = rows.iter().map(Vec::len).max().unwrap_or(0);
    for row in &mut rows {
        row.resize(width, String::new());
    }
    let mut lines = vec![
        format!("| {} |", rows[0].join(" | ")),
        format!("|{}", "---|".repeat(width)),
    ];
    lines.extend(
        rows.iter()
            .skip(1)
            .map(|row| format!("| {} |", row.join(" | "))),
    );
    lines
}

fn collect_table_rows(node: &XmlNode, rows: &mut Vec<Vec<String>>) {
    for part in &node.children {
        if let XmlPart::Node(row) = part {
            if row.name == "table-row" {
                let mut cells = Vec::new();
                for cpart in &row.children {
                    if let XmlPart::Node(cell) = cpart {
                        if cell.name == "table-cell" {
                            let value = normalise(&element_text(cell))
                                .replace('\n', "<br>")
                                .replace('|', "\\|");
                            let repeat = cell
                                .attrs
                                .get("number-columns-repeated")
                                .and_then(|v| v.parse::<usize>().ok())
                                .unwrap_or(1)
                                .min(10_000);
                            cells.extend(std::iter::repeat(value).take(repeat));
                        }
                    }
                }
                while cells.last().is_some_and(String::is_empty) {
                    cells.pop();
                }
                if !cells.is_empty() {
                    rows.push(cells);
                }
            } else {
                collect_table_rows(row, rows);
            }
        }
    }
}

fn blocks(node: &XmlNode, depth: usize) -> Vec<(String, String)> {
    let mut result = Vec::new();
    for part in &node.children {
        let XmlPart::Node(n) = part else { continue };
        match n.name.as_str() {
            "p" | "h" => {
                let text = normalise(&element_text(n));
                if !text.is_empty() {
                    result.push(("paragraph".into(), text));
                }
            }
            "list" => {
                for item in n.children.iter().filter_map(|p| match p {
                    XmlPart::Node(x) if x.name == "list-item" => Some(x),
                    _ => None,
                }) {
                    for (kind, text) in blocks(item, depth + 1) {
                        if kind == "paragraph" {
                            result.push(("list".into(), format!("{}- {text}", "  ".repeat(depth))));
                        } else {
                            result.push((kind, text));
                        }
                    }
                }
            }
            "table" => {
                let lines = render_table(n);
                if !lines.is_empty() {
                    result.push(("table".into(), lines.join("\n")));
                }
            }
            "section" | "soft-page-break" => result.extend(blocks(n, depth)),
            _ => {}
        }
    }
    result
}

fn join_blocks(blocks: &[(String, String)]) -> String {
    let mut result = String::new();
    for (index, (kind, text)) in blocks.iter().enumerate() {
        if index > 0 {
            if kind == "list" && blocks[index - 1].0 == "list" {
                result.push('\n');
            } else {
                result.push_str("\n\n");
            }
        }
        result.push_str(text);
    }
    result.trim().to_string()
}

pub fn parse_odt_bytes(bytes: &[u8], filename: &str) -> Result<TbDocument, TbError> {
    let mut archive = zip::ZipArchive::new(Cursor::new(bytes))
        .map_err(|e| TbError(format!("Kunde inte läsa ODT-innehållet i {filename}: {e}")))?;
    let mut content = Vec::new();
    archive
        .by_name("content.xml")
        .map_err(|e| TbError(format!("Kunde inte läsa ODT-innehållet i {filename}: {e}")))?
        .read_to_end(&mut content)
        .map_err(|e| TbError(format!("Kunde inte läsa ODT-innehållet i {filename}: {e}")))?;
    let root =
        parse_xml(&content).map_err(|e| TbError(format!("Ogiltig XML i {filename}: {e}")))?;
    let body = child(&root, "body")
        .and_then(|n| child(n, "text"))
        .ok_or_else(|| TbError(format!("Hittade ingen brödtext i {filename}")))?;
    let mut doc = TbDocument::default();
    let mut preamble: Vec<(String, String)> = Vec::new();
    let mut current: Option<Section> = None;
    let mut content_blocks: Vec<(String, String)> = Vec::new();
    for (kind, text) in blocks(body, 0) {
        if kind == "paragraph" {
            if let Some((code, title)) = parse_heading(&text) {
                if let Some(mut section) = current.take() {
                    section.body = join_blocks(&content_blocks);
                    doc.sections.push(section);
                }
                content_blocks.clear();
                current = Some(Section {
                    level: heading_level(&code),
                    code,
                    title,
                    body: String::new(),
                });
                continue;
            }
        }
        if current.is_some() {
            content_blocks.push((kind, text));
        } else {
            preamble.push((kind, text));
        }
    }
    if let Some(mut section) = current {
        section.body = join_blocks(&content_blocks);
        doc.sections.push(section);
    }
    doc.preamble = join_blocks(&preamble);
    Ok(doc)
}

pub fn parse_odt(path: &Path) -> Result<TbDocument, TbError> {
    if !path.is_file() {
        return Err(TbError(format!(
            "TB-handlingen hittades inte: {}",
            path.display()
        )));
    }
    let bytes =
        fs::read(path).map_err(|e| TbError(format!("Kunde inte läsa {}: {e}", path.display())))?;
    parse_odt_bytes(
        &bytes,
        path.file_name().and_then(|s| s.to_str()).unwrap_or("ODT"),
    )
}

pub fn render_markdown(document: &TbDocument, title: &str) -> String {
    let mut parts = vec![GENERATED_HEADER.to_string(), format!("# {title}\n")];
    if !document.preamble.is_empty() {
        parts.push(format!("{}\n", document.preamble));
    }
    for section in &document.sections {
        parts.push(format!("{}\n", section.heading()));
        if !section.body.is_empty() {
            parts.push(format!("{}\n", section.body));
        }
    }
    format!("{}\n", parts.join("\n").trim_end())
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SectionChange {
    pub code: String,
    pub kind: String,
    pub title: String,
    pub before: Option<Section>,
    pub after: Option<Section>,
}

#[derive(Clone, Debug, Default)]
pub struct TbDiff {
    pub changes: Vec<SectionChange>,
    pub duplicate_codes: Vec<String>,
}

impl TbDiff {
    pub fn has_changes(&self) -> bool {
        self.changes.iter().any(|c| c.kind != "moved")
    }
    pub fn by_kind(&self, kind: &str) -> Vec<&SectionChange> {
        self.changes.iter().filter(|c| c.kind == kind).collect()
    }
    pub fn summary(&self) -> String {
        if self.changes.is_empty() {
            return "Inga ändringar i TB.".into();
        }
        [
            ("added", "tillagda"),
            ("removed", "borttagna"),
            ("modified", "ändrade"),
            ("moved", "flyttade"),
        ]
        .iter()
        .filter_map(|(kind, label)| {
            let count = self.changes.iter().filter(|c| c.kind == *kind).count();
            (count > 0).then(|| format!("{count} {label}"))
        })
        .collect::<Vec<_>>()
        .join(", ")
    }
}

pub fn diff_documents(previous: &TbDocument, current: &TbDocument) -> TbDiff {
    let before = previous.section_map();
    let after = current.section_map();
    let before_order: HashMap<_, _> = previous
        .sections
        .iter()
        .enumerate()
        .map(|(i, s)| (s.code.clone(), i))
        .collect();
    let after_order: HashMap<_, _> = current
        .sections
        .iter()
        .enumerate()
        .map(|(i, s)| (s.code.clone(), i))
        .collect();
    let mut diff = TbDiff {
        changes: Vec::new(),
        duplicate_codes: current.duplicate_codes(),
    };
    for s in &current.sections {
        if !before.contains_key(&s.code) && !diff.changes.iter().any(|c| c.code == s.code) {
            diff.changes.push(SectionChange {
                code: s.code.clone(),
                kind: "added".into(),
                title: s.title.clone(),
                before: None,
                after: Some(s.clone()),
            });
        }
    }
    for s in &previous.sections {
        if !after.contains_key(&s.code) && !diff.changes.iter().any(|c| c.code == s.code) {
            diff.changes.push(SectionChange {
                code: s.code.clone(),
                kind: "removed".into(),
                title: s.title.clone(),
                before: Some(s.clone()),
                after: None,
            });
        }
    }
    for (code, a) in after {
        if let Some(b) = before.get(&code) {
            if b.fingerprint() != a.fingerprint() {
                diff.changes.push(SectionChange {
                    code,
                    kind: "modified".into(),
                    title: a.title.clone(),
                    before: Some(b.clone()),
                    after: Some(a),
                });
            } else if before_order.get(&code) != after_order.get(&code) {
                diff.changes.push(SectionChange {
                    code,
                    kind: "moved".into(),
                    title: a.title.clone(),
                    before: Some(b.clone()),
                    after: Some(a),
                });
            }
        }
    }
    let order = |kind: &str| match kind {
        "added" => 0,
        "modified" => 1,
        "removed" => 2,
        _ => 3,
    };
    diff.changes
        .sort_by(|a, b| (order(&a.kind), &a.code).cmp(&(order(&b.kind), &b.code)));
    diff
}

pub fn unified_diff(change: &SectionChange) -> String {
    let before = change
        .before
        .as_ref()
        .map(|s| s.body.lines().map(str::to_string).collect::<Vec<_>>())
        .unwrap_or_default();
    let after = change
        .after
        .as_ref()
        .map(|s| s.body.lines().map(str::to_string).collect::<Vec<_>>())
        .unwrap_or_default();
    let ops = line_diff(&before, &after);
    if ops.iter().all(|op| matches!(op, DiffOp::Equal(_))) {
        return String::new();
    }
    let mut changed = Vec::new();
    for (index, op) in ops.iter().enumerate() {
        if !matches!(op, DiffOp::Equal(_)) {
            changed.push((index.saturating_sub(1), (index + 2).min(ops.len())));
        }
    }
    let mut groups: Vec<(usize, usize)> = Vec::new();
    for (start, end) in changed {
        if let Some(last) = groups.last_mut() {
            if start <= last.1 + 1 {
                last.1 = end;
                continue;
            }
        }
        groups.push((start, end));
    }
    let mut output = format!("--- {} (tidigare)\n+++ {} (ny)", change.code, change.code);
    for (start, end) in groups {
        let a_start = ops[..start]
            .iter()
            .filter(|x| !matches!(x, DiffOp::Insert(_)))
            .count();
        let b_start = ops[..start]
            .iter()
            .filter(|x| !matches!(x, DiffOp::Delete(_)))
            .count();
        let a_count = ops[start..end]
            .iter()
            .filter(|x| !matches!(x, DiffOp::Insert(_)))
            .count();
        let b_count = ops[start..end]
            .iter()
            .filter(|x| !matches!(x, DiffOp::Delete(_)))
            .count();
        output.push_str(&format!(
            "\n@@ -{} +{} @@",
            range_start(a_start, a_count),
            range_start(b_start, b_count)
        ));
        for op in &ops[start..end] {
            match op {
                DiffOp::Equal(s) => output.push_str(&format!("\n {s}")),
                DiffOp::Delete(s) => output.push_str(&format!("\n-{s}")),
                DiffOp::Insert(s) => output.push_str(&format!("\n+{s}")),
            }
        }
    }
    output
}

fn range_start(start: usize, count: usize) -> String {
    if count == 0 {
        start.to_string()
    } else if count == 1 {
        (start + 1).to_string()
    } else {
        format!("{},{}", start + 1, count)
    }
}

#[derive(Clone)]
enum DiffOp {
    Equal(String),
    Delete(String),
    Insert(String),
}

fn line_diff(a: &[String], b: &[String]) -> Vec<DiffOp> {
    let mut lcs = vec![vec![0usize; b.len() + 1]; a.len() + 1];
    for i in (0..a.len()).rev() {
        for j in (0..b.len()).rev() {
            lcs[i][j] = if a[i] == b[j] {
                lcs[i + 1][j + 1] + 1
            } else {
                lcs[i + 1][j].max(lcs[i][j + 1])
            };
        }
    }
    let (mut i, mut j) = (0, 0);
    let mut ops = Vec::new();
    while i < a.len() || j < b.len() {
        if i < a.len() && j < b.len() && a[i] == b[j] {
            ops.push(DiffOp::Equal(a[i].clone()));
            i += 1;
            j += 1;
        } else if i < a.len() && (j == b.len() || lcs[i + 1][j] >= lcs[i][j + 1]) {
            ops.push(DiffOp::Delete(a[i].clone()));
            i += 1;
        } else {
            ops.push(DiffOp::Insert(b[j].clone()));
            j += 1;
        }
    }
    ops
}

pub fn render_diff_report(diff: &TbDiff) -> String {
    if diff.changes.is_empty() {
        return "Ingen skillnad mot föregående version.".into();
    }
    let mut blocks = Vec::new();
    for change in &diff.changes {
        let heading = format!("{} – {} [{}]", change.code, change.title, change.kind);
        let mut lines = vec![heading.clone(), "-".repeat(heading.chars().count())];
        match change.kind.as_str() {
            "moved" => lines.push("Endast flyttad i dokumentet. Innehållet är oförändrat.".into()),
            "added" => lines.push(
                change
                    .after
                    .as_ref()
                    .map(|s| s.body.trim())
                    .filter(|s| !s.is_empty())
                    .unwrap_or("(sektionen har ingen brödtext)")
                    .into(),
            ),
            "removed" => {
                lines.push("Borttagen. Tidigare lydelse:".into());
                lines.push(
                    change
                        .before
                        .as_ref()
                        .map(|s| s.body.trim())
                        .filter(|s| !s.is_empty())
                        .unwrap_or("(sektionen hade ingen brödtext)")
                        .into(),
                );
            }
            _ => {
                let d = unified_diff(change);
                lines.push(if d.is_empty() {
                    "(endast rubriken ändrad)".into()
                } else {
                    d
                });
            }
        }
        lines.push(String::new());
        blocks.push(lines.join("\n"));
    }
    blocks.join("\n").trim_end().to_string()
}

#[derive(Clone, Debug)]
pub struct RequirementRow {
    pub requirement_id: String,
    pub tb_section: String,
    pub requirement: String,
    pub modules: String,
    pub status: String,
}

pub type RequirementIndex = Vec<(String, Vec<RequirementRow>)>;

pub fn load_requirement_index(path: &Path) -> Result<RequirementIndex, TbError> {
    let mut index: RequirementIndex = Vec::new();
    if !path.is_file() {
        return Ok(index);
    }
    let text = fs::read_to_string(path)
        .map_err(|e| TbError(format!("Kunde inte läsa {}: {e}", path.display())))?;
    for line in text.lines() {
        let line = line.trim();
        if !line.starts_with('|') {
            continue;
        }
        let Some(end) = line.rfind('|') else { continue };
        let cells: Vec<_> = line[1..end].split('|').map(str::trim).collect();
        if cells.len() < 6
            || cells[0].chars().all(|c| c == '-' || c == ':')
            || cells[0].is_empty()
            || cells[0] == "Krav-ID"
        {
            continue;
        }
        let code_len = cells[1]
            .chars()
            .take_while(|c| c.is_ascii_uppercase())
            .count();
        if code_len == 0 || code_len > 4 {
            continue;
        }
        let mut code: String = cells[1].chars().take(code_len).collect();
        let rest: String = cells[1].chars().skip(code_len).collect();
        if let Some(suffix) = rest.strip_prefix('.') {
            let n = suffix.chars().take_while(|c| c.is_ascii_digit()).count();
            if (1..=3).contains(&n) {
                code.push_str(&format!(".{}", &suffix[..n]));
            }
        }
        let row = RequirementRow {
            requirement_id: cells[0].into(),
            tb_section: cells[1].into(),
            requirement: cells[2].into(),
            modules: cells[4].into(),
            status: cells[5].into(),
        };
        if let Some(position) = index.iter().position(|(indexed, _)| indexed == &code) {
            index[position].1.push(row);
        } else {
            index.push((code, vec![row]));
        }
    }
    Ok(index)
}

fn related_rows<'a>(code: &str, index: &'a RequirementIndex) -> Vec<&'a RequirementRow> {
    index
        .iter()
        .filter(|(key, _)| key.as_str() == code || key.starts_with(code))
        .flat_map(|(_, rows)| rows)
        .collect()
}

fn render_change(change: &SectionChange, index: &RequirementIndex) -> Vec<String> {
    let label = match change.kind.as_str() {
        "added" => "Ny sektion",
        "removed" => "Borttagen sektion",
        "modified" => "Ändrad sektion",
        _ => "Flyttad sektion (oförändrat innehåll)",
    };
    let heading = if change.title.is_empty() {
        change.code.clone()
    } else {
        format!("{} – {}", change.code, change.title)
    };
    let mut lines = vec![format!("### {label}: {heading}"), String::new()];
    let rows = related_rows(&change.code, index);
    let mut modules = Vec::new();
    for row in &rows {
        for module in row.modules.split(',').map(str::trim) {
            if !module.is_empty() && module != "—" && module != "-" && !modules.contains(&module)
            {
                modules.push(module);
            }
        }
    }
    if modules.is_empty() {
        lines.push("**Berörda moduler:** okänt — sektionen saknar rad i [kravsparning.md](kravsparning.md). Lägg till en rad där som en del av åtgärden.".into());
    } else {
        lines.push(format!("**Berörda moduler:** {}", modules.join(", ")));
    }
    lines.push(String::new());
    if !rows.is_empty() {
        lines.extend([
            "| Krav-ID | Krav | Modul | Status före ändringen |".into(),
            "|---|---|---|---|".into(),
        ]);
        for row in &rows {
            let mut requirement = row.requirement.replace('\n', " ");
            if requirement.chars().count() > 160 {
                requirement = requirement.chars().take(157).collect::<String>() + "...";
            }
            lines.push(format!(
                "| {} | {} | {} | {} |",
                row.requirement_id, requirement, row.modules, row.status
            ));
        }
        lines.push(String::new());
    }
    let body = if change.kind == "added" {
        change.after.as_ref().map(|s| s.body.trim()).unwrap_or("")
    } else {
        change.before.as_ref().map(|s| s.body.trim()).unwrap_or("")
    };
    match change.kind.as_str() {
        "added" => lines.extend([
            "**Åtgärd:**".into(), String::new(),
            format!("- [ ] Läs sektionen i den genererade speglingen och avgör om den inför ett nytt krav."),
            format!("- [ ] Lägg till rad(er) för `{}` i [kravsparning.md](kravsparning.md).", change.code),
            "- [ ] Bestäm modul enligt AFC.22 (modulnamnet ska hänvisa till TB-koden).".into(),
            "- [ ] Implementera och testa, eller dokumentera varför kravet inte implementeras nu.".into(),
            String::new(), "**Sektionens innehåll:**".into(), String::new(), "```".into(),
            if body.is_empty() { "(tom sektion)".into() } else { body.into() }, "```".into()
        ]),
        "removed" => lines.extend([
            "**Åtgärd:**".into(), String::new(),
            format!("- [ ] Bekräfta med beställaren att `{}` verkligen ska utgå (AFC.27).", change.code),
            "- [ ] Avgör om koden som uppfyllde kravet ska tas bort eller behållas.".into(),
            format!("- [ ] Ta bort eller markera raderna för `{}` i [kravsparning.md](kravsparning.md).", change.code),
            String::new(), "**Borttaget innehåll:**".into(), String::new(), "```".into(),
            if body.is_empty() { "(tom sektion)".into() } else { body.into() }, "```".into()
        ]),
        "modified" => lines.extend([
            "**Åtgärd:**".into(), String::new(),
            "- [ ] Granska diffen nedan och avgör om den ändrar ett krav eller bara formuleringen.".into(),
            "- [ ] Om kravet ändrats: uppdatera berörda moduler och deras tester.".into(),
            format!("- [ ] Uppdatera raderna för `{}` i [kravsparning.md](kravsparning.md).", change.code),
            "- [ ] Om den nya texten motsäger befintligt beteende: rapportera det enligt AFC.27".into(),
            "      i stället för att tyst välja en tolkning.".into(), String::new(), "**Diff:**".into(), String::new(),
            "```diff".into(), if unified_diff(change).is_empty() { "(endast rubriken ändrad)".into() } else { unified_diff(change) }, "```".into()
        ]),
        _ => lines.extend(["Innehållet är oförändrat, bara placeringen i dokumentet har ändrats.".into(), "Ingen kodändring krävs.".into()]),
    }
    lines.push(String::new());
    lines
}

pub fn render_action_plan(
    diff: &TbDiff,
    requirements: &Path,
    odt_name: &str,
) -> Result<String, TbError> {
    let index = load_requirement_index(requirements)?;
    let mut lines = vec![
        "<!-- GENERERAD FIL - skrivs om vid varje TB-andring. -->".into(),
        String::new(),
        "# Åtgärdsplan efter ändring i TB".into(),
        String::new(),
        format!("Primärkälla: `{odt_name}`."),
        "Speglingen finns i [TB-Datasamordningsassistenten.md](TB-Datasamordningsassistenten.md)."
            .into(),
        String::new(),
        format!("**Sammanfattning:** {}", diff.summary()),
        String::new(),
    ];
    if !diff.duplicate_codes.is_empty() {
        lines.extend([
            "## Avvikelse i handlingen (AFC.27)".into(),
            String::new(),
            "Följande TB-koder förekommer mer än en gång i handlingen, vilket gör dem".into(),
            "tvetydiga att hänvisa till. Endast den första förekomsten av varje kod".into(),
            "spåras. Detta bör rättas i TB:".into(),
            String::new(),
        ]);
        lines.extend(
            diff.duplicate_codes
                .iter()
                .map(|code| format!("- `{code}`")),
        );
        lines.push(String::new());
    }
    if diff.changes.is_empty() {
        lines.extend([
            "## Inga ändringar".into(),
            String::new(),
            "Handlingens innehåll är oförändrat sedan förra synkroniseringen.".into(),
            String::new(),
        ]);
        return Ok(lines.join("\n"));
    }
    lines.extend(["## Ändringar att åtgärda".into(), String::new()]);
    for change in &diff.changes {
        lines.extend(render_change(change, &index));
    }
    Ok(format!("{}\n", lines.join("\n").trim_end()))
}

#[derive(Debug)]
pub struct SyncResult {
    pub changed: bool,
    pub diff: TbDiff,
    pub markdown_path: PathBuf,
    pub action_plan: Option<String>,
    pub markdown_was_missing: bool,
    pub markdown_was_edited: bool,
    pub plan_was_written: bool,
}

pub fn parse_previous(markdown: &str) -> TbDocument {
    let mut doc = TbDocument::default();
    let mut current: Option<Section> = None;
    let mut body = Vec::new();
    let mut preamble: Vec<String> = Vec::new();
    let flush = |doc: &mut TbDocument, current: &mut Option<Section>, body: &mut Vec<String>| {
        if let Some(mut section) = current.take() {
            section.body = body.join("\n").trim().to_string();
            doc.sections.push(section);
        }
        body.clear();
    };
    for line in markdown.lines() {
        if line.starts_with("<!--") {
            continue;
        }
        if line.starts_with('#') {
            let level = line.chars().take_while(|c| *c == '#').count();
            let heading = line[level..].trim();
            if level == 1 {
                continue;
            }
            flush(&mut doc, &mut current, &mut body);
            let (code, title) = heading
                .split_once(" – ")
                .map_or((heading, ""), |(a, b)| (a.trim(), b.trim()));
            current = Some(Section {
                code: code.into(),
                title: title.into(),
                level,
                body: String::new(),
            });
        } else if current.is_some() {
            body.push(line.into());
        } else {
            preamble.push(line.into());
        }
    }
    flush(&mut doc, &mut current, &mut body);
    doc.preamble = preamble.join("\n").trim().to_string();
    doc
}

pub fn sync(
    odt: &Path,
    markdown: &Path,
    plan: &Path,
    requirements: &Path,
    state: &Path,
    write: bool,
) -> Result<SyncResult, TbError> {
    let document = parse_odt(odt)?;
    let generated = render_markdown(&document, "TB - Datasamordningsassistenten");
    let missing = !markdown.is_file();
    let previous = if missing {
        String::new()
    } else {
        fs::read_to_string(markdown)
            .map_err(|e| TbError(format!("Kunde inte läsa {}: {e}", markdown.display())))?
            .replace("\r\n", "\n")
    };
    let markdown_was_edited = !missing
        && stored_markdown_fingerprint(state)
            .is_some_and(|stored| stored != markdown_fingerprint(&previous));
    let changed = previous != generated;
    let diff = diff_documents(&parse_previous(&previous), &document);
    let action_plan = if changed {
        Some(render_action_plan(
            &diff,
            requirements,
            odt.file_name().and_then(|s| s.to_str()).unwrap_or("TB.odt"),
        )?)
    } else {
        None
    };
    if write && changed {
        write_parent_file(markdown, generated.as_bytes())?;
        if let Some(content) = &action_plan {
            write_parent_file(plan, content.as_bytes())?;
        }
    }
    if write {
        let state_text = format!(
            "{{\n  \"markdown_fingerprint\": \"{}\",\n  \"section_count\": {}\n}}\n",
            markdown_fingerprint(&generated),
            document.sections.len(),
        );
        write_parent_file(state, state_text.as_bytes())?;
    }
    Ok(SyncResult {
        changed,
        diff,
        markdown_path: markdown.to_path_buf(),
        action_plan,
        markdown_was_missing: missing,
        markdown_was_edited,
        plan_was_written: write && changed,
    })
}

fn markdown_fingerprint(text: &str) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for byte in text.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn stored_markdown_fingerprint(state_path: &Path) -> Option<String> {
    let state = fs::read_to_string(state_path).ok()?;
    let (_, value) = state.split_once("\"markdown_fingerprint\"")?;
    let (_, value) = value.split_once(':')?;
    let (_, value) = value.trim_start().split_once('"')?;
    Some(value.split_once('"')?.0.to_string())
}

fn write_parent_file(path: &Path, bytes: &[u8]) -> Result<(), TbError> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| TbError(format!("Kunde inte skapa {}: {e}", parent.display())))?;
    }
    fs::write(path, bytes)
        .map_err(|e| TbError(format!("Kunde inte skriva {}: {e}", path.display())))
}

pub fn project_paths() -> (PathBuf, PathBuf, PathBuf, PathBuf, PathBuf) {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap_or(Path::new("."));
    (
        root.join("TB - Datasamordningsassistenten.odt"),
        root.join("docs/TB-Datasamordningsassistenten.md"),
        root.join("docs/TB-atgardsplan.md"),
        root.join("docs/kravsparning.md"),
        root.join("docs/.tb-sync-state.json"),
    )
}

pub fn file_signature(path: &Path) -> Option<(u64, SystemTime)> {
    let metadata = fs::metadata(path).ok()?;
    Some((metadata.len(), metadata.modified().ok()?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    fn section(code: &str, title: &str, body: &str) -> Section {
        Section {
            code: code.into(),
            title: title.into(),
            level: heading_level(code),
            body: body.into(),
        }
    }

    fn temporary_directory() -> PathBuf {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!("tb-sync-test-{}-{id}", process_id()));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn process_id() -> u32 {
        std::process::id()
    }

    fn make_odt(xml: &str) -> Vec<u8> {
        use std::io::Write;
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        archive
            .start_file("content.xml", zip::write::SimpleFileOptions::default())
            .unwrap();
        archive.write_all(xml.as_bytes()).unwrap();
        archive.finish().unwrap().into_inner()
    }

    #[test]
    fn parses_representative_heading_forms_and_rejects_prose() {
        assert_eq!(
            parse_heading("BBFörarbeten"),
            Some(("BB".into(), "Förarbeten".into()))
        );
        assert_eq!(
            parse_heading("AFC.22 – Kvalitets och miljökrav"),
            Some(("AFC.22".into(), "Kvalitets och miljökrav".into()))
        );
        assert_eq!(
            parse_heading("A - Administrativa föreskrifter"),
            Some(("A".into(), "Administrativa föreskrifter".into()))
        );
        assert_eq!(
            parse_heading("DWG-data hämtas från blocket TRVJ_NAMNRUTA"),
            None
        );
        assert_eq!(heading_level("AFC.22"), 5);
    }

    #[test]
    fn moved_sections_alone_are_not_content_changes() {
        let before = TbDocument {
            preamble: String::new(),
            sections: vec![section("A", "Alpha", "same"), section("B", "Beta", "same")],
        };
        let after = TbDocument {
            preamble: String::new(),
            sections: vec![section("B", "Beta", "same"), section("A", "Alpha", "same")],
        };
        let diff = diff_documents(&before, &after);
        assert!(!diff.has_changes());
        assert_eq!(diff.by_kind("moved").len(), 2);
    }

    #[test]
    fn detects_moved_only_duplicates_and_modified_sections() {
        let before = TbDocument {
            preamble: String::new(),
            sections: vec![
                section("A", "Alpha", "unchanged"),
                section("B", "Beta", "old"),
            ],
        };
        let after = TbDocument {
            preamble: String::new(),
            sections: vec![
                section("B", "Beta", "new"),
                section("A", "Alpha", "unchanged"),
                section("A", "Duplicate", ""),
            ],
        };
        let diff = diff_documents(&before, &after);
        assert_eq!(diff.duplicate_codes, vec!["A"]);
        assert!(diff
            .changes
            .iter()
            .any(|c| c.code == "A" && c.kind == "moved"));
        assert!(diff
            .changes
            .iter()
            .any(|c| c.code == "B" && c.kind == "modified"));
        assert!(diff.has_changes());
    }

    #[test]
    fn renders_deterministic_markdown_and_unified_diff() {
        let doc = TbDocument {
            preamble: "Intro".into(),
            sections: vec![section("A", "Alpha", "before\nstay")],
        };
        assert!(
            render_markdown(&doc, "TB").contains("# TB\n\nIntro\n\n## A – Alpha\n\nbefore\nstay")
        );
        let c = SectionChange {
            code: "A".into(),
            kind: "modified".into(),
            title: "Alpha".into(),
            before: Some(section("A", "Alpha", "first\nold\nlast")),
            after: Some(section("A", "Alpha", "first\nnew\nlast")),
        };
        assert_eq!(
            unified_diff(&c),
            "--- A (tidigare)\n+++ A (ny)\n@@ -1,3 +1,3 @@\n first\n-old\n+new\n last"
        );
    }

    #[test]
    fn parses_xml_text_spaces_lists_tables_and_section_content() {
        let xml = r#"<office:document-content xmlns:office="o" xmlns:text="t" xmlns:table="tb"><office:body><office:text><text:p>Intro</text:p><text:h>A – Alpha</text:h><text:p>Hello<text:s text:c="2"/>world</text:p><text:list><text:list-item><text:p>item</text:p></text:list-item></text:list><table:table><table:table-row><table:table-cell><text:p>Key</text:p></table:table-cell><table:table-cell><text:p>Value</text:p></table:table-cell></table:table-row></table:table></office:text></office:body></office:document-content>"#;
        let mut archive = zip::ZipWriter::new(Cursor::new(Vec::new()));
        archive
            .start_file("content.xml", zip::write::SimpleFileOptions::default())
            .unwrap();
        use std::io::Write;
        archive.write_all(xml.as_bytes()).unwrap();
        let bytes = archive.finish().unwrap().into_inner();
        let doc = parse_odt_bytes(&bytes, "sample.odt").unwrap();
        assert_eq!(doc.preamble, "Intro");
        assert_eq!(
            doc.sections[0].body,
            "Hello world\n\n- item\n\n| Key | Value |\n|---|---|"
        );
    }

    #[test]
    fn renders_real_tb_identically_to_committed_mirror() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let odt = parse_odt(&root.join("TB - Datasamordningsassistenten.odt")).unwrap();
        let generated = render_markdown(&odt, "TB - Datasamordningsassistenten");
        let previous = fs::read_to_string(root.join("docs/TB-Datasamordningsassistenten.md"))
            .unwrap()
            .replace("\r\n", "\n");
        if generated != previous {
            let mismatch = generated
                .bytes()
                .zip(previous.bytes())
                .position(|(a, b)| a != b)
                .unwrap_or(generated.len().min(previous.len()));
            let start = mismatch.saturating_sub(80);
            let end_generated = (mismatch + 160).min(generated.len());
            let end_previous = (mismatch + 160).min(previous.len());
            let generated_bytes = generated.as_bytes();
            let previous_bytes = previous.as_bytes();
            panic!(
                "Rust-spegel avviker från befintlig Markdown vid byte {mismatch} (genererad längd {}, befintlig längd {}).\nGenererad: {:?}\nBefintlig: {:?}",
                generated.len(),
                previous.len(),
                String::from_utf8_lossy(&generated_bytes[start..end_generated]),
                String::from_utf8_lossy(&previous_bytes[start..end_previous]),
            );
        }
    }

    #[test]
    fn check_mode_returns_plan_without_writing_files() {
        let directory = temporary_directory();
        let odt = directory.join("TB test.odt");
        let markdown = directory.join("TB.md");
        let plan = directory.join("plan.md");
        let requirements = directory.join("kravsparning.md");
        let state = directory.join("state.json");
        let xml = r#"<office:document-content xmlns:office="o" xmlns:text="t"><office:body><office:text><text:h>CBE – Backend</text:h><text:p>new wording</text:p></office:text></office:body></office:document-content>"#;
        fs::write(&odt, make_odt(xml)).unwrap();
        let previous = TbDocument {
            preamble: String::new(),
            sections: vec![section("CBE", "Backend", "old wording")],
        };
        fs::write(
            &markdown,
            render_markdown(&previous, "TB - Datasamordningsassistenten"),
        )
        .unwrap();
        fs::write(
            &requirements,
            "| Krav-ID | TB-sektion | Krav | Ursprungsfunktion | Modul | Status |\n|---|---|---|---|---|---|\n| CBE-1 | CBE | Backend-krav | legacy | `tb_sync_rust` | Uppfyllt |\n",
        ).unwrap();
        let original_markdown = fs::read_to_string(&markdown).unwrap();

        let result = sync(&odt, &markdown, &plan, &requirements, &state, false).unwrap();

        assert!(result.changed);
        assert!(!result.plan_was_written);
        assert!(result
            .action_plan
            .as_ref()
            .unwrap()
            .contains("tb_sync_rust"));
        assert_eq!(fs::read_to_string(&markdown).unwrap(), original_markdown);
        assert!(!plan.exists());
        assert!(!state.exists());
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn sync_writes_updated_mirror_and_requirement_linked_plan() {
        let directory = temporary_directory();
        let odt = directory.join("TB test.odt");
        let markdown = directory.join("TB.md");
        let plan = directory.join("plan.md");
        let requirements = directory.join("kravsparning.md");
        let state = directory.join("state.json");
        let xml = r#"<office:document-content xmlns:office="o" xmlns:text="t"><office:body><office:text><text:h>CBE – Backend</text:h><text:p>new wording</text:p></office:text></office:body></office:document-content>"#;
        fs::write(&odt, make_odt(xml)).unwrap();
        let previous = TbDocument {
            preamble: String::new(),
            sections: vec![section("CBE", "Backend", "old wording")],
        };
        fs::write(
            &markdown,
            render_markdown(&previous, "TB - Datasamordningsassistenten"),
        )
        .unwrap();
        fs::write(
            &requirements,
            "| Krav-ID | TB-sektion | Krav | Ursprungsfunktion | Modul | Status |\n|---|---|---|---|---|---|\n| CBE-1 | CBE | Backend-krav | legacy | `tb_sync_rust` | Uppfyllt |\n",
        ).unwrap();

        let result = sync(&odt, &markdown, &plan, &requirements, &state, true).unwrap();

        assert!(result.changed);
        assert!(result.plan_was_written);
        assert!(fs::read_to_string(&markdown)
            .unwrap()
            .contains("new wording"));
        let action_plan = fs::read_to_string(&plan).unwrap();
        assert!(action_plan.contains("`tb_sync_rust`"));
        assert!(action_plan.contains("-old wording"));
        assert!(action_plan.contains("+new wording"));
        assert!(fs::read_to_string(&state)
            .unwrap()
            .contains("\"markdown_fingerprint\""));
        fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn sync_warns_and_repairs_a_hand_edited_mirror() {
        let directory = temporary_directory();
        let odt = directory.join("TB test.odt");
        let markdown = directory.join("TB.md");
        let plan = directory.join("plan.md");
        let requirements = directory.join("kravsparning.md");
        let state = directory.join("state.json");
        let xml = r#"<office:document-content xmlns:office="o" xmlns:text="t"><office:body><office:text><text:h>CBE – Backend</text:h><text:p>canonical wording</text:p></office:text></office:body></office:document-content>"#;
        fs::write(&odt, make_odt(xml)).unwrap();
        let canonical = render_markdown(
            &TbDocument {
                preamble: String::new(),
                sections: vec![section("CBE", "Backend", "canonical wording")],
            },
            "TB - Datasamordningsassistenten",
        );
        fs::write(&markdown, &canonical).unwrap();
        fs::write(
            &state,
            format!(
                "{{\"markdown_fingerprint\":\"{}\"}}\n",
                markdown_fingerprint(&canonical)
            ),
        )
        .unwrap();
        fs::write(
            &markdown,
            canonical.replace("canonical wording", "hand edited"),
        )
        .unwrap();

        let result = sync(&odt, &markdown, &plan, &requirements, &state, true).unwrap();

        assert!(result.markdown_was_edited);
        assert_eq!(fs::read_to_string(&markdown).unwrap(), canonical);
        fs::remove_dir_all(directory).unwrap();
    }
}
