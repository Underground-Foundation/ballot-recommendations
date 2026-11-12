import json
import unittest
from io import BytesIO
from pathlib import Path

import pdfplumber
from reportlab.pdfgen.canvas import Canvas

from render_recommendations import draw_legends, legend_order, legend_placements, number_ranges

# Made by:
# python pdf_inspection.py tests/fixtures/Footscray.pdf --candidate-text 'HALL, Katie' \
#     --candidate-count 'Number the squares' 'them last' \
#     --recommendation G "Number Green Light candidates" --recommendation O "Number Orange Light candidates" \
#     --recommendation R "Red Light candidates" > tests/fixtures/footscray_rendering.json
FOOTSCRAY_RENDERING = Path(__file__).resolve().parent / "fixtures" / "footscray_rendering.json"


def candidates_with(*recommendations: str) -> list[dict[str, str]]:
    return [{"Candidate": f"Candidate {i}", "Party": "Independent", "Recommendation": recommendation}
            for i, recommendation in enumerate(recommendations)]


class LegendTest(unittest.TestCase):
    def setUp(self):
        rendering = json.loads(FOOTSCRAY_RENDERING.read_text(encoding="utf-8"))[0]
        self.recommendations = rendering["recommendations"]
        self.page_size = (rendering["district_text"]["page_size_pt"]["width"],
                          rendering["district_text"]["page_size_pt"]["height"])

    def test_order_is_inferred_from_the_legends_down_the_page(self):
        self.assertEqual(legend_order(self.recommendations), ["G", "O", "R"])
        # The order comes from the positions on the page, not from the order of the JSON.
        reversed_recommendations = dict(reversed(self.recommendations.items()))
        self.assertEqual(legend_order(reversed_recommendations), ["G", "O", "R"])

    def test_all_recommendations_stay_in_place(self):
        self.assertEqual(legend_placements(self.recommendations, {"G", "O", "R"}), [("G", 0), ("O", 0), ("R", 0)])

    def test_missing_recommendation_leaves_no_gap(self):
        placements = dict(legend_placements(self.recommendations, {"G", "R"}))
        self.assertEqual(list(placements), ["G", "R"])
        self.assertEqual(placements["G"], 0)
        # R moves up to where O's border was.
        self.assertAlmostEqual(placements["R"], self.recommendations["O"]["border"]["top_pt"]
                               - self.recommendations["R"]["border"]["top_pt"])
        self.assertLess(placements["R"], 0)

    def test_only_the_last_recommendation_moves_to_the_top(self):
        placements = legend_placements(self.recommendations, {"R"})
        self.assertEqual([recommendation for recommendation, _ in placements], ["R"])
        self.assertAlmostEqual(placements[0][1], self.recommendations["G"]["border"]["top_pt"]
                               - self.recommendations["R"]["border"]["top_pt"])

    def test_number_ranges_follow_on_in_legend_order(self):
        candidates = candidates_with("R", "G", "G", "R", "G", "G", "R", "")
        self.assertEqual(number_ranges(self.recommendations, candidates), {"G": "(1-4)", "R": "(5-7)"})

    def test_single_candidate_gets_a_single_number(self):
        candidates = candidates_with("G", "G", "O", "R")
        self.assertEqual(number_ranges(self.recommendations, candidates), {"G": "(1-2)", "O": "(3)", "R": "(4)"})

    def test_drawn_green_legend_has_its_count(self):
        page = self.render(candidates_with("G", "G", "G", "G", "R")).pages[0]
        text = page.extract_text()
        self.assertIn("first (1-4)", text)
        self.assertIn("them last (5)", text)

    def render(self, candidates) -> pdfplumber.PDF:
        buffer = BytesIO()
        canvas = Canvas(buffer, pagesize=self.page_size)
        draw_legends(canvas, self.recommendations, candidates, self.page_size[1])
        canvas.save()
        buffer.seek(0)
        pdf = pdfplumber.open(buffer)
        self.addCleanup(pdf.close)
        return pdf

    def test_range_with_a_soft_hyphen_is_replaced(self):
        # Kew.pdf writes its ranges with a soft hyphen, eg "first (1\xad3)".
        green = json.loads(json.dumps(self.recommendations["G"]).replace("1-3", "1\\u00ad3"))
        self.assertIn("(1\xad3)", green["lines"][-1]["text"])
        page = self.render_legends({"G": green}, candidates_with("G", "G", "G", "G")).pages[0]
        text = page.extract_text()
        self.assertIn("first (1-4)", text)
        self.assertNotIn("\xad", text)

    def render_legends(self, recommendations, candidates) -> pdfplumber.PDF:
        buffer = BytesIO()
        canvas = Canvas(buffer, pagesize=self.page_size)
        draw_legends(canvas, recommendations, candidates, self.page_size[1])
        canvas.save()
        buffer.seek(0)
        pdf = pdfplumber.open(buffer)
        self.addCleanup(pdf.close)
        return pdf

    def test_drawn_legends_skip_missing_recommendation(self):
        page = self.render(candidates_with("G", "R", "R", "")).pages[0]
        words = {word["text"]: word for word in page.extract_words()}
        self.assertIn("GREEN", words)
        self.assertNotIn("ORANGE", words)
        # The RED heading is drawn where the ORANGE heading was, give or take the headings' positions in their borders.
        orange, red = self.recommendations["O"], self.recommendations["R"]
        expected_top = red["heading"]["top_pt"] + orange["border"]["top_pt"] - red["border"]["top_pt"]
        self.assertAlmostEqual(words["RED"]["top"], expected_top, delta=1)
        self.assertEqual(" ".join(w["text"] for w in page.extract_words() if w["top"] > words["RED"]["top"]),
                         "Red Light candidates are a dangerous choice. Number them last (2-3)")

        # Two rounded borders (green, red), each over its own black heading background.
        borders = [c for c in page.curves if c["stroke"] and c["linewidth"] > 8]
        self.assertEqual(len(borders), 2)
        self.assertAlmostEqual(min(b["top"] for b in borders), self.recommendations["G"]["border"]["top_pt"],
                               delta=0.5)
        self.assertAlmostEqual(max(b["top"] for b in borders), orange["border"]["top_pt"], delta=0.5)


if __name__ == "__main__":
    unittest.main()
