#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Tests for xlsx_insert_row.py / xlsx_add_column.py style-index parsing.

Regression tests for cells carrying an empty or non-numeric ``s`` attribute
(``s=""``, ``s="abc"``). The attribute is optional in the schema and foreign
writers do emit those shapes; both editors parsed it with
``int(c.get("s", "0"))``, so such a cell raised ``ValueError`` and the command
exited 1. For ``insert_row`` the crash landed *after* ``xlsx_shift_rows`` had
already rewritten the workbook, leaving a half-applied edit that a retry would
double-shift.

An unreadable style index must fall back to the default index 0 instead, and a
readable one must still be copied verbatim.
"""

import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
INSERT_ROW = os.path.join(SCRIPTS_DIR, "xlsx_insert_row.py")
ADD_COLUMN = os.path.join(SCRIPTS_DIR, "xlsx_add_column.py")

STYLES_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS_MAIN}">
  <fonts count="1"><font><sz val="11"/><color theme="1"/></font></fonts>
  <fills count="2">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
  </fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
  </cellXfs>
</styleSheet>
"""

WORKBOOK_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_MAIN}" xmlns:r="{NS_REL}">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
"""

# A package-absolute Target resolves the same way before and after the
# relative-rels-target fix, so this fixture does not depend on it.
RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
    Target="/xl/worksheets/sheet1.xml"/>
</Relationships>
"""

SHARED_STRINGS_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<sst xmlns="{NS_MAIN}" count="2" uniqueCount="2">
  <si><t>Header A</t></si>
  <si><t>Header B</t></si>
</sst>
"""


def _worksheet(rows: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<worksheet xmlns="{NS_MAIN}">\n'
            f'  <dimension ref="A1:B3"/>\n'
            f'  <sheetData>\n{rows}\n  </sheetData>\n'
            f'</worksheet>\n')


# Every style index a foreign writer can leave unreadable: B1 empty, row 2
# mixing the empty and the non-numeric form, row 3 unreadable on both cells.
# A1 keeps a readable index so the fallback stays observable next to it.
INVALID_WORKSHEET_XML = _worksheet("""    <row r="1">
      <c r="A1" s="1" t="s"><v>0</v></c>
      <c r="B1" s="" t="s"><v>1</v></c>
    </row>
    <row r="2">
      <c r="A2" s="abc"><v>10</v></c>
      <c r="B2" s=""><v>20</v></c>
    </row>
    <row r="3">
      <c r="A3" s=""><v>30</v></c>
      <c r="B3" s="xyz"><v>40</v></c>
    </row>""")

# The same sheet with every style index readable.
CLEAN_WORKSHEET_XML = _worksheet("""    <row r="1">
      <c r="A1" s="1" t="s"><v>0</v></c>
      <c r="B1" s="1" t="s"><v>1</v></c>
    </row>
    <row r="2">
      <c r="A2" s="1"><v>10</v></c>
      <c r="B2" s="1"><v>20</v></c>
    </row>
    <row r="3">
      <c r="A3" s="1"><v>30</v></c>
      <c r="B3" s="1"><v>40</v></c>
    </row>""")


def build_work_dir(dest: str, worksheet_xml: str) -> str:
    """Lay out a minimal unpacked workbook at *dest* and return its path."""
    for rel in ("xl/_rels", "xl/worksheets"):
        os.makedirs(os.path.join(dest, rel), exist_ok=True)
    parts = {
        "xl/workbook.xml": WORKBOOK_XML,
        "xl/_rels/workbook.xml.rels": RELS_XML,
        "xl/styles.xml": STYLES_XML,
        "xl/sharedStrings.xml": SHARED_STRINGS_XML,
        "xl/worksheets/sheet1.xml": worksheet_xml,
    }
    for rel, body in parts.items():
        with open(os.path.join(dest, rel), "w", encoding="utf-8") as fh:
            fh.write(body)
    return dest


def run_script(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, script, *args],
                          capture_output=True, text=True)


class EditWorkDirTestCase(unittest.TestCase):
    """Hand every test its own pristine copy of the fixture workbook."""

    worksheet_xml = INVALID_WORKSHEET_XML

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.work_dir = build_work_dir(os.path.join(self.root, "work"),
                                       self.worksheet_xml)

    def worksheet(self) -> ET.ElementTree:
        return ET.parse(os.path.join(self.work_dir, "xl", "worksheets",
                                     "sheet1.xml"))

    def cell_style(self, ref: str):
        """Return the ``s`` attribute of one cell (None when absent)."""
        for cell in self.worksheet().getroot().iter(f"{{{NS_MAIN}}}c"):
            if cell.get("r") == ref:
                return cell.get("s")
        raise AssertionError(f"cell {ref} missing from the worksheet")

    def row_numbers(self) -> list[str]:
        return [row.get("r")
                for row in self.worksheet().getroot().iter(f"{{{NS_MAIN}}}row")]

    def assertCleanRun(self, result: subprocess.CompletedProcess) -> None:
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        self.assertNotIn("ValueError", result.stderr, result.stderr)
        self.assertEqual(result.returncode, 0,
                         f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")


class TestInsertRowInvalidStyle(EditWorkDirTestCase):
    def test_copy_style_from_row_with_unreadable_style_index(self):
        """--copy-style-from a row holding s="abc" must not crash."""
        result = run_script(INSERT_ROW, self.work_dir, "--at", "3",
                            "--copy-style-from", "2",
                            "--text", "A=Inserted", "--values", "B=99")
        self.assertCleanRun(result)
        # The unreadable index falls back to 0 instead of aborting the edit.
        self.assertEqual(self.cell_style("A3"), "0")

    def test_shift_and_insert_land_together(self):
        """No half-applied edit: rows stay unique and ascending."""
        result = run_script(INSERT_ROW, self.work_dir, "--at", "2",
                            "--copy-style-from", "3", "--text", "A=Inserted")
        self.assertCleanRun(result)
        self.assertEqual(self.row_numbers(), ["1", "2", "3", "4"])


class TestAddColumnInvalidStyle(EditWorkDirTestCase):
    def test_header_and_formula_styles_from_unreadable_indexes(self):
        """Header and data style lookup over s="" / s="abc" must not crash."""
        result = run_script(ADD_COLUMN, self.work_dir, "--col", "C",
                            "--header", "Total",
                            "--formula", "=B{row}*2", "--formula-rows", "2:3")
        self.assertCleanRun(result)
        self.assertEqual(self.cell_style("C1"), "0")
        self.assertEqual(self.cell_style("C2"), "0")

    def test_border_row_with_unreadable_style_indexes(self):
        """The top-border pass re-reads every cell style in the row."""
        result = run_script(ADD_COLUMN, self.work_dir, "--col", "C",
                            "--border-row", "3")
        self.assertCleanRun(result)
        for ref in ("A3", "B3"):
            style = self.cell_style(ref)
            self.assertTrue((style or "0").isdigit(),
                            f"{ref} keeps a numeric style index, got {style!r}")


class TestReadableStyleIndexesUnchanged(EditWorkDirTestCase):
    """The defensive parse must leave a well-formed workbook alone."""

    worksheet_xml = CLEAN_WORKSHEET_XML

    def test_add_column_copies_the_readable_style_index(self):
        result = run_script(ADD_COLUMN, self.work_dir, "--col", "C",
                            "--header", "Total")
        self.assertCleanRun(result)
        self.assertEqual(self.cell_style("C1"), "1")

    def test_insert_row_copies_the_readable_style_index(self):
        result = run_script(INSERT_ROW, self.work_dir, "--at", "4",
                            "--copy-style-from", "3", "--values", "A=50")
        self.assertCleanRun(result)
        self.assertEqual(self.cell_style("A4"), "1")
        self.assertEqual(self.row_numbers(), ["1", "2", "3", "4"])


if __name__ == "__main__":
    unittest.main()
