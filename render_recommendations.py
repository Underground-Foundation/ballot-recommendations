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
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import ArrayObject, ContentStream, FloatObject, TextStringObject
from reportlab.lib.colors import HexColor, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics import renderPDF
from reportlab.pdfgen.canvas import Canvas
from svglib.svglib import svg2rlg

from pdf_inspection import find_candidate_count

DEFAULT_INPUT = "candidates_with_scores.csv"
DEFAULT_RENDERING = "candidate_rendering.json"
DEFAULT_BASE_PDF = "base.pdf"
DEFAULT_LOGO_DIR = "party_logos"
DEFAULT_OUTPUT_DIR = "ballots"
SCORE_COLUMN = "Recommendation"
# Fill colours of the recommendation box, as on the reference ballot.
SCORE_COLOURS = {"G": "#01A300", "O": "#FF831F", "R": "#FF0000"}


def filter_rows(rows: list[dict[str, str]], column: str, value: str | None, path: Path) -> list[dict[str, str]]:
    """Keep the rows whose column matches the value, ignoring case and surrounding spaces (all rows if no value)."""
    if value is None:
        return rows
    kept = [row for row in rows if row.get(column, "").strip().casefold() == value.strip().casefold()]
    if not kept:
        available = sorted({row.get(column, "").strip() for row in rows} - {""})
        sys.exit(f"No candidates in {path} with {column} {value!r}; choose from: {', '.join(available)}")
    return kept


def read_electorates(path: Path, house: str, electorate: str | None = None) -> dict[str, list[dict[str, str]]]:
    """Group the house's candidates in the CSV by electorate, keeping only electorates with a recommendation for
    someone. If an electorate is given, only its candidates are kept."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        sys.exit(f"Input file not found: {path}")
    rows = filter_rows(rows, "House", house, path)
    rows = filter_rows(rows, "Electorate", electorate, path)

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


def draw_with_style_reference(canvas: Canvas, text: str, reference: dict) -> None:
    """Draw the text with the font, colour and position of the given text on the reference ballot."""
    font = register_font(reference["font_faces"][0])
    size = reference["sizes_pt"][0]
    canvas.saveState()  # as the horizontal scale would otherwise carry over to later text
    # The baseline is already measured up from the bottom of the page, as ReportLab expects.
    text_object = canvas.beginText(reference["x_pt"], reference["baseline_pt"])
    text_object.setFont(font, size)
    text_object.setHorizScale(reference.get("horizontal_scale", 1) * 100)
    text_object.setFillColor(get_text_colour(reference))
    text_object.textOut(text)
    canvas.drawText(text_object)
    canvas.restoreState()


def legend_top(legend: dict) -> float:
    """The top of a recommendation's legend, in points down from the top of the page: the top of its border, else
    of its heading, else of its text."""
    return (legend.get("border") or legend.get("heading") or legend)["top_pt"]


def legend_order(recommendations: dict[str, dict]) -> list[str]:
    """The recommendations in the order their legends appear down the reference ballot, eg G, O, R."""
    return sorted(recommendations, key=lambda recommendation: legend_top(recommendations[recommendation]))


def legend_placements(recommendations: dict[str, dict], present: set[str]) -> list[tuple[str, float]]:
    """The recommendations whose legends to draw, ie those given to a candidate, in legend order, each with how far
    to move its legend down the page (in points). The legends fill the reference ballot's legend positions from the
    top, so that a missing recommendation leaves no gap, eg with no O, the R legend is drawn where the O one was."""
    order = legend_order(recommendations)
    shown = [recommendation for recommendation in order if recommendation in present]
    return [
        (recommendation, legend_top(recommendations[position]) - legend_top(recommendations[recommendation]))
        for recommendation, position in zip(shown, order)
    ]


def hex_colours(colours: list) -> list[str]:
    """The colours described by pdf_inspection.py that are given in RGB, as hex."""
    return [colour["hex"] for colour in colours if isinstance(colour, dict) and "hex" in colour]


# How far along a Bézier curve's tangent its control points are, as a proportion of the radius, for the curve to
# approximate a quarter of a circle or ellipse.
KAPPA = 4 / 3 * (2 ** 0.5 - 1)


def draw_shape(canvas: Canvas, shape: dict, shift: float, page_height: float) -> None:
    """Draw a (possibly rounded) rectangle described by pdf_inspection.py, filled then stroked, moved down by shift.
    The bounds are those of its path, so its stroke straddles them, as on the reference ballot."""
    fills, strokes = hex_colours(shape["fill_colours"]), hex_colours(shape["stroke_colours"])
    radius = shape.get("corner_radius_pt") or {"x": 0.0, "y": 0.0}
    rx, ry = radius["x"], radius["y"]
    x0, x1 = shape["x_pt"], shape["right_pt"]
    # ReportLab measures y up from the bottom of the page.
    top, bottom = page_height - (shape["top_pt"] + shift), page_height - (shape["bottom_pt"] + shift)
    kx, ky = KAPPA * rx, KAPPA * ry

    path = canvas.beginPath()
    path.moveTo(x0 + rx, top)
    path.lineTo(x1 - rx, top)
    path.curveTo(x1 - rx + kx, top, x1, top - ry + ky, x1, top - ry)
    path.lineTo(x1, bottom + ry)
    path.curveTo(x1, bottom + ry - ky, x1 - rx + kx, bottom, x1 - rx, bottom)
    path.lineTo(x0 + rx, bottom)
    path.curveTo(x0 + rx - kx, bottom, x0, bottom + ry - ky, x0, bottom + ry)
    path.lineTo(x0, top - ry)
    path.curveTo(x0, top - ry + ky, x0 + rx - kx, top, x0 + rx, top)
    path.close()

    canvas.saveState()
    if fills:
        canvas.setFillColor(HexColor(fills[0]))
    if strokes:
        canvas.setStrokeColor(HexColor(strokes[0]))
        canvas.setLineWidth(shape["line_widths_pt"][0])
    canvas.drawPath(path, fill=bool(fills), stroke=bool(strokes))
    canvas.restoreState()


# A range of candidate numbers in a legend, eg the "(1-3)" of "Number Green Light candidates first (1-3)". Its dash
# may be any kind, eg the soft hyphen (U+00AD) of some ballots, which many terminals don't show, so "(1-3)" looks
# like "(13)".
NUMBER_RANGE = re.compile(r"\(\d+(?:\s*[-­‐-―−]\s*\d+)?\)")


def format_number_range(first: int, count: int) -> str:
    """The range of the count of numbers from first, eg "(5-7)", or "(5)" for one number."""
    last = first + count - 1
    return f"({first})" if last == first else f"({first}-{last})"


def number_ranges(recommendations: dict[str, dict], candidates: list[dict[str, str]]) -> dict[str, str]:
    """The range of numbers to give the candidates of each recommendation, numbering them in legend order, eg with
    four G candidates and three R ones, G is "(1-4)" and R is "(5-7)"."""
    scores = [candidate[SCORE_COLUMN].strip().upper() for candidate in candidates]
    ranges = {}
    first = 1
    for recommendation in legend_order(recommendations):
        count = scores.count(recommendation)
        if count:
            ranges[recommendation] = format_number_range(first, count)
            first += count
    return ranges


def text_width(text: str, block: dict) -> float:
    """The width of the text drawn in the block's font, size and horizontal scale."""
    font = register_font(block["font_faces"][0])
    return pdfmetrics.stringWidth(text, font, block["sizes_pt"][0]) * block.get("horizontal_scale", 1)


def draw_text_block(canvas: Canvas, block: dict, shift: float, number_range: str | None = None) -> None:
    """Draw each line of a block of text described by pdf_inspection.py, moved down by shift. If a number range is
    given, it replaces the block's last range, eg "(1-3)", and that line is moved to keep the block's alignment."""
    lines = [dict(line) for line in block["lines"]]
    if number_range:
        line = next((line for line in reversed(lines) if NUMBER_RANGE.search(line["text"])), None)
        if line is None:
            print(f'Warning: no number range like "(1-3)" in "{block["text"]}"', file=sys.stderr)
        else:
            *_, found = NUMBER_RANGE.finditer(line["text"])
            text = line["text"][:found.start()] + number_range + line["text"][found.end():]
            growth = text_width(text, block) - text_width(line["text"], block)
            line["x_pt"] -= {"centre": growth / 2, "right": growth}.get(block.get("alignment"), 0)
            line["text"] = text
    for line in lines:
        # Baselines are measured up from the bottom of the page, so moving down lowers them.
        draw_with_style_reference(canvas, line["text"],
                                  {**block, "x_pt": line["x_pt"], "baseline_pt": line["baseline_pt"] - shift})


def draw_legend(canvas: Canvas, legend: dict, shift: float, page_height: float, number_range: str | None) -> None:
    """Draw a recommendation's legend, ie its border, its heading on its background and its text, moved down by
    shift (in points), eg the rounded green box headed "GREEN" saying "Number Green Light candidates first (1-3)",
    with the range of numbers for its candidates in place of the reference ballot's."""
    if legend.get("border"):
        draw_shape(canvas, legend["border"], shift, page_height)
    heading = legend.get("heading")
    if heading:
        if heading.get("background"):
            draw_shape(canvas, heading["background"], shift, page_height)
        draw_text_block(canvas, heading, shift)
    draw_text_block(canvas, legend, shift, number_range)


def draw_legends(canvas: Canvas, recommendations: dict[str, dict], candidates: list[dict[str, str]],
                 page_height: float) -> None:
    """Draw the legend of each recommendation given to any of the candidates, filling the legend positions from the
    top, each with the range of numbers for its candidates."""
    present = {candidate[SCORE_COLUMN].strip().upper() for candidate in candidates} - {""}
    unknown = sorted(present - set(recommendations))
    if unknown:
        print(f"Warning: no legend in the rendering JSON for {SCORE_COLUMN} {', '.join(unknown)}", file=sys.stderr)
    ranges = number_ranges(recommendations, candidates)
    for recommendation, shift in legend_placements(recommendations, present):
        draw_legend(canvas, recommendations[recommendation], shift, page_height, ranges.get(recommendation))


def make_room_for_number(page: PageObject, box_text: str, number: str, gap: float) -> None:
    """Remove the number, the last in the page's text showing box_text, leaving a gap for its replacement. The gap is in
    thousandths of the text's font size, and moves the text after the number along to suit the replacement's width."""
    squeeze = lambda text: "".join(text.split())  # spaces may be drawn as gaps rather than as characters
    content = ContentStream(page.get_contents(), page.pdf)
    for operands, operator in content.operations:
        if operator != b"TJ":
            continue
        text = "".join(item for item in operands[0] if isinstance(item, str))
        numbers = list(re.finditer(r"\d+", text))
        if not numbers or numbers[-1].group() != number or squeeze(box_text) not in squeeze(text):
            continue

        found = numbers[-1]
        spliced = ArrayObject()
        position = 0
        gap_added = False
        for item in operands[0]:
            if not isinstance(item, str):
                spliced.append(item)
                continue
            start, position = position, position + len(item)
            if position <= found.start() or start >= found.end():
                spliced.append(item)
                continue
            if found.start() > start:
                spliced.append(TextStringObject(item[:found.start() - start]))
            if not gap_added:
                spliced.append(FloatObject(-gap))
                gap_added = True
            if found.end() < position:
                spliced.append(TextStringObject(item[found.end() - start:]))
        operands[0] = spliced
        page.replace_contents(content)
        return
    sys.exit(f'Could not edit the number {number} next to "{box_text}" in the base PDF; '
             "its text is drawn in a way this program doesn't handle")


def multiply(m: list[float], n: list[float]) -> list[float]:
    """The product of two PDF transformation matrices [a b c d e f], ie m then n."""
    return [m[0] * n[0] + m[1] * n[2], m[0] * n[1] + m[1] * n[3],
            m[2] * n[0] + m[3] * n[2], m[2] * n[1] + m[3] * n[3],
            m[4] * n[0] + m[5] * n[2] + n[4], m[4] * n[1] + m[5] * n[3] + n[5]]


IDENTITY = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
POSITION_TOLERANCE = 1.0  # pt


def remove_block_text(page: PageObject, block: dict) -> int:
    """Remove the page's text drawn within a block described by pdf_inspection.py, ie each text-showing operation
    whose origin is on one of the block's baselines, between its left and right edges. Return how many were removed.

    Only the start of each operation is located (its glyphs' widths aren't worked out), so text drawn by further
    operations after it, without being positioned anew, is taken to start there too. Text in form XObjects is left."""
    def in_block(x: float, y: float) -> bool:
        return (block["x_pt"] - POSITION_TOLERANCE <= x <= block["right_pt"] + POSITION_TOLERANCE
                and any(abs(y - line["baseline_pt"]) <= POSITION_TOLERANCE for line in block["lines"]))

    content = ContentStream(page.get_contents(), page.pdf)
    ctm, stack = IDENTITY, []
    text_matrix = line_matrix = IDENTITY
    leading = 0.0
    kept, removed = [], 0
    for operands, operator in content.operations:
        values = [float(o) for o in operands] if operator in (b"cm", b"Tm", b"Td", b"TD", b"TL") else []
        if operator == b"q":
            stack.append(ctm)
        elif operator == b"Q" and stack:
            ctm = stack.pop()
        elif operator == b"cm":
            ctm = multiply(values, ctm)
        elif operator == b"BT":
            text_matrix = line_matrix = IDENTITY
        elif operator == b"Tm":
            text_matrix = line_matrix = values
        elif operator == b"TL":
            leading = values[0]
        elif operator in (b"Td", b"TD"):
            if operator == b"TD":
                leading = -values[1]
            text_matrix = line_matrix = multiply([1, 0, 0, 1, *values], line_matrix)
        elif operator in (b"T*", b"'", b'"'):
            text_matrix = line_matrix = multiply([1, 0, 0, 1, 0, -leading], line_matrix)

        if operator in (b"Tj", b"TJ", b"'", b'"'):
            origin = multiply(text_matrix, ctm)
            if in_block(origin[4], origin[5]):
                removed += 1
                # Keep the move to the next line (and the spacing) of the operators that make one.
                if operator == b'"':
                    kept += [([operands[0]], b"Tw"), ([operands[1]], b"Tc")]
                if operator in (b"'", b'"'):
                    kept.append(([], b"T*"))
                continue
        kept.append((operands, operator))
    if removed:
        content.operations = kept
        page.replace_contents(content)
    return removed


def styled_width(text: str, style: dict, factor: float = 1) -> float:
    """The width of the text drawn in the style of a run described by pdf_inspection.py, with its size scaled by
    the factor."""
    font = register_font(style["font_face"])
    return pdfmetrics.stringWidth(text, font, style["size_pt"] * factor) * style.get("horizontal_scale", 1)


# A word, as pieces of text each in the style of the run it came from, eg "Kew's" when only "Kew" is replaceable.
Word = list[tuple[str, dict]]


def substitute_electorate_name(replaceable: dict, electorate: str) -> list[Word]:
    """The words of the block containing the electorate's name, with the name replaced by the given electorate's,
    in capitals if the replaced name was."""
    name = electorate.upper() if replaceable["replaceable_text"].isupper() else electorate
    words, word = [], []
    for run in replaceable["block"]["runs"]:
        for part in re.split(r"(\s+)", name if run["replaceable"] else run["text"]):
            if part.isspace():
                if word:
                    words.append(word)
                word = []
            elif part:
                word.append((part, run))
    if word:
        words.append(word)
    return words


def line_width(words: list[Word], factor: float) -> float:
    """The width of a line of words, each separated by a space in the style of the end of the word before it."""
    width = sum(styled_width(text, style, factor) for word in words for text, style in word)
    return width + sum(styled_width(" ", word[-1][1], factor) for word in words[:-1])


def wrap_words(words: list[Word], max_width: float, factor: float) -> list[list[Word]]:
    """Fill lines with as many words as fit within the width, at sizes scaled by the factor."""
    lines = []
    for word in words:
        if lines and line_width(lines[-1] + [word], factor) <= max_width:
            lines[-1].append(word)
        else:
            lines.append([word])
    return lines


SHRINK_STEP = 0.97
MIN_SHRINK = 0.5


def reference_width(replaceable: dict) -> float:
    """The width of the widest line of the block on the reference ballot, measured as the rendered text will be."""
    words = substitute_electorate_name(replaceable, replaceable["replaceable_text"])
    widths = []
    for line in replaceable["block"]["lines"]:
        count = len(line["text"].split())
        widths.append(line_width(words[:count], 1))
        words = words[count:]
    return max(widths)


def layout_block(words: list[Word], block: dict, max_width: float) -> tuple[list[list[Word]], float]:
    """Wrap the words to fit within the width and the block's number of lines, as on the reference ballot,
    shrinking their sizes if a longer name doesn't fit. Return the lines and the factor by which to scale the sizes."""
    max_width += 0.01  # so that the reference's widest line still fits, despite rounding
    factor = 1.0
    while True:
        lines = wrap_words(words, max_width, factor)
        if ((len(lines) <= len(block["lines"]) and all(line_width(line, factor) <= max_width for line in lines))
                or factor < MIN_SHRINK):
            return lines, factor
        factor *= SHRINK_STEP


def draw_electorate_name_block(canvas: Canvas, replaceable: dict, electorate: str) -> None:
    """Draw the block of text naming the reference ballot's electorate, eg "Kew is a Red Light electorate!", naming
    the given electorate instead. The rest of the text is moved to make room, the words re-wrapped and the lines kept
    to the block's alignment, eg centred, and its baselines (centring fewer lines on them)."""
    block = replaceable["block"]
    lines, factor = layout_block(substitute_electorate_name(replaceable, electorate), block,
                                 reference_width(replaceable))
    baselines = [line["baseline_pt"] for line in block["lines"]]
    spacing = block["line_spacing_pt"] or 0
    # With fewer lines than the reference, centre them on the reference's lines (baselines fall down the page).
    offset = (len(baselines) - len(lines)) * spacing / 2
    for i, words in enumerate(lines):
        baseline = baselines[i] - offset if i < len(baselines) else baselines[-1] - (i - len(baselines) + 1) * spacing
        width = line_width(words, factor)
        x = {"centre": block["centre_x_pt"] - width / 2, "right": block["right_pt"] - width}.get(
            block.get("alignment"), block["x_pt"])
        pieces = [piece for j, word in enumerate(words)
                  for piece in (word if j == 0 else [(" ", words[j - 1][-1][1])] + word)]
        for text, style in pieces:
            canvas.saveState()  # as the horizontal scale would otherwise carry over to later text
            text_object = canvas.beginText(x, baseline)
            text_object.setFont(register_font(style["font_face"]), style["size_pt"] * factor)
            text_object.setHorizScale(style.get("horizontal_scale", 1) * 100)
            colour = style.get("colour")
            text_object.setFillColor(HexColor(colour["hex"]) if isinstance(colour, dict) and "hex" in colour
                                     else HexColor("#000000"))
            text_object.textOut(text)
            canvas.drawText(text_object)
            canvas.restoreState()
            x += styled_width(text, style, factor)


def render_overlay(electorate: str, candidates: list[dict[str, str]], rendering: dict,
                   page_size: tuple[float, float], logo_dir: Path, candidate_counts: list[dict]) -> BytesIO:
    """Draw the district name, the legends of the candidates' recommendations, the candidate rows and any candidate
    count on a transparent page, to be laid over the base ballot PDF."""
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
    draw_with_style_reference(canvas, f"{electorate.upper()} DISTRICT", rendering["district_text"])
    if rendering.get("recommendations"):
        draw_legends(canvas, rendering["recommendations"], candidates, page_height)
    if rendering.get("replaceable_electorate_name"):
        draw_electorate_name_block(canvas, rendering["replaceable_electorate_name"], electorate)
    for candidate_count in candidate_counts:
        draw_with_style_reference(canvas, str(len(candidates)), candidate_count)
    canvas.setLineWidth(line_width)
    canvas.setStrokeColor(outline)
    for i, candidate in enumerate(candidates):
        top = box["top_pt"] + i * spacing
        bottom = page_height - top - height  # ReportLab measures y up from the bottom of the page
        if bottom < 0:
            print(f"Warning: no room on the page for {candidate['Candidate']} in {electorate}", file=sys.stderr)
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
    parser.add_argument("--electorate", help="render only this electorate, eg Footscray (default: all)")
    parser.add_argument("--house", required=True,
                        help="the house whose candidates to render, eg 'Legislative Assembly'")
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

    electorates = read_electorates(args.input, args.house, args.electorate)
    if not electorates:
        sys.exit(f"No matching electorate in {args.input} has a {SCORE_COLUMN}")

    with pdfplumber.open(args.base_pdf) as base:
        existing = [line["text"] for line in base.pages[0].extract_text_lines()
                    if line["text"].strip().endswith(" DISTRICT")]
        # Without a candidate_count in the JSON, the base's counts are left as they are. The numbers are being changed
        # in the base, so match the base rather than the reference ballot.
        candidate_counts = [find_candidate_count(base.pages[0], candidate_count["box_text"])
                            for candidate_count in rendering.get("candidate_count", [])]
    if existing:
        print(f"Warning: {args.base_pdf} already has {existing[0]!r}; each district will be drawn over it",
              file=sys.stderr)

    replaceable = rendering.get("replaceable_electorate_name")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for i, (electorate, candidates) in enumerate(electorates.items()):
        writer = PdfWriter()
        base_page = writer.add_page(PdfReader(args.base_pdf).pages[0])
        page_size = (float(base_page.mediabox.width), float(base_page.mediabox.height))
        # The block naming the electorate, eg "Kew is a Red Light electorate!", is redrawn with each electorate's name.
        if replaceable and not remove_block_text(base_page, replaceable["block"]) and i == 0:
            print(f'Warning: "{replaceable["block"]["text"]}" was not found in {args.base_pdf} to be removed; '
                  "drawing each electorate's version where it was", file=sys.stderr)
        for candidate_count in candidate_counts:
            font = register_font(candidate_count["font_faces"][0])
            make_room_for_number(base_page, candidate_count["box_text"], candidate_count["text"],
                                 pdfmetrics.stringWidth(str(len(candidates)), font, 1000))
        overlay = render_overlay(electorate, candidates, rendering, page_size, args.logo_dir, candidate_counts)
        base_page.merge_page(PdfReader(overlay).pages[0])

        output = args.output_dir / f"{electorate}.pdf"
        with output.open("wb") as f:
            writer.write(f)
        print(f"Wrote {len(candidates)} candidates for {electorate} to {output}")


if __name__ == "__main__":
    main()
