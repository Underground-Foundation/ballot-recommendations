"""Find candidate text in a PDF and print, as JSON, its font details (colour, size, font face and position),
the box to its left and the party text below it, plus optionally the candidate counts and any vertical text (with
the box around it), the blocks of text stating how many candidates have each recommendation and the block of text
naming the electorate, eg "Kew is a Red Light electorate!", on the same page."""

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


SHAPE_TOLERANCE = 0.5  # pt


def containing_shapes(page, x0, top, x1, bottom) -> list[dict]:
    """The smallest rect or curve that contains the given area, with any others of the same bounds, eg one filled
    and one stroked, or no shapes if none contains it."""
    tolerance = SHAPE_TOLERANCE
    containing = [
        s for s in page.rects + page.curves
        if s["x0"] <= x0 + tolerance and s["top"] <= top + tolerance
        and s["x1"] >= x1 - tolerance and s["bottom"] >= bottom - tolerance
    ]
    if not containing:
        return []
    smallest = min(containing, key=lambda s: s["width"] * s["height"])
    return [s for s in containing if all(abs(s[k] - smallest[k]) <= tolerance for k in ("x0", "top", "x1", "bottom"))]


def corner_radius(shape) -> dict | None:
    """The horizontal (x) and vertical (y) radii of a rectangle's corners: 0 for a plain rectangle, or those of a
    rectangle whose four corners are quarter ellipses (circles if the radii are equal), each drawn as a Bézier curve
    between straight sides. None for any other shape."""
    if shape["object_type"] == "rect":
        return {"x": 0.0, "y": 0.0}
    path = shape.get("path") or []
    if sum(1 for op in path if op[0] == "c") != 4 or any(op[0] not in "mlch" for op in path):
        return None
    for previous, op in zip(path, path[1:]):
        if op[0] == "c":
            start, end = previous[-1], op[-1]
            return {"x": round(abs(end[0] - start[0]), 2), "y": round(abs(end[1] - start[1]), 2)}
    return None


def describe_shapes(shapes: list[dict]) -> dict:
    """Describe shapes with the same bounds, eg a rounded rectangle filled white and another stroked green.

    The bounds are those of the path, measured from the top-left of the page as in describe_horizontal_text. A
    stroke is centred on the path, so half of its line width lies outside the bounds and half inside. To redraw
    it, draw a rectangle with these bounds and corners with the horizontal (x) and vertical (y) radii of
    corner_radius_pt, filled then stroked."""
    first = shapes[0]
    return {
        "x_pt": round(first["x0"], 2),
        "top_pt": round(first["top"], 2),
        "right_pt": round(first["x1"], 2),
        "bottom_pt": round(first["bottom"], 2),
        "width_pt": round(first["width"], 2),
        "height_pt": round(first["height"], 2),
        "corner_radius_pt": corner_radius(first),
        "fill_colours": distinct(format_colour(s["non_stroking_color"]) for s in shapes if s["fill"]),
        "stroke_colours": distinct(format_colour(s["stroking_color"]) for s in shapes if s["stroke"]),
        "line_widths_pt": distinct(round(s["linewidth"], 3) for s in shapes if s["stroke"]),
    }


def find_bounding_box(page, x0, top, x1, bottom) -> dict | None:
    """Describe the smallest rect or curve that contains the given area, and any upright text inside it."""
    tolerance = SHAPE_TOLERANCE
    shapes = containing_shapes(page, x0, top, x1, bottom)
    if not shapes:
        return None
    smallest = shapes[0]

    def inside(item) -> bool:
        return (item["x0"] >= smallest["x0"] - tolerance and item["x1"] <= smallest["x1"] + tolerance
                and item["top"] >= smallest["top"] - tolerance and item["bottom"] <= smallest["bottom"] + tolerance)

    other_text = [
        describe_horizontal_text(page, word)
        for word in page.extract_words(return_chars=True)
        if inside(word) and word["upright"]
    ]
    return {**describe_shapes(shapes), "other_text": other_text}


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


def word_size(word) -> float:
    return round(max(c["size"] for c in word["chars"]), 2)


def text_lines(page) -> list[dict]:
    """Group the page's upright words into lines: runs of words in the same size on the same baseline, split where
    the gap between words is wider than the size. Unlike pdfplumber's lines, text in different columns at about the
    same height, eg a party name beside a recommendation's paragraph, is kept apart."""
    lines = []
    words = [w for w in page.extract_words(return_chars=True) if w["upright"]]
    for word in sorted(words, key=lambda w: (round(w["chars"][0]["matrix"][5], 1), w["x0"])):
        size, baseline = word_size(word), word["chars"][0]["matrix"][5]
        line = next((l for l in lines
                     if l["size"] == size and abs(l["baseline"] - baseline) <= 0.5
                     and 0 <= word["x0"] - l["x1"] <= size), None)
        if line is None:
            lines.append({"text": word["text"], "chars": list(word["chars"]), "size": size, "baseline": baseline,
                          **{k: word[k] for k in ("x0", "x1", "top", "bottom")}})
        else:
            line["text"] += " " + word["text"]
            line["chars"] += word["chars"]
            line["x1"] = word["x1"]
            line["top"], line["bottom"] = min(line["top"], word["top"]), max(line["bottom"], word["bottom"])
    return lines


def text_blocks(page) -> list[list[dict]]:
    """Group the page's upright lines of text into blocks, ie paragraphs: runs of lines in the same size, each
    directly below the previous one and overlapping it horizontally, eg a centred paragraph wrapped over lines."""
    blocks = []
    for line in sorted(text_lines(page), key=lambda l: l["top"]):
        size = line["size"]

        def continues(block) -> bool:
            prev = block[-1]
            return (prev["size"] == size
                    and -size / 4 <= line["top"] - prev["bottom"] <= size / 2
                    and line["x0"] < prev["x1"] and line["x1"] > prev["x0"])

        block = next((b for b in blocks if continues(b)), None)
        if block is None:
            blocks.append([line])
        else:
            block.append(line)
    return blocks


def block_alignment(lines: list[dict]) -> str | None:
    """Whether the lines of a block are aligned left, centred or aligned right, or None if that can't be told."""
    if len(lines) < 2:
        return None
    # Glyphs' side bearings and kerning shift each line's ends a little, so allow a fraction of the text size.
    tolerance = max(c["size"] for line in lines for c in line["chars"]) / 5

    def aligned(edge) -> bool:
        positions = [edge(line) for line in lines]
        return max(positions) - min(positions) <= tolerance

    if aligned(lambda l: l["x0"]):
        return "left"
    if aligned(lambda l: (l["x0"] + l["x1"]) / 2):
        return "centre"
    if aligned(lambda l: l["x1"]):
        return "right"
    return None


def block_bounds(lines: list[dict]) -> tuple[float, float, float, float]:
    """The (x0, top, x1, bottom) bounds of a block of lines."""
    return (min(line["x0"] for line in lines), lines[0]["top"],
            max(line["x1"] for line in lines), lines[-1]["bottom"])


def find_recommendation_count(page, search_text: str) -> dict:
    """Describe the only block of text, eg a paragraph wrapped over several lines, that contains the given text, so
    that the block can be replaced, eg "Number Green Light candidates first (1-3)" by searching for "Number Green
    Light". The search ignores case, and any whitespace in it, including a line break, matches any whitespace.

    Also describe, so that they can be redrawn, the border around the block (the smallest shape containing it, eg a
    white rounded rectangle with a thick green outline) and the heading inside that border above the block (eg
    "GREEN"), with the background behind the heading (eg a black rectangle). The heading is described like the
    block; its background and the border are described by describe_shapes. Either may be None if there is none.

        ╭───────────────────────╮ ◄─ border: bounds of its path, with corner_radius_pt and line_widths_pt
        │  ┌─────────────────┐  │ ◄─ heading["background"]
        │  │      GREEN      │  │ ◄─ heading
        │  └─────────────────┘  │
        │     Number Green      │ ◄─ the block, ie the top-level details
        │   Light candidates    │
        │      first (1-3)      │
        ╰───────────────────────╯
    """
    wanted = " ".join(search_text.split()).casefold()
    blocks = text_blocks(page)
    found = [
        block for block in blocks
        if wanted in " ".join(" ".join(line["text"] for line in block).split()).casefold()
    ]
    if len(found) != 1:
        sys.exit(f'Expected one block of text containing "{search_text}" on page {page.page_number}, '
                 f"but found {len(found)}")
    lines = found[0]
    border_shapes = containing_shapes(page, *block_bounds(lines))
    return {
        "search_text": search_text,
        **describe_block(page, lines),
        "heading": find_heading(page, blocks, lines, border_shapes),
        "border": describe_shapes(border_shapes) if border_shapes else None,
    }


def find_heading(page, blocks, lines, border_shapes) -> dict | None:
    """Describe the nearest block of text above the given one inside its border, with the background behind it."""
    if not border_shapes:
        return None
    border = border_shapes[0]
    tolerance = SHAPE_TOLERANCE

    def above_inside(block) -> bool:
        x0, top, x1, bottom = block_bounds(block)
        return (block is not lines and bottom <= lines[0]["top"] + tolerance
                and x0 >= border["x0"] - tolerance and x1 <= border["x1"] + tolerance
                and top >= border["top"] - tolerance)

    heading = max((b for b in blocks if above_inside(b)), key=lambda b: b[-1]["bottom"], default=None)
    if heading is None:
        return None
    background = containing_shapes(page, *block_bounds(heading))
    # The border itself is not a background.
    if background and all(abs(background[0][k] - border[k]) <= tolerance for k in ("x0", "top", "x1", "bottom")):
        background = []
    return {
        **describe_block(page, heading),
        "background": describe_shapes(background) if background else None,
    }


def describe_block(page, lines: list[dict]) -> dict:
    """Describe the fonts and position of a block of lines of text, eg a paragraph.

    As in describe_horizontal_text, x_pt, top_pt, right_pt and bottom_pt bound the block and are measured from the
    top-left of the page, but each line's baseline_pt is measured up from the bottom of the page (PDF coordinates).
    To redraw a line, draw it at its size from (x_pt, baseline_pt), or centred on centre_x_pt if the block is centred.
    """
    chars = [c for line in lines for c in line["chars"] if not c["text"].isspace()]
    x0, top, x1, bottom = block_bounds(lines)
    baselines = [next(c for c in line["chars"] if not c["text"].isspace())["matrix"][5] for line in lines]
    matrix = chars[0]["matrix"]
    return {
        "page": page.page_number,
        "text": " ".join(line["text"].strip() for line in lines),
        "font_faces": distinct(c["fontname"] for c in chars),
        "sizes_pt": distinct(round(c["size"], 2) for c in chars),
        "colours": distinct(format_colour(c["non_stroking_color"]) for c in chars),
        # Text may be condensed, so the width isn't given by the size alone.
        "horizontal_scale": round(abs(matrix[0] / matrix[3]), 4),
        "alignment": block_alignment(lines),
        "x_pt": round(x0, 2),
        "top_pt": round(top, 2),
        "right_pt": round(x1, 2),
        "bottom_pt": round(bottom, 2),
        "width_pt": round(x1 - x0, 2),
        "height_pt": round(bottom - top, 2),
        "centre_x_pt": round((x0 + x1) / 2, 2),
        # Distance between consecutive baselines.
        "line_spacing_pt": round((baselines[0] - baselines[-1]) / (len(lines) - 1), 2) if len(lines) > 1 else None,
        "lines": [
            {
                "text": line["text"].strip(),
                "x_pt": round(line["x0"], 2),
                "width_pt": round(line["x1"] - line["x0"], 2),
                "baseline_pt": round(baseline, 2),
            }
            for line, baseline in zip(lines, baselines)
        ],
        "page_size_pt": {"width": page.width, "height": page.height},
    }


def char_style(char) -> dict:
    """The font, size, colour and horizontal scale of a character."""
    matrix = char["matrix"]
    return {
        "font_face": char["fontname"],
        "size_pt": round(char["size"], 2),
        "colour": format_colour(char["non_stroking_color"]),
        # Text may be condensed, so the width isn't given by the size alone.
        "horizontal_scale": round(abs(matrix[0] / matrix[3]), 4),
    }


def find_replaceable_electorate_name(page, name: str) -> dict:
    """Describe the electorate's name, eg "Kew", in the only block of text containing it as whole words, eg the
    sentence "Kew is a Red Light electorate!", so that the name can be replaced by another electorate's and the rest
    of the sentence moved to suit. The search is case-sensitive, so that eg "KEW DISTRICT" isn't matched, and any
    whitespace in the name, including a line break, matches any whitespace.

    The block is described as by describe_block, with its text split into runs: consecutive characters in the same
    style (font, size, colour and horizontal scale), with the name in a run of its own marked "replaceable". Each
    space between words is styled like the character before it."""
    wanted = " ".join(name.split())
    pattern = re.compile(rf"(?<!\w){re.escape(wanted)}(?!\w)")
    found = []
    for lines in text_blocks(page):
        block_text = " ".join(line["text"].strip() for line in lines)
        found += [(lines, block_text, match) for match in pattern.finditer(block_text)]
    if len(found) != 1:
        sys.exit(f'Expected "{wanted}" once, as whole words, in the text on page {page.page_number}, '
                 f"but found it {len(found)} times")
    lines, block_text, match = found[0]

    # The style of each character of the block's text, taking the characters drawn in turn.
    chars = iter(c for line in lines for c in line["chars"] if not c["text"].isspace())
    styles, remaining, style = [], 0, None
    name_chars = []
    for i, letter in enumerate(block_text):
        if letter.isspace():
            styles.append(style)
            continue
        if remaining == 0:  # a character may draw several letters, eg a ligature
            char = next(chars)
            style, remaining = char_style(char), len(char["text"])
            if match.start() <= i < match.end():
                name_chars.append(char)
        styles.append(style)
        remaining -= 1

    runs = []
    for i, (letter, style) in enumerate(zip(block_text, styles)):
        replaceable = match.start() <= i < match.end()
        if runs and runs[-1]["replaceable"] == replaceable and runs[-1]["style"] == style:
            runs[-1]["text"] += letter
        else:
            runs.append({"text": letter, "replaceable": replaceable, "style": style})
    matrix = name_chars[0]["matrix"]
    return {
        "replaceable_text": wanted,
        "font_faces": distinct(c["fontname"] for c in name_chars),
        "sizes_pt": distinct(round(c["size"], 2) for c in name_chars),
        "colours": distinct(format_colour(c["non_stroking_color"]) for c in name_chars),
        "horizontal_scale": round(abs(matrix[0] / matrix[3]), 4),
        "block": {
            **describe_block(page, lines),
            "runs": [{"text": run["text"], "replaceable": run["replaceable"], **run["style"]} for run in runs],
        },
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
    parser.add_argument("--recommendation", metavar=("RECOMMENDATION", "TEXT"), nargs=2, action="append",
                        help="a recommendation and any text in the block of text on the same page stating how many "
                             "candidates have it, to describe so that the block can be replaced; may be repeated, "
                             "eg --recommendation G 'Number Green Light candidates'")
    parser.add_argument("--electorate-name", metavar="TEXT",
                        help="the electorate's name as written (case-sensitive) in a block of text on the same page, "
                             "to be replaced by each electorate's name when rendering, eg 'Kew' in "
                             "'Kew is a Red Light electorate!'")
    args = parser.parse_args()

    recommendations = [recommendation for recommendation, _ in args.recommendation or []]
    duplicates = sorted({r for r in recommendations if recommendations.count(r) > 1})
    if duplicates:
        parser.error(f"--recommendation given more than once for {', '.join(duplicates)}")
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
                if args.recommendation:
                    details["recommendations"] = {
                        recommendation: find_recommendation_count(page, text)
                        for recommendation, text in args.recommendation
                    }
                if args.electorate_name:
                    details["replaceable_electorate_name"] = find_replaceable_electorate_name(
                        page, args.electorate_name)
                matches.append(details)

    if not matches:
        sys.exit(f'"{args.candidate_text}" not found in {args.pdf}')

    print(json.dumps(matches, indent=2))


if __name__ == "__main__":
    main()
