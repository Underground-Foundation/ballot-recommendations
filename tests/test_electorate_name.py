import json
import subprocess
import sys
import unittest
from io import BytesIO
from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from pdf_inspection import find_replaceable_electorate_name
from render_recommendations import (draw_electorate_name_block, layout_block, reference_width, remove_block_text,
                                    substitute_electorate_name)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
KEW_PDF = FIXTURES / "Kew.pdf"
# Made by:
# python pdf_inspection.py tests/fixtures/Kew.pdf --candidate-text 'THEODOSIS, Katerine' --electorate-name Kew \
#     > tests/fixtures/kew_rendering.json
KEW_RENDERING = FIXTURES / "kew_rendering.json"


def line_texts(lines) -> list[str]:
    return [" ".join("".join(text for text, _ in word) for word in line) for line in lines]


class FindReplaceableElectorateNameTest(unittest.TestCase):
    def setUp(self):
        self.pdf = pdfplumber.open(KEW_PDF)
        self.page = self.pdf.pages[0]

    def tearDown(self):
        self.pdf.close()

    def test_name_and_its_sentence(self):
        found = find_replaceable_electorate_name(self.page, "Kew")
        self.assertEqual(found["replaceable_text"], "Kew")
        self.assertEqual(len(found["font_faces"]), 1)
        self.assertIn("Arial-Italic", found["font_faces"][0])
        self.assertEqual(found["sizes_pt"], [16.0])
        self.assertEqual(found["horizontal_scale"], 0.85)
        block = found["block"]
        self.assertEqual(block["text"], "Kew is a Red Light electorate!")
        self.assertEqual([line["text"] for line in block["lines"]], ["Kew is a Red", "Light electorate!"])
        self.assertEqual(block["alignment"], "centre")

    def test_runs_split_the_name_and_each_font(self):
        runs = find_replaceable_electorate_name(self.page, "Kew")["block"]["runs"]
        self.assertEqual("".join(run["text"] for run in runs), "Kew is a Red Light electorate!")
        self.assertEqual([(run["text"], run["replaceable"]) for run in runs],
                         [("Kew", True), (" is a ", False), ("Red Light electorate", False), ("!", False)])
        self.assertIn("Arial-Italic", runs[1]["font_face"])
        self.assertIn("Arial-Black", runs[2]["font_face"])

    def test_name_is_case_sensitive_and_whole_words(self):
        # "KEW DISTRICT" isn't matched, so "Kew" is found once; "Ke" is only part of a word.
        with self.assertRaises(SystemExit):
            find_replaceable_electorate_name(self.page, "Ke")
        with self.assertRaises(SystemExit):
            find_replaceable_electorate_name(self.page, "Footscray")


class RenderElectorateNameTest(unittest.TestCase):
    def setUp(self):
        self.replaceable = json.loads(KEW_RENDERING.read_text(encoding="utf-8"))[0]["replaceable_electorate_name"]
        self.block = self.replaceable["block"]

    def layout(self, electorate: str):
        words = substitute_electorate_name(self.replaceable, electorate)
        return layout_block(words, self.block, reference_width(self.replaceable))

    def test_reference_name_keeps_its_layout(self):
        lines, factor = self.layout("Kew")
        self.assertEqual(line_texts(lines), ["Kew is a Red", "Light electorate!"])
        self.assertEqual(factor, 1)

    def test_longer_name_moves_the_rest_of_the_sentence(self):
        lines, factor = self.layout("Albert Park")
        self.assertEqual(line_texts(lines), ["Albert Park is a Red", "Light electorate!"])
        self.assertEqual(factor, 1)

    def test_name_too_long_for_the_lines_is_shrunk(self):
        lines, factor = self.layout("Narre Warren South")
        self.assertEqual(len(lines), 2)
        self.assertLess(factor, 1)
        # The name keeps the replaced name's style; the rest keeps its own.
        name_style = lines[0][0][0][1]
        self.assertIn("Arial-Italic", name_style["font_face"])
        self.assertEqual(line_texts(lines)[1], "Red Light electorate!")

    def test_base_sentence_is_replaced(self):
        writer = PdfWriter()
        page = writer.add_page(PdfReader(KEW_PDF).pages[0])
        self.assertGreater(remove_block_text(page, self.block), 0)
        size = (float(page.mediabox.width), float(page.mediabox.height))
        buffer = BytesIO()
        canvas = Canvas(buffer, pagesize=size)
        draw_electorate_name_block(canvas, self.replaceable, "Albert Park")
        canvas.save()
        buffer.seek(0)
        page.merge_page(PdfReader(buffer).pages[0])
        output = BytesIO()
        writer.write(output)
        output.seek(0)
        with pdfplumber.open(output) as pdf:
            right = pdf.pages[0].crop((self.block["x_pt"] - 5, 0, pdf.pages[0].width, pdf.pages[0].height))
            text = right.extract_text()
        self.assertIn("Albert Park is a Red\nLight electorate!", text)
        self.assertNotIn("Kew is", text)
        # Text around the block is left alone.
        self.assertIn("Number them last", text)
        self.assertIn("Turbocharge the", text)


class CommandLineTest(unittest.TestCase):
    def test_electorate_name(self):
        result = subprocess.run([sys.executable, str(ROOT / "pdf_inspection.py"), str(KEW_PDF),
                                 "--candidate-text", "THEODOSIS, Katerine", "--electorate-name", "Kew"],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        replaceable = json.loads(result.stdout)[0]["replaceable_electorate_name"]
        self.assertEqual(replaceable["replaceable_text"], "Kew")
        self.assertEqual(replaceable["block"]["text"], "Kew is a Red Light electorate!")


if __name__ == "__main__":
    unittest.main()
