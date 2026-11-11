"""Find candidate text in a PDF and print, as JSON, its font details (colour, size, font face and position),
the box to its left and the party text below it, plus optionally the candidate counts and any vertical text (with
the box around it) on the same page."""

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


def describe_horizontal_text(page, text_box) -> dict:
    """Describe the fonts and position of some text, given its bounding box and characters.

    The position is given in points, measured as follows. top_pt is measured down from the top of the page, but
    baseline_pt is measured up from the bottom (PDF coordinates). The baseline is the line where the letters sit.
    Descending letters (eg "g"), dip below it to the bottom of the text box. On a ballot, the candidate's empty box
    (described separately, by find_left_box) is to the left of the text.

        ┌──────────────────────────────────────────────────────────────┐ ─┬─ top of page
        │                       ▲                                      │  │
        │                       │ top_pt                               │  │
        │                       ▼                                      │  │
        │     ┌─────┐     ┌───────────────┐ ◄─ top of text box         │  │
        │     │     │     │  HALL, Katie  │                            │  │
        │     │     │     │╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌│╌╌╌┬ ◄─ baseline            │  │ page_size_pt["height"]
        │     └─────┘     └───────────────┘   │ ◄─ bottom of text box  │  │
        │     empty box                       │                        │  │
        │     (left_box)                      │                        │  │
        │◄───── x_pt ────►                    │ baseline_pt            │  │
        │                                     ▼                        │  │
        └──────────────────────────────────────────────────────────────┘ ─┴─ bottom of page
        ◄─────────────────── page_size_pt["width"] ────────────────────►
    """
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


def vertical_size(char) -> float:
    """The font size of a character rotated a quarter turn. pdfminer's "size" of such a character is the advance
    along the line, but the extent across the line, like an upright character's height, is the font size."""
    return char["x1"] - char["x0"]


def vertical_lines(page) -> list[list[dict]]:
    """Group the page's characters rotated a quarter turn into lines, each in reading order. Text rotated
    anticlockwise reads upward and its later lines are to the right; text rotated clockwise is the reverse."""
    columns = {}
    for c in page.chars:
        a, b = c["matrix"][:2]
        if c["upright"] or abs(a) > 1e-6 or b == 0:
            continue
        # The characters of a line share a baseline, the vertical line through each glyph's origin.
        columns.setdefault((b > 0, round(c["matrix"][4], 1)), []).append(c)
    lines = []
    for (upward, _), chars in sorted(columns.items()):
        chars.sort(key=lambda c: c["matrix"][5], reverse=not upward)
        line = [chars[0]]
        for prev, c in zip(chars, chars[1:]):
            # Split the column where there is a gap, eg between different parties' names.
            gap = prev["top"] - c["bottom"] if upward else c["top"] - prev["bottom"]
            if gap > vertical_size(c):
                lines.append(line)
                line = []
            line.append(c)
        lines.append(line)
    return lines


def vertical_blocks(page) -> list[list[list[dict]]]:
    """Group the vertical lines into blocks of consecutive lines, in reading order, eg a party name over two lines."""
    def is_next(line, following) -> bool:
        upward = line[0]["matrix"][1] > 0
        if (following[0]["matrix"][1] > 0) != upward:
            return False
        step = (following[0]["matrix"][4] - line[0]["matrix"][4]) * (1 if upward else -1)
        overlaps = (min(c["top"] for c in following) < max(c["bottom"] for c in line)
                    and max(c["bottom"] for c in following) > min(c["top"] for c in line))
        return overlaps and 0 < step <= 2 * max(vertical_size(c) for c in line)

    lines = sorted(vertical_lines(page), key=lambda l: l[0]["matrix"][4] * (1 if l[0]["matrix"][1] > 0 else -1))
    blocks = []
    for line in lines:
        block = next((b for b in blocks if is_next(b[-1], line)), None)
        if block is None:
            blocks.append([line])
        else:
            block.append(line)
    return blocks


def find_vertical_text(page, text: str) -> list[dict] | None:
    """The characters, in reading order, of the first match of the text among the page's vertical text, where any
    whitespace in the text, including a line break, matches any whitespace."""
    wanted = " ".join(text.split())
    for block in vertical_blocks(page):
        # Each character of the block's text, with whitespace collapsed to one space, and its source character.
        sequence = []
        for line in block:
            for c in line + [None]:  # a line break is whitespace
                char_text = " " if c is None or c["text"].isspace() else c["text"]
                if char_text == " " and (not sequence or sequence[-1][0] == " "):
                    continue
                sequence.append((char_text, c))
        start = "".join(t for t, _ in sequence).find(wanted)
        if start >= 0:
            return [c for _, c in sequence[start:start + len(wanted)] if c is not None]
    return None


def find_bounding_box(page, x0, top, x1, bottom) -> dict | None:
    """Describe the smallest rect or curve that contains the given area, and any upright text inside it."""
    tolerance = 0.5
    containing = [
        s for s in page.rects + page.curves
        if s["x0"] <= x0 + tolerance and s["top"] <= top + tolerance
        and s["x1"] >= x1 - tolerance and s["bottom"] >= bottom - tolerance
    ]
    if not containing:
        return None
    smallest = min(containing, key=lambda s: s["width"] * s["height"])
    # There may be several shapes of the same size, eg one filled and one stroked.
    shapes = [s for s in containing if all(abs(s[k] - smallest[k]) <= tolerance for k in ("x0", "top", "x1", "bottom"))]

    def inside(item) -> bool:
        return (item["x0"] >= smallest["x0"] - tolerance and item["x1"] <= smallest["x1"] + tolerance
                and item["top"] >= smallest["top"] - tolerance and item["bottom"] <= smallest["bottom"] + tolerance)

    other_text = [
        describe_horizontal_text(page, word)
        for word in page.extract_words(return_chars=True)
        if inside(word) and word["upright"]
    ]
    return {
        "x_pt": round(smallest["x0"], 2),
        "top_pt": round(smallest["top"], 2),
        "right_pt": round(smallest["x1"], 2),
        "bottom_pt": round(smallest["bottom"], 2),
        "width_pt": round(smallest["width"], 2),
        "height_pt": round(smallest["height"], 2),
        "fill_colours": distinct(format_colour(s["non_stroking_color"]) for s in shapes if s["fill"]),
        "stroke_colours": distinct(format_colour(s["stroking_color"]) for s in shapes if s["stroke"]),
        "line_widths_pt": distinct(s["linewidth"] for s in shapes if s["stroke"]),
        "other_text": other_text,
    }


def describe_vertical_text(page, text: str) -> dict | None:
    """Describe the fonts and position of some text rotated a quarter turn, eg a party name on an upper house
    ballot, and the box that visually bounds it. The text may span several lines, eg "DEMOCRATIC LABOUR PARTY".

    In this diagram the letters are drawn upright, but on the page they are rotated a quarter turn anticlockwise,
    so each line reads upward and later lines are to its right. Each line's baseline (╎) is then a vertical line
    to the right of its letters, and its origin (●), where the baseline starts, is at the bottom.

    All positions are in points (1/72 inch). As in describe_horizontal_text, x_pt, top_pt, right_pt and bottom_pt
    are measured from the top-left of the page, but each line's start_pt is measured up from the bottom of the
    page (PDF coordinates). Each line's baseline_x_pt and start_pt are the origin at which to draw that line,
    rotated, at its size.

        ┌─────────────────────────────────────────────────────────────┐ ─ top of page
        │           ▲                                                 │
        │   ┌───────┼─────────────────┐ ◄ bounding_box["top_pt"]      │
        │   │       │ top_pt          │                               │
        │   │       ▼                 │                               │
        │   │   ┌───────┐             │                               │
        │   │   │    Y╎ │             │                               │
        │   │   │ C╎ T╎ │             │                               │
        │   │   │ I╎ R╎ │             │                               │
        │   │   │ T╎ A╎ │             │                               │
        │   │   │ ┊╎ P╎ │             │                               │
        │   │   │ M╎ ┊╎ │             │                               │
        │   │   │ E╎ B╎ │             │                               │
        │   │   │ D● A╎ │             │                               │
        │   │   │    L● │             │                               │
        │   │   └─────┼─┘ ◄───────────┼── bottom_pt                   │
        │   │         │               │                               │
        │   │         │               │                               │
        │   │         │     R ◄───────┼── bounding_box["other_text"]  │
        │   │         │               │                               │
        │   └─────────┼───────────────┘ ◄ bounding_box["bottom_pt"]   │
        │◄─────► x_pt │                                               │
        │◄────────►   │ start_pt (of the second line)                 │
        │baseline_x_pt▼                                               │
        └─────────────────────────────────────────────────────────────┘ ─ bottom of page
    """
    matched = find_vertical_text(page, text)
    if matched is None:
        return None
    lines = {}
    for c in matched:
        lines.setdefault(round(c["matrix"][4], 1), []).append(c)
    chars = [c for c in matched if not c["text"].isspace()]
    x0, top = min(c["x0"] for c in chars), min(c["top"] for c in chars)
    x1, bottom = max(c["x1"] for c in chars), max(c["bottom"] for c in chars)
    return {
        "page": page.page_number,
        "text": " ".join("".join(c["text"] for c in line).strip() for line in lines.values()),
        "direction": "upward" if chars[0]["matrix"][1] > 0 else "downward",
        "font_faces": distinct(c["fontname"] for c in chars),
        "sizes_pt": distinct(round(vertical_size(c), 3) for c in chars),
        "colours": distinct(format_colour(c["non_stroking_color"]) for c in chars),
        "x_pt": round(x0, 2),
        "top_pt": round(top, 2),
        "right_pt": round(x1, 2),
        "bottom_pt": round(bottom, 2),
        "lines": [
            {
                "text": "".join(c["text"] for c in line).strip(),
                "baseline_x_pt": round(line[0]["matrix"][4], 2),
                "start_pt": round(next(c for c in line if not c["text"].isspace())["matrix"][5], 2),
            }
            for line in lines.values()
        ],
        "bounding_box": find_bounding_box(page, x0, top, x1, bottom),
        "page_size_pt": {"width": page.width, "height": page.height},
    }


# The word following the electorate's name on each house's ballot.
ELECTORATE_SUFFIXES = {
    "Legislative Assembly": "DISTRICT",
    "Legislative Council": "REGION",
}


def find_district_text(page, house: str) -> dict | None:
    """Describe the first line of the page that names the house's electorate, eg "FOOTSCRAY DISTRICT" for the
    Legislative Assembly or "SOUTHERN METROPOLITAN REGION" for the Legislative Council."""
    suffix = ELECTORATE_SUFFIXES[house]
    for line in page.extract_text_lines(return_chars=True):
        if re.fullmatch(rf"\S.*\s{suffix}", line["text"].strip()):
            return describe_horizontal_text(page, line)
    return None


def find_candidate_count(page, box_text: str) -> dict:
    """Describe the last number in the only line containing the given text, eg the 8 in "Number the squares from 1 to
    8" or in "them last (4-8)"."""
    lines = [line for line in page.extract_text_lines() if box_text in line["text"]]
    if len(lines) != 1:
        sys.exit(f'Expected one line containing "{box_text}" on page {page.page_number}, but found {len(lines)}')
    line = lines[0]
    numbers = [
        n for n in page.search(r"\d+", regex=True, return_chars=True)
        if n["x0"] >= line["x0"] and n["x1"] <= line["x1"] and n["top"] < line["bottom"] and n["bottom"] > line["top"]
    ]
    if not numbers:
        sys.exit(f'Expected a number in "{line["text"]}", but found none')
    last = max(numbers, key=lambda n: n["x0"])
    matrix = last["chars"][0]["matrix"]
    return {
        "box_text": box_text,
        **describe_horizontal_text(page, last),
        # Text may be condensed, eg the sidebar of the base ballot, so the width isn't given by the size alone.
        "horizontal_scale": round(abs(matrix[0] / matrix[3]), 4),
    }


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

    return describe_horizontal_text(page, {
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
    parser.add_argument("--house", choices=ELECTORATE_SUFFIXES, default="Legislative Assembly",
                        help="the house the ballot is for (default: %(default)s)")
    parser.add_argument("--candidate-text", required=True, help="candidate text to find, eg 'HALL, Katie'")
    parser.add_argument("--candidate-count", metavar="TEXT", nargs="+", action="extend",
                        help="any text unique to each line stating the candidate count as its last number, "
                             "eg 'Number the squares' 'them last'")
    parser.add_argument("--vertical-text", metavar="TEXT", nargs="+", action="extend",
                        help="any text rotated a quarter turn on the same page to describe, with the box around it, "
                             "eg 'DEMOCRATIC LABOUR PARTY'")
    args = parser.parse_args()

    if not args.pdf.is_file():
        sys.exit(f"No such file: {args.pdf}")

    matches = []
    with pdfplumber.open(args.pdf) as pdf:
        for page in pdf.pages:
            for match in page.search(args.candidate_text, regex=False, return_chars=True):
                details = {
                    "candidate_text": describe_horizontal_text(page, match),
                    "left_box": find_left_box(page, match),
                    "party_text": find_party_text(page, match),
                    "district_text": find_district_text(page, args.house),
                }
                if args.candidate_count:
                    details["candidate_count"] = [find_candidate_count(page, text) for text in args.candidate_count]
                if args.vertical_text:
                    details["vertical_text"] = []
                    for text in args.vertical_text:
                        description = describe_vertical_text(page, text)
                        if description is None:
                            sys.exit(f'Vertical text "{text}" not found on page {page.page_number} of {args.pdf}')
                        details["vertical_text"].append(description)
                matches.append(details)

    if not matches:
        sys.exit(f'"{args.candidate_text}" not found in {args.pdf}')

    print(json.dumps(matches, indent=2))


if __name__ == "__main__":
    main()
