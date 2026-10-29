"""Render each electorate's recommended candidates onto a copy of a base ballot PDF.

Each candidate is a row of: a box with the party logo (if any), an empty box, a box filled with the colour of the
candidate's recommendation (with its letter inside), then the candidate's name with the party name below it. The
positions, sizes and fonts come from the JSON printed by pdf_inspection.py for a candidate on a reference ballot.
"""

import argparse
import csv
import json
import re
import subprocess
import sys
from functools import cache
from io import BytesIO
from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics import renderPDF
from reportlab.pdfgen.canvas import Canvas
from svglib.svglib import svg2rlg

DEFAULT_INPUT = "candidates_with_scores.csv"
DEFAULT_RENDERING = "candidate_rendering.json"
DEFAULT_BASE_PDF = "base.pdf"
DEFAULT_LOGO_DIR = "party_logos"
DEFAULT_OUTPUT_DIR = "ballots"
SCORE_COLUMN = "Recommendation"
# Fill colours of the recommendation box, as on the reference ballot.
SCORE_COLOURS = {"G": "#01A300", "O": "#FF831F", "R": "#FF0000"}


def read_electorates(path: Path) -> dict[str, list[dict[str, str]]]:
    """Group the CSV's candidates by electorate, keeping only electorates with a recommendation for someone."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        sys.exit(f"Input file not found: {path}")

    electorates: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        electorates.setdefault(row["Electorate"].strip(), []).append(row)

    recommended = {}
    for electorate, candidates in electorates.items():
        unscored = [c["Candidate"] for c in candidates if not c[SCORE_COLUMN].strip()]
        if len(unscored) == len(candidates):
            continue
        if unscored:
            print(f"Warning: {electorate} has no {SCORE_COLUMN} for: {', '.join(unscored)}", file=sys.stderr)
        recommended[electorate] = candidates
    return recommended


def register_font(pdf_font_name: str) -> str:
    """Register the system's copy of a font named in a PDF, returning its ReportLab name (Helvetica if absent)."""
    # Embedded fonts are usually subsets, named with a six-letter prefix, eg IOEEAD+DejaVuSans.
    name = pdf_font_name.split("+", 1)[-1]
    if name in pdfmetrics.getRegisteredFontNames():
        return name
    # Match on the PostScript name, as a family name alone would lose the style, eg the "Bold" of DejaVuSans-Bold.
    font_file = subprocess.run(
        ["fc-match", "--format=%{file}", f":postscriptname={name}"], capture_output=True, text=True, check=False
    ).stdout
    if not font_file.lower().endswith((".ttf", ".otf")):
        print(f"Warning: no TrueType file found for font {name}; using Helvetica", file=sys.stderr)
        return "Helvetica"
    pdfmetrics.registerFont(TTFont(name, font_file))
    return name


def get_text_colour(text: dict) -> HexColor:
    colour = text["colours"][0]
    return HexColor(colour["hex"]) if isinstance(colour, dict) else HexColor("#000000")


def serialize_ballot_name(name: str) -> str:
    """Format a name the way ballots do, eg "Katie Hall" as "HALL, Katie"."""
    *given, surname = name.split()
    return f"{surname.upper()}, {' '.join(given)}" if given else surname.upper()


def contains_words(words: list[str], sought: list[str]) -> bool:
    """Whether the sought words occur consecutively in the words."""
    return any(words[i:i + len(sought)] == sought for i in range(len(words) - len(sought) + 1))


@cache  # so each party's warning is printed once
def get_party_logo(party: str, logo_dir: Path) -> Path | None:
    """Find the party's logo: the file named exactly after the party, else the only file whose name contains the
    party's name as whole words, eg "One Nation" in "One Nation Victoria" (but "Liberal" isn't in "Libertarian"). Any
    parenthesised aside in a file's name is ignored, eg the "(formerly Liberal Democrats)" of the Libertarian Party."""
    name = party.casefold()
    if name == "independent":
        return None
    logos = [f for f in logo_dir.iterdir() if f.is_file()]
    exact = [f for f in logos if f.stem.casefold() == name]
    if exact:
        return exact[0]
    words = name.split()
    containing = [f for f in logos if contains_words(re.sub(r"\(.*?\)", " ", f.stem.casefold()).split(), words)]
    if len(containing) == 1:
        return containing[0]
    found = f"{len(containing)} logos whose names contain it" if containing else "no matching logo"
    print(f"Warning: rendering no logo for party {party!r}; found {found} in {logo_dir}", file=sys.stderr)
    return None


def draw_logo(canvas: Canvas, logo: Path, x: float, y: float, width: float, height: float) -> None:
    """Draw the logo as large as fits within the box, keeping its proportions and centring it."""
    if logo.suffix.casefold() != ".svg":
        canvas.drawImage(str(logo), x, y, width, height, mask="auto", preserveAspectRatio=True, anchor="c")
        return
    drawing = svg2rlg(str(logo))
    scale = min(width / drawing.width, height / drawing.height)
    drawing.scale(scale, scale)
    renderPDF.draw(drawing, canvas, x + (width - drawing.width * scale) / 2,
                   y + (height - drawing.height * scale) / 2)


def draw_district(canvas: Canvas, electorate: str, district_text: dict) -> None:
    """Draw eg "ALBERT PARK DISTRICT" with the font, colour and position of the reference ballot's district text."""
    font = register_font(district_text["font_faces"][0])
    size = district_text["sizes_pt"][0]
    canvas.setFont(font, size)
    canvas.setFillColor(get_text_colour(district_text))
    # The baseline is already measured up from the bottom of the page, as ReportLab expects.
    canvas.drawString(district_text["x_pt"], district_text["baseline_pt"], f"{electorate.upper()} DISTRICT")


def render_overlay(electorate: str, candidates: list[dict[str, str]], rendering: dict,
                   page_size: tuple[float, float], logo_dir: Path) -> BytesIO:
    """Draw the district name and candidate rows on a transparent page, to be laid over the base ballot."""
    candidate_text, box, party_text = rendering["candidate_text"], rendering["left_box"], rendering["party_text"]
    page_height = page_size[1]
    candidate_font = register_font(candidate_text["font_faces"][0])
    candidate_size = candidate_text["sizes_pt"][0]
    party_font = register_font(party_text["font_faces"][0])
    party_size = party_text["sizes_pt"][0]
    line_width = box["line_widths_pt"][0]
    # The reference box outline may be a pattern rather than a colour, so outline in the text's colour.
    outline = get_text_colour(candidate_text)

    # Offsets of the text baselines below the top of the row's boxes, as on the reference ballot.
    candidate_offset = (page_height - candidate_text["baseline_pt"]) - box["top_pt"]
    party_offset = (page_height - party_text["baseline_pt"]) - box["top_pt"]
    width, height = box["width_pt"], box["height_pt"]
    score_x = box["x_pt"]
    empty_x = score_x - box["horizontal_spacings_pt"][0]
    logo_x = empty_x - box["horizontal_spacings_pt"][1]
    spacing = box["vertical_spacing_pt"]

    ascent, descent = pdfmetrics.getAscentDescent(candidate_font, candidate_size)
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=page_size)
    draw_district(canvas, electorate, rendering["district_text"])
    canvas.setLineWidth(line_width)
    canvas.setStrokeColor(outline)
    for i, candidate in enumerate(candidates):
        top = box["top_pt"] + i * spacing
        bottom = page_height - top - height  # ReportLab measures y up from the bottom of the page
        if bottom < 0:
            print(f"Warning: no room on the page for {candidate['Candidate']}", file=sys.stderr)
            break

        logo = get_party_logo(candidate["Party"].strip(), logo_dir)
        if logo:
            draw_logo(canvas, logo, logo_x, bottom, width, height)
        canvas.rect(logo_x, bottom, width, height)
        canvas.rect(empty_x, bottom, width, height)

        score = candidate[SCORE_COLUMN].strip().upper()
        fill = SCORE_COLOURS.get(score)
        if score and not fill:
            print(f"Warning: unknown {SCORE_COLUMN} {score!r} for {candidate['Candidate']}", file=sys.stderr)
        if fill:
            canvas.setFillColor(HexColor(fill))
        canvas.rect(score_x, bottom, width, height, fill=bool(fill))
        canvas.setFillColor(white)
        canvas.setFont(candidate_font, candidate_size)
        # Centre the letter vertically on its glyph height (descent is negative).
        canvas.drawCentredString(score_x + width / 2, bottom + height / 2 - (ascent + descent) / 2, score)

        canvas.setFillColor(get_text_colour(candidate_text))
        canvas.drawString(candidate_text["x_pt"], page_height - top - candidate_offset,
                          serialize_ballot_name(candidate["Candidate"]))
        canvas.setFillColor(get_text_colour(party_text))
        canvas.setFont(party_font, party_size)
        canvas.drawString(party_text["x_pt"], page_height - top - party_offset, candidate["Party"].strip().upper())
    canvas.save()
    buffer.seek(0)
    return buffer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=Path(DEFAULT_INPUT),
                        help=f"CSV of candidates and their recommendations (default: {DEFAULT_INPUT})")
    parser.add_argument("--rendering", type=Path, default=Path(DEFAULT_RENDERING),
                        help=f"JSON printed by pdf_inspection.py (default: {DEFAULT_RENDERING})")
    parser.add_argument("--base-pdf", type=Path, default=Path(DEFAULT_BASE_PDF),
                        help=f"ballot to draw the candidates onto (default: {DEFAULT_BASE_PDF})")
    parser.add_argument("--logo-dir", type=Path, default=Path(DEFAULT_LOGO_DIR),
                        help=f"directory of party logos (default: {DEFAULT_LOGO_DIR})")
    parser.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR),
                        help=f"directory to write a PDF per electorate into (default: {DEFAULT_OUTPUT_DIR})")
    args = parser.parse_args()

    for path in (args.rendering, args.base_pdf):
        if not path.is_file():
            sys.exit(f"No such file: {path}")
    matches = json.loads(args.rendering.read_text(encoding="utf-8"))
    rendering = matches[0] if matches else {}
    box = rendering.get("left_box") or {}
    if not rendering.get("district_text"):
        sys.exit(f'{args.rendering} has no district_text; the reference ballot needs a line like "FOOTSCRAY DISTRICT"')
    if not (rendering.get("party_text") and box.get("line_widths_pt") and box.get("vertical_spacing_pt")
            and len(box.get("horizontal_spacings_pt", [])) >= 2):
        sys.exit(f"{args.rendering} needs a match with a party_text and a left_box that has a line width, "
                 "a box below it and two boxes to its left; inspect a different candidate")

    electorates = read_electorates(args.input)
    if not electorates:
        sys.exit(f"No electorate in {args.input} has a {SCORE_COLUMN}")

    with pdfplumber.open(args.base_pdf) as base:
        existing = [line["text"] for line in base.pages[0].extract_text_lines()
                    if line["text"].strip().endswith(" DISTRICT")]
    if existing:
        print(f"Warning: {args.base_pdf} already has {existing[0]!r}; each district will be drawn over it",
              file=sys.stderr)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for electorate, candidates in electorates.items():
        base_page = PdfReader(args.base_pdf).pages[0]
        page_size = (float(base_page.mediabox.width), float(base_page.mediabox.height))
        overlay = render_overlay(electorate, candidates, rendering, page_size, args.logo_dir)
        base_page.merge_page(PdfReader(overlay).pages[0])

        writer = PdfWriter()
        writer.add_page(base_page)
        output = args.output_dir / f"{electorate}.pdf"
        with output.open("wb") as f:
            writer.write(f)
        print(f"Wrote {len(candidates)} candidates for {electorate} to {output}")


if __name__ == "__main__":
    main()
