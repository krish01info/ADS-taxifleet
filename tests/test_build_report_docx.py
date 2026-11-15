"""Smoke tests for docs/build_report_docx.py (markdown to Word report generator).

Where it fits: the report is built from docs/final_report.md on the official template. These
tests use a tiny template made on the fly with python-docx, so they need neither the real
template nor Microsoft Word. Run with:  pytest --noconftest tests/test_build_report_docx.py
"""

import base64
import importlib.util
from pathlib import Path

import pytest
from docx import Document

GENERATOR_PATH = Path(__file__).resolve().parents[1] / "docs" / "build_report_docx.py"
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

MINI_REPORT = """---
title: A Tiny Test Report
student: Ada One | 111
student: Ben Two | 222
---

# ACKNOWLEDGEMENT

Thanks to everyone. See [@alpha] and [@beta; @alpha].

# LIST OF FIGURES

[[LIST_OF_FIGURES]]

# CHAPTER 1: INTRODUCTION

## 1.1 Background

A paragraph with **bold**, *italic* and `code`, and a marker [PENDING P-99: later].

- first bullet
- second bullet

1. first step
2. second step

Table 1.1: A small table

| Name | Value |
|---|---:|
| one | 1 |
| two | 2 |

![A tiny picture](tiny.png)

Figure 1.1: A tiny picture

# REFERENCES

@alpha: First source, 2026.
@beta: Second source, 2025.
"""


def load_generator():
    """Import docs/build_report_docx.py (docs/ is not a Python package)."""
    spec = importlib.util.spec_from_file_location("build_report_docx", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def workspace(tmp_path):
    """Make a minimal template, a markdown file and a picture in a temporary folder."""
    template = Document()
    template.add_paragraph("Cover page of the template")
    template.add_paragraph("<Project Title>")
    template.save(tmp_path / "template.docx")
    (tmp_path / "report.md").write_text(MINI_REPORT, encoding="utf-8")
    (tmp_path / "tiny.png").write_bytes(ONE_PIXEL_PNG)
    return tmp_path


def test_markdown_becomes_word_document(workspace):
    """Headings, list items, the table, the picture and the cover title all arrive."""
    generator = load_generator()
    output = workspace / "out.docx"
    counts = generator.build_report(workspace / "template.docx", workspace / "report.md", output)

    assert counts["warnings"] == []
    assert counts["tables"] == 1
    assert counts["pictures"] == 1
    document = Document(str(output))
    styles = [paragraph.style.name for paragraph in document.paragraphs]
    assert styles.count("Heading 1") == 4  # ACKNOWLEDGEMENT, LIST OF FIGURES, CHAPTER 1, REFERENCES
    assert styles.count("Heading 2") == 1
    assert styles.count("List Paragraph") == 4
    assert len(document.tables) == 1
    assert document.tables[0].rows[1].cells[1].text == "1"
    assert len(document.inline_shapes) == 1
    texts = [paragraph.text for paragraph in document.paragraphs]
    assert "A Tiny Test Report" in texts  # the template's <Project Title> was filled
    assert "Figure 1.1: A tiny picture" in texts
    assert any(text.startswith("Table 1.1: A small table") for text in texts)
    assert document.settings.element.xpath("//w:updateFields")


def test_citations_are_numbered_in_order_of_first_use(workspace):
    """[@alpha] becomes [1], [@beta; @alpha] becomes [2], [1], and the list follows."""
    generator = load_generator()
    output = workspace / "out.docx"
    generator.build_report(workspace / "template.docx", workspace / "report.md", output)

    texts = [paragraph.text for paragraph in Document(str(output)).paragraphs]
    assert any("See [1] and [2], [1]." in text for text in texts)
    assert "[1]\tFirst source, 2026." in texts
    assert "[2]\tSecond source, 2025." in texts


def test_template_is_never_overwritten_and_bad_caption_numbers_are_reported(workspace):
    """The output may not be the template; a wrongly numbered caption gives a warning."""
    generator = load_generator()
    with pytest.raises(ValueError):
        generator.build_report(workspace / "template.docx", workspace / "report.md", workspace / "template.docx")

    wrong = MINI_REPORT.replace("Figure 1.1:", "Figure 1.5:")
    (workspace / "wrong.md").write_text(wrong, encoding="utf-8")
    counts = generator.build_report(workspace / "template.docx", workspace / "wrong.md", workspace / "wrong.docx")
    assert any("Figure 1.5 should be Figure 1.1" in warning for warning in counts["warnings"])
