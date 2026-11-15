"""Build the final project report (.docx) from docs/final_report.md on the official template.

Where it fits: the report text lives in docs/final_report.md (headings follow the course
template). This script opens the instructor's template, fills the cover and declaration,
removes the template's instruction text and writes the markdown content using the template's
own styles (Heading 1/2/3, List Paragraph, Normal). It also builds the TOC, the list of
figures and the list of tables, numbers the references and writes docs/Project_Report.docx.

Run (the template is not in the repository, so its path is an argument):
    python docs/build_report_docx.py D:/ADS/ADS_Project_Report_Template.docx
Options:
    --markdown PATH   markdown source (default: docs/final_report.md)
    --output PATH     output file (default: docs/Project_Report.docx)
    --update-fields   afterwards let Microsoft Word (Windows only) refresh the TOC and the
                      lists, so the page numbers are filled in, and save the file
    --pdf PATH        also export a PDF through Word (Windows only) to check the page count
    --no-highlight    do not highlight [PENDING ...] markers in yellow

Markdown subset understood (everything else is plain paragraph text):
    # / ## / ###        chapter title (Heading 1), section (Heading 2), sub-section (Heading 3)
    paragraphs          lines separated by blank lines
    - item / 1. item    bullet list and numbered list (one line per item)
    | a | b |           pipe tables (second line is the |---|---:| separator)
    ![alt](path)        picture, path relative to the markdown file
    Figure 3.1: text    figure caption (put it after the picture)
    Table 3.1: text     table caption (put it before the table)
    **bold** *italic* `code`
    [@key]              citation, numbered [1], [2] ... in order of first use
    @key: text          reference entry, written under "# REFERENCES"
    [[TOC]]  [[LIST_OF_FIGURES]]  [[LIST_OF_TABLES]]   generated lists
    ``` ... ```         code block
    ---  front matter at the top: title: and student: Name | Enrolment lines
    <!-- ... -->        comment, ignored
"""

import argparse
import copy
import os
import re
import subprocess
import sys
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Emu, Inches, Pt
from docx.text.paragraph import Paragraph

DOCS_FOLDER = Path(__file__).resolve().parent
DEFAULT_MARKDOWN = DOCS_FOLDER / "final_report.md"
DEFAULT_OUTPUT = DOCS_FOLDER / "Project_Report.docx"

ACKNOWLEDGEMENT_TITLE = "ACKNOWLEDGEMENT"
REFERENCES_TITLE = "REFERENCES"
END_OF_REPORT_TEXT = "END OF REPORT"
TEXT_WIDTH_TWIPS = 9026  # A4 page minus 1 inch margins, as in the template
MAX_PICTURE_WIDTH_INCHES = 6.2
MAX_PICTURE_HEIGHT_INCHES = 5.6
BODY_LINE_SPACING = 1.5
BODY_SPACE_AFTER_POINTS = 6
TABLE_FONT_POINTS = 10
WIDE_TABLE_FONT_POINTS = 9
WIDE_TABLE_COLUMNS = 5
MAX_COLUMN_WEIGHT = 60
CHARACTER_TWIPS = 100  # rough width of one character at 9 to 10 points
CELL_PADDING_TWIPS = 180
MAX_MINIMUM_WIDTH_TWIPS = 2000
GREY_HEADER_FILL = "D9D9D9"
CODE_FONT = "Courier New"
FIGURE_CAPTION_STYLE = "Figure Caption"
TABLE_CAPTION_STYLE = "Table Caption"

COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
CITATION_PATTERN = re.compile(r"\[(@[A-Za-z0-9_]+(?:\s*[;,]\s*@[A-Za-z0-9_]+)*)\]")
REFERENCE_PATTERN = re.compile(r"^@([A-Za-z0-9_]+):\s*(.+)$")
HEADING_PATTERN = re.compile(r"^(#{1,3})\s+(.*)$")
DIRECTIVE_PATTERN = re.compile(r"^\[\[([A-Z_]+)\]\]$")
IMAGE_PATTERN = re.compile(r"^!\[(.*?)\]\((.*?)\)\s*$")
CAPTION_PATTERN = re.compile(r"^(Figure|Table)\s+([A-Z]|\d+)\.(\d+):\s+(.+)$")
BULLET_PATTERN = re.compile(r"^\s*[-*]\s+(.*)$")
NUMBERED_PATTERN = re.compile(r"^\s*\d+\.\s+(.*)$")
INLINE_PATTERN = re.compile(
    r"(\*\*.+?\*\*|`[^`]+`|(?<![\w*])\*(?![\s*])[^*]+?(?<![\s*])\*(?![\w*]))"
)
PENDING_PATTERN = re.compile(r"(\[PENDING[^\]]*\])")
CHAPTER_PATTERN = re.compile(r"^CHAPTER\s+(\d+)\b")
APPENDIX_PATTERN = re.compile(r"^Appendix\s+([A-Z])\b")
TEMPLATE_LEFTOVERS = ("<Project Title>", "<Name", "<Enrollment", "[Provide", "[Express")

WORD_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$word = New-Object -ComObject Word.Application
$word.Visible = $false
$word.DisplayAlerts = 0
try {
  $doc = $word.Documents.Open($env:REPORT_DOCX)
  $doc.Repaginate()
  $doc.Fields.Update() | Out-Null
  foreach ($toc in $doc.TablesOfContents) { $toc.Update() }
  $doc.Fields.Update() | Out-Null
  foreach ($toc in $doc.TablesOfContents) { $toc.Update() }
  if ($env:REPORT_SAVE -eq "1") { $doc.Save() }
  if ($env:REPORT_PDF) { $doc.ExportAsFixedFormat($env:REPORT_PDF, 17) }
  Write-Output ("PAGES=" + $doc.ComputeStatistics(2))
  $doc.Close(0)
} finally { $word.Quit() }
"""


# ---------------------------------------------------------------------------
# Step 1: read the markdown into simple blocks
# ---------------------------------------------------------------------------


def read_front_matter(lines):
    """Split the optional '---' front matter from the body.

    Returns (meta, body_lines). meta has 'title' and a list 'students' of
    (name, enrolment) pairs.
    """
    meta = {"title": "", "students": []}
    if not lines or lines[0].strip() != "---":
        return meta, lines
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("Front matter starts with '---' but is never closed.")
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "title":
            meta["title"] = value
        elif key == "student":
            name, _, enrolment = value.partition("|")
            meta["students"].append((name.strip(), enrolment.strip()))
    return meta, lines[end + 1 :]


def number_citations(lines):
    """Replace [@key] citations by IEEE numbers in order of first use.

    The reference entries ('@key: text' lines under the REFERENCES heading) are removed
    from the text and returned. Returns (new_lines, numbered_references) where
    numbered_references is a list of (number, text) sorted by number.
    """
    entries = {}
    body = []
    in_references = False
    for line in lines:
        heading = HEADING_PATTERN.match(line)
        if heading and heading.group(1) == "#":
            in_references = heading.group(2).strip() == REFERENCES_TITLE
        reference = REFERENCE_PATTERN.match(line) if in_references else None
        if reference:
            entries[reference.group(1)] = reference.group(2).strip()
        else:
            body.append(line)
    numbers = {}

    def replace(match):
        keys = [part.strip().lstrip("@") for part in re.split(r"[;,]", match.group(1))]
        for key in keys:
            if key not in entries:
                raise ValueError(f"Citation [@{key}] has no reference entry '@{key}: ...'.")
            numbers.setdefault(key, len(numbers) + 1)
        return ", ".join(f"[{numbers[key]}]" for key in keys)

    body = [CITATION_PATTERN.sub(replace, line) for line in body]
    unused = sorted(set(entries) - set(numbers))
    if unused:
        print(f"WARNING: references never cited: {', '.join(unused)}", file=sys.stderr)
    numbered = sorted((number, entries[key]) for key, number in numbers.items())
    return body, numbered


def split_table_row(line):
    """Return the cell texts of one '| a | b |' line."""
    text = line.strip()
    text = text.removeprefix("|")
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", text)]


def read_alignments(separator_cells):
    """Return 'left', 'right' or 'center' for each cell of a |---:| separator row."""
    result = []
    for cell in separator_cells:
        if cell.startswith(":") and cell.endswith(":") and len(cell) > 1:
            result.append("center")
        elif cell.endswith(":"):
            result.append("right")
        else:
            result.append("left")
    return result


def is_separator_row(cells):
    """True if every cell looks like ---, :--, --: or :-:."""
    return all(re.fullmatch(r":?-{1,}:?", cell) for cell in cells)


def parse_blocks(lines):
    """Turn markdown lines into a list of block dictionaries, in document order."""
    blocks = []
    paragraph_lines = []
    index = 0

    def flush_paragraph():
        if paragraph_lines:
            blocks.append({"type": "paragraph", "text": " ".join(paragraph_lines)})
            paragraph_lines.clear()

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            code = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index].rstrip())
                index += 1
            blocks.append({"type": "code", "lines": code})
        elif not stripped:
            flush_paragraph()
        elif HEADING_PATTERN.match(stripped):
            flush_paragraph()
            match = HEADING_PATTERN.match(stripped)
            blocks.append({"type": "heading", "level": len(match.group(1)), "text": match.group(2).strip()})
        elif DIRECTIVE_PATTERN.match(stripped):
            flush_paragraph()
            blocks.append({"type": "directive", "name": DIRECTIVE_PATTERN.match(stripped).group(1)})
        elif IMAGE_PATTERN.match(stripped):
            flush_paragraph()
            match = IMAGE_PATTERN.match(stripped)
            blocks.append({"type": "image", "alt": match.group(1), "path": match.group(2)})
        elif CAPTION_PATTERN.match(stripped):
            flush_paragraph()
            match = CAPTION_PATTERN.match(stripped)
            blocks.append({
                "type": "caption",
                "kind": match.group(1),
                "label": f"{match.group(2)}.{match.group(3)}",
                "scope": match.group(2),
                "number": int(match.group(3)),
                "text": match.group(4).strip(),
            })
        elif stripped.startswith("|"):
            flush_paragraph()
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(split_table_row(lines[index]))
                index += 1
            index -= 1
            if len(rows) < 2 or not is_separator_row(rows[1]):
                raise ValueError(f"Table without a |---| separator row near: {stripped[:60]}")
            blocks.append({"type": "table", "header": rows[0], "aligns": read_alignments(rows[1]), "rows": rows[2:]})
        elif BULLET_PATTERN.match(line):
            flush_paragraph()
            items = []
            while index < len(lines) and BULLET_PATTERN.match(lines[index]):
                items.append(BULLET_PATTERN.match(lines[index]).group(1).strip())
                index += 1
            index -= 1
            blocks.append({"type": "bullets", "items": items})
        elif NUMBERED_PATTERN.match(line):
            flush_paragraph()
            items = []
            while index < len(lines) and NUMBERED_PATTERN.match(lines[index]):
                items.append(NUMBERED_PATTERN.match(lines[index]).group(1).strip())
                index += 1
            index -= 1
            blocks.append({"type": "numbered", "items": items})
        else:
            paragraph_lines.append(stripped)
        index += 1
    flush_paragraph()
    return blocks


def read_markdown(path):
    """Read the markdown file; return (meta, blocks, numbered_references)."""
    text = COMMENT_PATTERN.sub("", Path(path).read_text(encoding="utf-8"))
    meta, lines = read_front_matter(text.splitlines())
    lines, references = number_citations(lines)
    return meta, parse_blocks(lines), references


def check_caption_numbers(blocks):
    """Return warnings for figure/table captions whose number does not follow the chapter.

    Captions must read 'Figure <chapter>.<n>' or 'Table <chapter>.<n>' (appendix letters
    work the same way), counting up from 1 inside each chapter or appendix.
    """
    warnings = []
    scope = None
    counters = {}
    for block in blocks:
        if block["type"] == "heading":
            chapter = CHAPTER_PATTERN.match(block["text"]) if block["level"] == 1 else None
            appendix = APPENDIX_PATTERN.match(block["text"]) if block["level"] == 2 else None
            if chapter:
                scope = chapter.group(1)
            elif appendix:
                scope = appendix.group(1)
        elif block["type"] == "caption":
            key = (block["kind"], scope)
            counters[key] = counters.get(key, 0) + 1
            expected = f"{scope}.{counters[key]}"
            if block["label"] != expected:
                warnings.append(f"{block['kind']} {block['label']} should be {block['kind']} {expected}")
    return warnings


# ---------------------------------------------------------------------------
# Step 2: helpers that write into the Word document
# ---------------------------------------------------------------------------


def get_style(document, name, fallback="Normal"):
    """Return the named style, or the fallback style if the template lacks it."""
    try:
        return document.styles[name]
    except KeyError:
        return document.styles[fallback]


def ensure_caption_styles(document):
    """Add 'Figure Caption' and 'Table Caption' paragraph styles if missing."""
    for name in (FIGURE_CAPTION_STYLE, TABLE_CAPTION_STYLE):
        try:
            document.styles[name]
            continue
        except KeyError:
            pass
        style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = document.styles["Normal"]
        style.font.size = Pt(10.5)
        style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        style.paragraph_format.space_before = Pt(4)
        style.paragraph_format.space_after = Pt(10)
        style.paragraph_format.keep_together = True
        if name == TABLE_CAPTION_STYLE:
            style.paragraph_format.space_before = Pt(10)
            style.paragraph_format.space_after = Pt(4)
            style.paragraph_format.keep_with_next = True


def add_text_run(paragraph, text, bold=False, italic=False, size=None, code=False, highlight=False):
    """Add one run with the given formatting."""
    run = paragraph.add_run(text)
    run.bold = bold or None
    run.italic = italic or None
    if size:
        run.font.size = Pt(size)
    if code:
        run.font.name = CODE_FONT
        run._element.rPr.rFonts.set(qn("w:cs"), CODE_FONT)
        run._element.rPr.rFonts.set(qn("w:eastAsia"), CODE_FONT)
        run.font.size = Pt((size or 12) - 1.5)
    if highlight:
        run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    return run


def add_inline(paragraph, text, size=None, bold=False, italic=False, highlight=True):
    """Write text with **bold**, *italic*, `code` and highlighted [PENDING ...] markers."""
    position = 0
    for match in INLINE_PATTERN.finditer(text):
        add_plain(paragraph, text[position : match.start()], size, bold, italic, highlight)
        token = match.group(0)
        if token.startswith("**"):
            add_inline(paragraph, token[2:-2], size, True, italic, highlight)
        elif token.startswith("`"):
            add_text_run(paragraph, token[1:-1], bold, italic, size, code=True)
        else:
            add_inline(paragraph, token[1:-1], size, bold, True, highlight)
        position = match.end()
    add_plain(paragraph, text[position:], size, bold, italic, highlight)


def add_plain(paragraph, text, size, bold, italic, highlight):
    """Add plain text, splitting out [PENDING ...] markers so they can be highlighted."""
    for part in PENDING_PATTERN.split(text):
        if part:
            is_pending = highlight and part.startswith("[PENDING")
            add_text_run(paragraph, part, bold, italic, size, highlight=is_pending)


def strip_inline(text):
    """Return text without markdown inline marks (for lengths and list entries)."""
    return INLINE_PATTERN.sub(lambda m: m.group(0).strip("*`"), text)


def set_body_format(paragraph, justify=True):
    """Give a paragraph the template's body look: 1.5 line spacing, justified."""
    paragraph.paragraph_format.line_spacing = BODY_LINE_SPACING
    paragraph.paragraph_format.space_after = Pt(BODY_SPACE_AFTER_POINTS)
    if justify:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY


def add_numbering_definition(document, kind):
    """Create a bullet or decimal numbering definition; return a function giving new list ids.

    Each call of the returned function makes a new w:num that restarts at 1, so every
    numbered list in the report starts again at 1.
    """
    numbering = document.part.numbering_part.element
    abstract_ids = [int(a.get(qn("w:abstractNumId"))) for a in numbering.findall(qn("w:abstractNum"))]
    abstract_id = max(abstract_ids + [0]) + 1
    if kind == "bullet":
        level = '<w:numFmt w:val="bullet"/><w:lvlText w:val="\u2022"/>'
    else:
        level = '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>'
    abstract = parse_xml(
        f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{abstract_id}">'
        '<w:multiLevelType w:val="hybridMultilevel"/>'
        f'<w:lvl w:ilvl="0"><w:start w:val="1"/>{level}<w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum>'
    )
    first_num = numbering.find(qn("w:num"))
    if first_num is not None:
        first_num.addprevious(abstract)
    else:
        numbering.append(abstract)

    def new_list_id():
        existing = [int(n.get(qn("w:numId"))) for n in numbering.findall(qn("w:num"))]
        num_id = max(existing + [0]) + 1
        numbering.append(parse_xml(
            f'<w:num {nsdecls("w")} w:numId="{num_id}"><w:abstractNumId w:val="{abstract_id}"/>'
            '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
        ))
        return num_id

    return new_list_id


def set_list_numbering(paragraph, num_id):
    """Attach a paragraph to a numbering list."""
    properties = paragraph._p.get_or_add_pPr()
    num_pr = parse_xml(
        f'<w:numPr {nsdecls("w")}><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
    )
    properties.append(num_pr)


def add_list(document, items, new_list_id, highlight):
    """Write a bullet or numbered list using the template's 'List Paragraph' style."""
    num_id = new_list_id()
    for item in items:
        paragraph = document.add_paragraph(style=get_style(document, "List Paragraph"))
        set_list_numbering(paragraph, num_id)
        paragraph.paragraph_format.line_spacing = BODY_LINE_SPACING
        paragraph.paragraph_format.space_after = Pt(3)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        add_inline(paragraph, item, highlight=highlight)


def column_widths(header, rows):
    """Return column widths in twips for a table.

    Every column first gets enough room for its longest word (so short markers such as
    "[PENDING P-07]" do not break in the middle of a word); the remaining width is shared
    in proportion to how much text the column holds.
    """
    minimums, weights = [], []
    for column in range(len(header)):
        texts = [strip_inline(row[column]) if column < len(row) else "" for row in [header, *rows]]
        longest_word = max((len(word) for text in texts for word in text.split()), default=1)
        minimums.append(min(longest_word * CHARACTER_TWIPS + CELL_PADDING_TWIPS, MAX_MINIMUM_WIDTH_TWIPS))
        weights.append(min(max(len(text) for text in texts), MAX_COLUMN_WEIGHT))
    if sum(minimums) > TEXT_WIDTH_TWIPS:
        scale = TEXT_WIDTH_TWIPS / sum(minimums)
        return [int(width * scale) for width in minimums]
    spare = TEXT_WIDTH_TWIPS - sum(minimums)
    total_weight = sum(weights) or 1
    return [int(width + spare * weight / total_weight) for width, weight in zip(minimums, weights, strict=True)]


def style_table_borders(table):
    """Give the table single black borders and small cell margins, like the template's table."""
    properties = table._tbl.tblPr
    for old in properties.findall(qn("w:tblBorders")):
        properties.remove(old)
    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    new_elements = [
        parse_xml(f'<w:tblBorders {nsdecls("w")}>{borders}</w:tblBorders>'),
        parse_xml(
            f'<w:tblCellMar {nsdecls("w")}><w:top w:w="40" w:type="dxa"/><w:left w:w="80" w:type="dxa"/>'
            '<w:bottom w:w="40" w:type="dxa"/><w:right w:w="80" w:type="dxa"/></w:tblCellMar>'
        ),
    ]
    look = properties.find(qn("w:tblLook"))
    for element in new_elements:
        # Word expects tblBorders and tblCellMar before tblLook.
        if look is not None:
            look.addprevious(element)
        else:
            properties.append(element)


def add_table(document, block, highlight):
    """Write a pipe table as a bordered Word table with a grey, repeating header row."""
    header, rows, aligns = block["header"], block["rows"], block["aligns"]
    column_count = len(header)
    table = document.add_table(rows=1 + len(rows), cols=column_count)
    style_table_borders(table)
    widths = column_widths(header, rows)
    table.autofit = False
    grid = table._tbl.tblGrid
    for column, grid_column in enumerate(grid.findall(qn("w:gridCol"))):
        grid_column.set(qn("w:w"), str(widths[column]))
    font_size = WIDE_TABLE_FONT_POINTS if column_count >= WIDE_TABLE_COLUMNS else TABLE_FONT_POINTS
    alignment_map = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
    }
    for row_index, cells_text in enumerate([header, *rows]):
        row = table.rows[row_index]
        properties = row._tr.get_or_add_trPr()
        properties.append(OxmlElement("w:cantSplit"))
        if row_index == 0:
            properties.append(OxmlElement("w:tblHeader"))
        for column in range(column_count):
            cell = row.cells[column]
            cell.width = Emu(int(widths[column] * 635))
            text = cells_text[column] if column < len(cells_text) else ""
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.0
            if row_index > 0 or aligns[column] != "left":
                paragraph.alignment = alignment_map[aligns[column]]
            add_inline(paragraph, text, size=font_size, bold=(row_index == 0), highlight=highlight)
            if row_index == 0:
                shading = parse_xml(
                    f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{GREY_HEADER_FILL}"/>'
                )
                cell._tc.get_or_add_tcPr().append(shading)
    spacer = document.add_paragraph()
    spacer.paragraph_format.space_after = Pt(4)


def add_picture(document, block, markdown_folder):
    """Insert a centred picture, scaled to fit the page; return True if it was inserted."""
    path = (markdown_folder / block["path"]).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Picture not found: {path}")
    paragraph = document.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.keep_with_next = True
    paragraph.paragraph_format.space_before = Pt(6)
    shape = paragraph.add_run().add_picture(str(path))
    ratio = shape.height / shape.width
    width = min(Inches(MAX_PICTURE_WIDTH_INCHES), shape.width)
    if width * ratio > Inches(MAX_PICTURE_HEIGHT_INCHES):
        width = Inches(MAX_PICTURE_HEIGHT_INCHES) / ratio
    shape.width = Emu(int(width))
    shape.height = Emu(int(width * ratio))


def add_caption(document, block, highlight):
    """Write 'Figure 3.1: text' or 'Table 3.1: text' in the caption style."""
    style_name = FIGURE_CAPTION_STYLE if block["kind"] == "Figure" else TABLE_CAPTION_STYLE
    paragraph = document.add_paragraph(style=get_style(document, style_name))
    add_text_run(paragraph, f"{block['kind']} {block['label']}: ", bold=True)
    add_inline(paragraph, block["text"], highlight=highlight)


def add_code_block(document, lines):
    """Write a code block, one monospaced paragraph per line."""
    for line in lines:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Inches(0.3)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.line_spacing = 1.0
        add_text_run(paragraph, line if line else " ", size=10, code=True)
    document.add_paragraph().paragraph_format.space_after = Pt(4)


def field_run(kind):
    """Return a w:r element holding a field character ('begin', 'separate' or 'end')."""
    run = OxmlElement("w:r")
    char = OxmlElement("w:fldChar")
    char.set(qn("w:fldCharType"), kind)
    run.append(char)
    return run


def instruction_run(instruction):
    """Return a w:r element holding a field instruction such as TOC."""
    run = OxmlElement("w:r")
    text = OxmlElement("w:instrText")
    text.set(qn("xml:space"), "preserve")
    text.text = f" {instruction} "
    run.append(text)
    return run


def add_field_block(document, instruction, entries):
    """Insert a TOC-type field whose stored result is the given (text, style) entries.

    Word recalculates it (with page numbers) when the fields are updated; the stored
    entries make the lists readable even before that.
    """
    if not entries:
        entries = [("(none)", "Normal")]
    last = len(entries) - 1
    for index, (text, style_name) in enumerate(entries):
        paragraph = document.add_paragraph(style=get_style(document, style_name))
        if index == 0:
            paragraph._p.append(field_run("begin"))
            paragraph._p.append(instruction_run(instruction))
            paragraph._p.append(field_run("separate"))
        paragraph.add_run(text)
        if index == last:
            paragraph._p.append(field_run("end"))


def set_update_fields_on_open(document):
    """Ask Word to refresh all fields (TOC, lists, page numbers) when the file is opened."""
    settings = document.settings.element
    for old in settings.findall(qn("w:updateFields")):
        settings.remove(old)
    element = OxmlElement("w:updateFields")
    element.set(qn("w:val"), "true")
    anchor = settings.find(qn("w:footnotePr"))
    if anchor is None:
        anchor = settings.find(qn("w:compat"))
    if anchor is not None:
        anchor.addprevious(element)
    else:
        settings.append(element)


# ---------------------------------------------------------------------------
# Step 3: fill the template's cover and declaration, clear its instruction text
# ---------------------------------------------------------------------------


def replace_in_paragraph(paragraph, old, new):
    """Replace text inside a paragraph even when it is split over several runs.

    Keeps the formatting of the first run that holds part of the old text.
    Returns True if the text was found.
    """
    runs = paragraph.runs
    full = "".join(run.text for run in runs)
    start = full.find(old)
    if start < 0:
        return False
    end = start + len(old)
    position = 0
    placed = False
    for run in runs:
        run_start, run_end = position, position + len(run.text)
        position = run_end
        if run_end <= start or run_start >= end:
            continue
        before = run.text[: max(start - run_start, 0)]
        after = run.text[min(end - run_start, len(run.text)) :]
        run.text = before + (new if not placed else "") + after
        placed = True
    return True


def fill_cover_and_declaration(document, meta):
    """Fill title, students and declaration lines in the template; return what was filled."""
    filled = 0
    students = meta["students"]
    for paragraph in document.paragraphs:
        text = paragraph.text
        if "<Project Title>" in text:
            # In the declaration the title is split over runs ('"<' and 'Project Title>').
            filled += replace_in_paragraph(paragraph, "<Project Title>", meta["title"])
        for number, (name, enrolment) in enumerate(students, start=1):
            filled += replace_in_paragraph(paragraph, f"<Name of Student-{number}>", name)
            filled += replace_in_paragraph(paragraph, f"<Enrollment Number of Student-{number}>", enrolment)
            filled += replace_in_paragraph(
                paragraph,
                f"<Name & Signature of Student-{number}>",
                f"{name} ({enrolment})      Signature: ____________________",
            )
    if meta["title"]:
        document.core_properties.title = meta["title"]
    if students:
        document.core_properties.author = ", ".join(name for name, _ in students)
    return filled


def clear_template_body(document):
    """Remove everything from the ACKNOWLEDGEMENT heading to the end of the template.

    Returns the template's closing paragraphs ('END OF REPORT' and the plagiarism note)
    as copies, so they can be put back at the end of the new content. If the template has no
    ACKNOWLEDGEMENT heading, nothing is removed and an empty list is returned.
    """
    body = document.element.body
    anchor = next(
        (p for p in document.paragraphs if p.text.strip() == ACKNOWLEDGEMENT_TITLE and p.style.name.startswith("Heading")),
        None,
    )
    if anchor is None:
        return []
    siblings = [el for el in anchor._p.itersiblings() if el.tag != qn("w:sectPr")]
    closing = []
    paragraphs = [Paragraph(el, document) for el in siblings if el.tag == qn("w:p")]
    for position, paragraph in enumerate(paragraphs):
        if paragraph.text.strip() == END_OF_REPORT_TEXT:
            start = max(position - 1, 0)
            closing = [copy.deepcopy(p._p) for p in paragraphs[start:]]
            break
    for element in siblings:
        body.remove(element)
    body.remove(anchor._p)
    return closing


# ---------------------------------------------------------------------------
# Step 4: put everything together
# ---------------------------------------------------------------------------


def collect_lists(blocks):
    """Return TOC entries, figure entries and table entries as (text, style) pairs."""
    toc, figures, tables = [], [], []
    for block in blocks:
        if block["type"] == "heading" and block["level"] <= 2:
            toc.append((block["text"], "toc 1" if block["level"] == 1 else "toc 2"))
        elif block["type"] == "caption":
            entry = (f"{block['kind']} {block['label']}: {strip_inline(block['text'])}", "toc 1")
            (figures if block["kind"] == "Figure" else tables).append(entry)
    return toc, figures, tables


def write_blocks(document, blocks, references, markdown_folder, highlight):
    """Write all blocks into the document; return counts of what was written."""
    toc, figures, tables = collect_lists(blocks)
    new_bullets = add_numbering_definition(document, "bullet")
    new_numbers = add_numbering_definition(document, "decimal")
    counts = {"headings": 0, "tables": 0, "pictures": 0, "captions": 0}
    first_heading = True
    for block in blocks:
        kind = block["type"]
        if kind == "heading":
            level = block["level"]
            paragraph = document.add_paragraph(style=get_style(document, f"Heading {level}"))
            add_inline(paragraph, block["text"], highlight=False)
            if level == 1:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                paragraph.paragraph_format.page_break_before = not first_heading
                first_heading = False
            counts["headings"] += 1
            if block["text"] == REFERENCES_TITLE:
                write_references(document, references, highlight)
        elif kind == "paragraph":
            paragraph = document.add_paragraph()
            set_body_format(paragraph)
            add_inline(paragraph, block["text"], highlight=highlight)
        elif kind == "bullets":
            add_list(document, block["items"], new_bullets, highlight)
        elif kind == "numbered":
            add_list(document, block["items"], new_numbers, highlight)
        elif kind == "table":
            add_table(document, block, highlight)
            counts["tables"] += 1
        elif kind == "image":
            add_picture(document, block, markdown_folder)
            counts["pictures"] += 1
        elif kind == "caption":
            add_caption(document, block, highlight)
            counts["captions"] += 1
        elif kind == "code":
            add_code_block(document, block["lines"])
        elif kind == "directive":
            add_directive(document, block["name"], toc, figures, tables)
    return counts


def add_directive(document, name, toc, figures, tables):
    """Insert the generated TOC, list of figures or list of tables."""
    if name == "TOC":
        add_field_block(document, 'TOC \\o "1-2" \\h \\z \\u', toc)
    elif name == "LIST_OF_FIGURES":
        add_field_block(document, f'TOC \\h \\z \\t "{FIGURE_CAPTION_STYLE},1"', figures)
    elif name == "LIST_OF_TABLES":
        add_field_block(document, f'TOC \\h \\z \\t "{TABLE_CAPTION_STYLE},1"', tables)
    else:
        raise ValueError(f"Unknown directive [[{name}]]")


def write_references(document, references, highlight):
    """Write the numbered IEEE reference list with a hanging indent."""
    for number, text in references:
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Inches(0.45)
        paragraph.paragraph_format.first_line_indent = Inches(-0.45)
        paragraph.paragraph_format.space_after = Pt(6)
        paragraph.paragraph_format.line_spacing = 1.15
        add_text_run(paragraph, f"[{number}]\t", size=11)
        add_inline(paragraph, text, size=11, highlight=highlight)
        paragraph.paragraph_format.tab_stops.add_tab_stop(Inches(0.45))


def find_leftover_placeholders(document):
    """Return template instruction fragments that are still in the document text."""
    texts = [p.text for p in document.paragraphs]
    return [t[:60] for t in texts if any(token in t for token in TEMPLATE_LEFTOVERS)]


def build_report(template_path, markdown_path, output_path, highlight=True):
    """Build the report; return a dictionary with counts and any warnings."""
    template_path, markdown_path, output_path = Path(template_path), Path(markdown_path), Path(output_path)
    if not template_path.is_file():
        raise FileNotFoundError(f"Template not found: {template_path}")
    if template_path.resolve() == output_path.resolve():
        raise ValueError("The output file must not be the template itself.")
    meta, blocks, references = read_markdown(markdown_path)
    warnings = check_caption_numbers(blocks)
    document = Document(str(template_path))
    ensure_caption_styles(document)
    filled = fill_cover_and_declaration(document, meta)
    closing = clear_template_body(document)
    counts = write_blocks(document, blocks, references, markdown_path.parent, highlight)
    for element in closing:
        document.element.body.sectPr.addprevious(element)
    set_update_fields_on_open(document)
    leftovers = find_leftover_placeholders(document)
    if leftovers:
        warnings.append(f"template placeholder text still present: {leftovers}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output_path))
    counts.update({"cover_fields_filled": filled, "references": len(references), "warnings": warnings})
    return counts


def run_word(docx_path, save, pdf_path):
    """Let Microsoft Word refresh fields and optionally export a PDF; return the page count."""
    environment = dict(os.environ, REPORT_DOCX=str(Path(docx_path).resolve()), REPORT_SAVE="1" if save else "0",
                       REPORT_PDF=str(Path(pdf_path).resolve()) if pdf_path else "")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", WORD_SCRIPT],
        env=environment, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Word automation failed: {result.stderr.strip() or result.stdout.strip()}")
    match = re.search(r"PAGES=(\d+)", result.stdout)
    return int(match.group(1)) if match else None


def main(argv=None):
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Build the project report from markdown and the official template.")
    parser.add_argument("template", help="path to the official ADS_Project_Report_Template.docx")
    parser.add_argument("--markdown", default=str(DEFAULT_MARKDOWN), help="markdown source")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="output .docx")
    parser.add_argument("--update-fields", action="store_true", help="refresh TOC and lists with Word (Windows)")
    parser.add_argument("--pdf", default="", help="also export a PDF with Word (Windows)")
    parser.add_argument("--no-highlight", action="store_true", help="do not highlight [PENDING ...] markers")
    arguments = parser.parse_args(argv)
    counts = build_report(arguments.template, arguments.markdown, arguments.output, not arguments.no_highlight)
    for warning in counts["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    print(f"Wrote {arguments.output}")
    print({key: value for key, value in counts.items() if key != "warnings"})
    if arguments.update_fields or arguments.pdf:
        pages = run_word(arguments.output, arguments.update_fields, arguments.pdf)
        print(f"Word refreshed the fields; page count: {pages}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
