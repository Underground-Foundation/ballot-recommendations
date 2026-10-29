"""Find candidate text in a PDF and print, as JSON, its font details (colour, size, font face and position),
the box to its left and the party text below it."""

import argparse
import json
import re
import sys
from pathlib import Path

import pdfplumber


def format_colour(colour):
    """Describe a pdfplumber colour, which is a tuple of components in the PDF's colour space."""
    if colour is None:
        return None
    components = tuple(colour) if isinstance(colour, (list, tuple)) else (colour,)
    if not all(isinstance(c, (int, float)) for c in components):
        return str(colour)  # eg a pattern
    if len(components) == 1:
        components *= 3  # greyscale
    if len(components) == 3:
        hex_colour = "#" + "".join(f"{round(c * 255):02X}" for c in components)
        return {"hex": hex_colour, "rgb": list(components)}
    if len(components) == 4:
        return {"cmyk": list(components)}
    return list(components)


def distinct(values) -> list:
    """List the distinct values, so mixed formatting within a match is visible."""
    return list({json.dumps(v, sort_keys=True): v for v in values}.values())


def describe_text(page, text_box) -> dict:
    """Describe the fonts and position of some text, given its bounding box and characters."""
    chars = [c for c in text_box["chars"] if not c["text"].isspace()]
    return {
        "page": page.page_number,
        "text": text_box["text"],
        "font_faces": distinct(c["fontname"] for c in chars),
        "sizes_pt": distinct(c["size"] for c in chars),
        "colours": distinct(format_colour(c["non_stroking_color"]) for c in chars),
        # Points from the left edge and from the top edge (top of text box).
        "x_pt": round(text_box["x0"], 2),
        "top_pt": round(text_box["top"], 2),
        # The text matrix's translation is the glyph origin on the baseline, in PDF
        # coordinates, ie points from the bottom edge.
        "baseline_pt": round(chars[0]["matrix"][5], 2),
        "page_size_pt": {"width": page.width, "height": page.height},
    }


DISTRICT_SUFFIX = "DISTRICT"


def find_district_text(page) -> dict | None:
    """Describe the first line of the page that reads "<name> DISTRICT", eg "FOOTSCRAY DISTRICT"."""
    for line in page.extract_text_lines(return_chars=True):
        if re.fullmatch(rf"\S.*\s{DISTRICT_SUFFIX}", line["text"].strip()):
            return describe_text(page, line)
    return None


def find_party_text(page, match) -> dict | None:
    """Describe the line of text immediately below the candidate text, starting at about the same left edge."""
    tolerance = 5  # pt; party names are only roughly aligned with the candidate name
    words = page.extract_words(return_chars=True)
    below = [
        w for w in words
        if w["top"] >= match["bottom"] - 1 and abs(w["x0"] - match["x0"]) <= tolerance
    ]
    if not below:
        return None
    first = min(below, key=lambda w: w["top"])

    # Gather the rest of that line: words on the same baseline that follow on with no large gap.
    line = [first]
    rest = sorted(
        (w for w in words if abs(w["bottom"] - first["bottom"]) <= 1 and w["x0"] > first["x0"]),
        key=lambda w: w["x0"],
    )
    for word in rest:
        if word["x0"] - line[-1]["x1"] > first["bottom"] - first["top"]:
            break
        line.append(word)

    return describe_text(page, {
        "text": " ".join(w["text"] for w in line),
        "chars": [c for w in line for c in w["chars"]],
        "x0": first["x0"],
        "top": min(w["top"] for w in line),
    })


def find_left_box(page, match) -> dict | None:
    """Describe the ~square box to the left of the match, whose right edge is the closest vertical line."""
    # Vertical edges (from lines, rects and curves) that end left of the text and overlap it vertically.
    candidates = [
        e for e in page.edges
        if e["orientation"] == "v"
        and e["x0"] <= match["x0"]
        and e["top"] < match["bottom"]
        and e["bottom"] > match["top"]
    ]
    if not candidates:
        return None
    right_edge = max(candidates, key=lambda e: e["x0"])
    right = right_edge["x0"]

    # The shapes forming the box share that right edge; there may be several, eg one filled and one stroked.
    tolerance = 0.5
    shapes = [
        s for s in page.rects + page.curves
        if abs(s["x1"] - right) <= tolerance
        and s["top"] < right_edge["bottom"]
        and s["bottom"] > right_edge["top"]
    ]
    if shapes:
        x0 = min(s["x0"] for s in shapes)
        top = min(s["top"] for s in shapes)
        bottom = max(s["bottom"] for s in shapes)
    else:
        # The box is drawn from separate lines: take the nearest vertical line to the left that is roughly a
        # square's width away (so the adjoining box's edge isn't mistaken for this one's left edge).
        shapes = [right_edge]
        top, bottom = right_edge["top"], right_edge["bottom"]
        lefts = [
            e for e in page.edges
            if e["orientation"] == "v"
            and e["x0"] <= right - (bottom - top) / 2
            and e["top"] < bottom
            and e["bottom"] > top
        ]
        if not lefts:
            return None
        left_edge = max(lefts, key=lambda e: e["x0"])
        shapes.append(left_edge)
        x0 = left_edge["x0"]

    below = find_box_below(page, x0, right, top, bottom)
    lefts = find_boxes_to_left(page, x0, right, top, bottom)
    return {
        "x_pt": round(x0, 2),
        "top_pt": round(top, 2),
        "right_pt": round(right, 2),
        "bottom_pt": round(bottom, 2),
        "width_pt": round(right - x0, 2),
        "height_pt": round(bottom - top, 2),
        "fill_colours": distinct(format_colour(s["non_stroking_color"]) for s in shapes if s["fill"]),
        "stroke_colours": distinct(format_colour(s["stroking_color"]) for s in shapes if s["stroke"]),
        "line_widths_pt": distinct(s["linewidth"] for s in shapes if s["stroke"]),
        # Distance from this box's top to the top of the box below it, ie the pitch between rows.
        "vertical_spacing_pt": round(below["top"] - top, 2) if below else None,
        # Distances from this box's left edge to the left edge of each box in the row to its left, in turn: the
        # first is to the nearest box, the second from that box to the next one, and so on.
        "horizontal_spacings_pt": [
            round(right_box["x0"] - left_box["x0"], 2)
            for right_box, left_box in zip([{"x0": x0}] + lefts, lefts)
        ],
    }


SIZE_TOLERANCE = 0.1  # proportion of a box's size


def similar_shapes(page, width, height) -> list[dict]:
    """The rects and curves that are about the given size."""
    return [
        s for s in page.rects + page.curves
        if abs(s["width"] - width) <= width * SIZE_TOLERANCE
        and abs(s["height"] - height) <= height * SIZE_TOLERANCE
    ]


def find_box_below(page, x0, x1, top, bottom) -> dict | None:
    """Find the nearest shape below the given box that is about the same size and overlaps it horizontally."""
    height = bottom - top
    candidates = [
        s for s in similar_shapes(page, x1 - x0, height)
        if s["x0"] < x1 and s["x1"] > x0 and s["top"] >= bottom - height * SIZE_TOLERANCE
    ]
    return min(candidates, key=lambda s: s["top"], default=None)


def find_boxes_to_left(page, x0, x1, top, bottom) -> list[dict]:
    """Find the row of shapes to the left of the given box that are about the same size, nearest first."""
    width = x1 - x0
    shapes = [s for s in similar_shapes(page, width, bottom - top) if s["top"] < bottom and s["bottom"] > top]
    boxes = []
    while True:
        left = max((s for s in shapes if s["x1"] <= x0 + width * SIZE_TOLERANCE), key=lambda s: s["x0"], default=None)
        if left is None:
            return boxes
        boxes.append(left)
        x0 = left["x0"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="path of the PDF file to inspect, eg Footscray.pdf")
    parser.add_argument("--candidate-text", required=True, help="candidate text to find, eg 'HALL, Katie'")
    args = parser.parse_args()

    if not args.pdf.is_file():
        sys.exit(f"No such file: {args.pdf}")

    matches = []
    with pdfplumber.open(args.pdf) as pdf:
        for page in pdf.pages:
            for match in page.search(args.candidate_text, regex=False, return_chars=True):
                matches.append({
                    "candidate_text": describe_text(page, match),
                    "left_box": find_left_box(page, match),
                    "party_text": find_party_text(page, match),
                    "district_text": find_district_text(page),
                })

    if not matches:
        sys.exit(f'"{args.candidate_text}" not found in {args.pdf}')

    print(json.dumps(matches, indent=2))


if __name__ == "__main__":
    main()
