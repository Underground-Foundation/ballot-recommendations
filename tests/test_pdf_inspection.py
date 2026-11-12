import json
import subprocess
import sys
import unittest
from pathlib import Path

import pdfplumber

from pdf_inspection import find_recommendation_count

ROOT = Path(__file__).resolve().parent.parent
FOOTSCRAY_PDF = Path(__file__).resolve().parent / "fixtures" / "Footscray.pdf"


class FindRecommendationCountTest(unittest.TestCase):
    def setUp(self):
        self.pdf = pdfplumber.open(FOOTSCRAY_PDF)
        self.page = self.pdf.pages[0]

    def tearDown(self):
        self.pdf.close()

    def test_green_block_spans_its_lines(self):
        green = find_recommendation_count(self.page, "Number Green Light Candidates")
        self.assertEqual(green["text"], "Number Green Light candidates first (1-3)")
        self.assertEqual([line["text"] for line in green["lines"]],
                         ["Number Green", "Light candidates", "first (1-3)"])
        self.assertEqual(green["alignment"], "centre")
        self.assertEqual(green["line_spacing_pt"], 15.0)
        self.assertEqual(green["sizes_pt"], [15.0])
        self.assertTrue(all("DejaVuSans" in face for face in green["font_faces"]))
        self.assertEqual(green["colours"][0]["hex"], "#181715")
        self.assertAlmostEqual(green["x_pt"], 439.37, places=1)
        self.assertAlmostEqual(green["top_pt"], 251.61, places=1)
        self.assertAlmostEqual(green["width_pt"], 112.05, places=1)
        self.assertAlmostEqual(green["height_pt"], 45.0, places=1)
        # Baselines are measured up from the bottom, so they fall line by line.
        baselines = [line["baseline_pt"] for line in green["lines"]]
        self.assertEqual(baselines, sorted(baselines, reverse=True))

    def test_green_border_is_a_rounded_rectangle(self):
        border = find_recommendation_count(self.page, "Number Green Light Candidates")["border"]
        self.assertAlmostEqual(border["x_pt"], 431.08, places=1)
        self.assertAlmostEqual(border["top_pt"], 204.64, places=1)
        self.assertAlmostEqual(border["width_pt"], 128.55, places=1)
        self.assertAlmostEqual(border["height_pt"], 100.0, places=1)
        self.assertAlmostEqual(border["corner_radius_pt"]["x"], 14.17, places=1)
        self.assertAlmostEqual(border["corner_radius_pt"]["y"], 14.17, places=1)
        self.assertEqual([c["hex"] for c in border["stroke_colours"]], ["#01A300"])
        self.assertEqual([c["hex"] for c in border["fill_colours"]], ["#FFFFFF"])
        self.assertAlmostEqual(border["line_widths_pt"][0], 8.504, places=2)

    def test_green_heading_has_its_black_background(self):
        heading = find_recommendation_count(self.page, "Number Green Light Candidates")["heading"]
        self.assertEqual(heading["text"], "GREEN")
        self.assertEqual(heading["sizes_pt"], [20.0])
        self.assertTrue(all("DejaVuSans" in face for face in heading["font_faces"]))
        self.assertEqual(heading["colours"][0]["hex"], "#FFFFFF")
        self.assertAlmostEqual(heading["lines"][0]["baseline_pt"], 607.58, places=1)
        background = heading["background"]
        self.assertAlmostEqual(background["x_pt"], 444.93, places=1)
        self.assertAlmostEqual(background["top_pt"], 209.57, places=1)
        self.assertAlmostEqual(background["width_pt"], 100.0, places=1)
        self.assertAlmostEqual(background["height_pt"], 37.8, places=1)
        self.assertEqual(background["corner_radius_pt"], {"x": 0.0, "y": 0.0})
        self.assertEqual([c["hex"] for c in background["fill_colours"]], ["#181715"])

    def test_each_block_has_its_own_heading_and_border(self):
        orange = find_recommendation_count(self.page, "Number Orange")
        self.assertEqual(orange["heading"]["text"], "ORANGE")
        self.assertEqual([c["hex"] for c in orange["border"]["stroke_colours"]], ["#E27D09"])
        red = find_recommendation_count(self.page, "red light")
        self.assertEqual(red["heading"]["text"], "RED")
        self.assertEqual([c["hex"] for c in red["border"]["stroke_colours"]], ["#CC1408"])

    def test_text_in_other_columns_is_left_out(self):
        # "ONE NATION", a party name, is at about the height of "first (1-3)".
        green = find_recommendation_count(self.page, "Number Green Light Candidates")
        self.assertNotIn("ONE NATION", green["text"])
        self.assertGreater(green["x_pt"], 400)

    def test_other_blocks_are_kept_separate(self):
        orange = find_recommendation_count(self.page, "Number Orange")
        self.assertEqual(orange["text"], "Number Orange Light candidates second (3)")
        red = find_recommendation_count(self.page, "red light")
        self.assertEqual(red["text"], "Red Light candidates are a dangerous choice. Number them last (4-8)")

    def test_ambiguous_or_missing_text_exits(self):
        with self.assertRaises(SystemExit):
            find_recommendation_count(self.page, "Light candidates")  # in both the Green and Orange blocks
        with self.assertRaises(SystemExit):
            find_recommendation_count(self.page, "Purple Light")


class CommandLineTest(unittest.TestCase):
    def run_inspection(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "pdf_inspection.py"), str(FOOTSCRAY_PDF), *args],
                              capture_output=True, text=True, cwd=ROOT)

    def test_candidate_count_and_recommendations(self):
        result = self.run_inspection("--candidate-text", "HALL, Katie",
                                     "--candidate-count", "Number the squares",
                                     "--recommendation", "G", "Number Green Light Candidates",
                                     "--recommendation", "R", "Red Light")
        self.assertEqual(result.returncode, 0, result.stderr)
        matches = json.loads(result.stdout)
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match["candidate_text"]["text"], "HALL, Katie")
        self.assertEqual(match["party_text"]["text"], "LABOR PARTY")
        self.assertEqual(match["district_text"]["text"], "FOOTSCRAY DISTRICT")
        self.assertEqual([(c["box_text"], c["text"]) for c in match["candidate_count"]],
                         [("Number the squares", "8")])
        recommendations = match["recommendations"]
        self.assertEqual(list(recommendations), ["G", "R"])
        self.assertEqual(recommendations["G"]["text"], "Number Green Light candidates first (1-3)")
        self.assertEqual(recommendations["R"]["text"],
                         "Red Light candidates are a dangerous choice. Number them last (4-8)")

    def test_green_orange_and_red_recommendations(self):
        result = self.run_inspection("--candidate-text", "HALL, Katie",
                                     "--recommendation", "G", "Number Green Light candidates",
                                     "--recommendation", "O", "Number Orange Light candidates",
                                     "--recommendation", "R", "Red Light candidates")
        self.assertEqual(result.returncode, 0, result.stderr)
        recommendations = json.loads(result.stdout)[0]["recommendations"]
        self.assertEqual(list(recommendations), ["G", "O", "R"])
        expected = {
            # The orange and red borders' corners are elliptical: wider than they are tall.
            "G": ("Number Green Light candidates first (1-3)", "GREEN", "#01A300", 14.17),
            "O": ("Number Orange Light candidates second (3)", "ORANGE", "#E27D09", 12.59),
            "R": ("Red Light candidates are a dangerous choice. Number them last (4-8)", "RED", "#CC1408", 12.59),
        }
        for recommendation, (text, heading, border_colour, vertical_radius) in expected.items():
            with self.subTest(recommendation=recommendation):
                details = recommendations[recommendation]
                self.assertEqual(details["text"], text)
                self.assertEqual(details["heading"]["text"], heading)
                self.assertEqual([c["hex"] for c in details["heading"]["background"]["fill_colours"]], ["#181715"])
                self.assertEqual([c["hex"] for c in details["border"]["stroke_colours"]], [border_colour])
                self.assertAlmostEqual(details["border"]["corner_radius_pt"]["x"], 14.17, places=1)
                self.assertAlmostEqual(details["border"]["corner_radius_pt"]["y"], vertical_radius, places=1)

    def test_repeated_recommendation_is_rejected(self):
        result = self.run_inspection("--candidate-text", "HALL, Katie",
                                     "--recommendation", "G", "Number Green",
                                     "--recommendation", "G", "Number Orange")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("more than once for G", result.stderr)


if __name__ == "__main__":
    unittest.main()
