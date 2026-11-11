"""Update the "All candidate scores" sheet of a Google Spreadsheet with the candidates from a local CSV file.

The sheet is created (with the CSV's columns, plus a Recommendation column) if it doesn't exist. A candidate is
identified by the combination of Electorate, Candidate and Party, ignoring case. A CSV candidate with no exact match is
also treated as already in the sheet if a candidate of the same party in the same electorate has a similar name (see
similar_names), e.g. "Adrian William King" and "Adrian King", since sources often spell names differently. For candidates
already in the sheet, only blank cells are filled in (from the CSV); existing values are never overwritten, except
that --replace-names replaces the sheet's spelling of a candidate's name with the CSV's. New rows are inserted so that
the sheet stays sorted by Electorate, then Candidate, then Party.

With --sort (which requires --with-candidate-removal), the sheet's rows are then reordered to follow the CSV's row
order. Rows the CSV lacks that survive the removal (those in electorates the CSV has no candidates for) go after the
CSV's rows, in their existing order.

With --with-candidate-removal, rows of the sheet whose candidate isn't in the CSV are deleted, but only in electorates
that the CSV has candidates for; rows in other electorates are left alone.
"""

import argparse
import bisect
import csv
import difflib
import sys
import unicodedata
from pathlib import Path

import gspread
from gspread.utils import rowcol_to_a1

from sheet import SCORES_SHEET_TITLE, add_url_argument, get_or_create_worksheet, open_spreadsheet

DEFAULT_INPUT = "candidates.csv"
KEY_COLUMNS = ("Electorate", "Candidate", "Party")
# Added to a newly created sheet even if the CSV lacks it.
RECOMMENDATION_COLUMN = "Recommendation"
# How alike (from 0 to 1) two words must be spelt to count as the same word in a name, e.g. "Coombs" and "Coombes"
# (0.92) or "Ollie" and "Oliver" (0.73). Different people's names in the same seat and party, such as "Milton" and
# "Leanne", score well below this.
SIMILAR_SPELLING = 0.7

Key = tuple[str, ...]


def read_candidates(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Return the CSV's column names and its rows."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            columns = list(reader.fieldnames or [])
    except FileNotFoundError:
        sys.exit(f"Input file not found: {path}")

    missing = [c for c in KEY_COLUMNS if c not in columns]
    if missing:
        sys.exit(f"{path} is missing required column(s): {', '.join(missing)}")
    return columns, rows


def identity(row: dict[str, str]) -> Key:
    """The values that identify a candidate."""
    return tuple((row.get(c) or "").strip() for c in KEY_COLUMNS)


def sort_key(key: Key) -> Key:
    return tuple(v.casefold() for v in key)


def get_name_words(name: str) -> set[str]:
    """The words of a name, ignoring case, accents and surrounding punctuation ("K." -> "k", "Dolejší" -> "dolejsi")."""
    name = "".join(c for c in unicodedata.normalize("NFKD", name.casefold()) if not unicodedata.combining(c))
    return {w for w in (word.strip(".,'\"()’") for word in name.split()) if w}


def word_fits(word: str, others: set[str]) -> bool:
    """Whether a word is part of one of the others ("tim" in "timothy") or spelt like one of them."""
    return any(
        word in other or difflib.SequenceMatcher(None, word, other).ratio() >= SIMILAR_SPELLING for other in others
    )


def is_covered(a: set[str], b: set[str]) -> bool:
    """Whether every word of a is in b, part of a word of b, or spelt like a word of b."""
    return all(a_word in b or word_fits(a_word, b) for a_word in a)


def are_names_similar(name: str, other: str) -> bool:
    """Whether two names are probably the same person's: every word of the name with fewer words (either, if they have
    as many) is in the other name, part of a word in it, or spelt like a word in it. So "Adrian King" is like "Adrian
    William King", "Tim Gooden" like "Timothy Gooden" and "Gary Coombs" like "Gary Coombes", but "Leanne Wilde" is
    not like "Milton Wilde"."""
    a, b = get_name_words(name), get_name_words(other)
    if not a or not b:
        return False
    if len(a) == len(b):
        return is_covered(a, b) or is_covered(b, a)
    return is_covered(a, b) if len(a) < len(b) else is_covered(b, a)


def match_existing(local_keys: list[Key], existing_keys: list[Key]) -> dict[int, int]:
    """Map each CSV row (by index) that is already in the sheet to its existing row's index.

    A CSV row matches an existing row with the same key exactly. Failing that, it matches an existing row in the same
    electorate with the same party whose name is similar (see similar_names), preferring the most shared words.
    Each existing row is matched by at most one such similar name, and never if it already has an exact match.
    """
    electorate, candidate, party = (KEY_COLUMNS.index(c) for c in ("Electorate", "Candidate", "Party"))
    exact: dict[Key, int] = {}
    for i, key in enumerate(existing_keys):
        exact.setdefault(key, i)

    matches: dict[int, int] = {}
    for n, key in enumerate(local_keys):
        if key in exact:
            matches[n] = exact[key]
    claimed = set(matches.values())

    for n, key in enumerate(local_keys):
        if n in matches:
            continue
        words = get_name_words(key[candidate])
        best, best_overlap = None, -1
        for i, existing in enumerate(existing_keys):
            if i in claimed or existing[electorate] != key[electorate] or existing[party] != key[party]:
                continue
            if not are_names_similar(key[candidate], existing[candidate]):
                continue
            overlap = len(words & get_name_words(existing[candidate]))
            if overlap > best_overlap:
                best, best_overlap = i, overlap
        if best is not None:
            matches[n] = best
            claimed.add(best)
    return matches


def add_electorate_cell_borders(
    spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet, keys: list[Key], column_count: int
) -> None:
    """Draw a border under the last row of each electorate, clearing any other horizontal borders between data rows.

    Clearing matters because inserted rows inherit the formatting (including borders) of the row above them.
    """
    if not keys:
        return

    def border_range(start: int, end: int) -> dict:
        # Data rows start at index 1, after the header.
        return {
            "sheetId": worksheet.id,
            "startRowIndex": start + 1,
            "endRowIndex": end + 1,
            "startColumnIndex": 0,
            "endColumnIndex": column_count,
        }

    none = {"style": "NONE"}
    requests = [{"updateBorders": {"range": border_range(0, len(keys)), "bottom": none, "innerHorizontal": none}}]
    for i, key in enumerate(keys):
        if i + 1 == len(keys) or keys[i + 1][0] != key[0]:
            requests.append({"updateBorders": {"range": border_range(i, i + 1), "bottom": {"style": "SOLID"}}})
    spreadsheet.batch_update({"requests": requests})


def sort_rows(spreadsheet: gspread.Spreadsheet, worksheet: gspread.Worksheet, order: list[int]) -> None:
    """Reorder the sheet's data rows so that the row now at index order[k] ends up at index k.

    Each row's rank is written to a temporary column, which the rows are then sorted by and which is then deleted, all
    in one atomic batch. Moving whole rows (rather than rewriting their values) keeps their formulas and formatting.
    """
    rank_column = worksheet.col_count  # 0-based index of the column about to be appended
    ranks = [0] * len(order)
    for k, i in enumerate(order):
        ranks[i] = k
    data_range = {"sheetId": worksheet.id, "startRowIndex": 1, "endRowIndex": len(order) + 1}
    spreadsheet.batch_update(
        {
            "requests": [
                {"appendDimension": {"sheetId": worksheet.id, "dimension": "COLUMNS", "length": 1}},
                {
                    "updateCells": {
                        "range": {**data_range, "startColumnIndex": rank_column, "endColumnIndex": rank_column + 1},
                        "rows": [{"values": [{"userEnteredValue": {"numberValue": r}}]} for r in ranks],
                        "fields": "userEnteredValue",
                    }
                },
                {
                    "sortRange": {
                        "range": {**data_range, "startColumnIndex": 0, "endColumnIndex": rank_column + 1},
                        "sortSpecs": [{"dimensionIndex": rank_column, "sortOrder": "ASCENDING"}],
                    }
                },
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": worksheet.id,
                            "dimension": "COLUMNS",
                            "startIndex": rank_column,
                            "endIndex": rank_column + 1,
                        }
                    }
                },
            ]
        }
    )


def update_remote_candidates(
    spreadsheet: gspread.Spreadsheet,
    input_path: Path,
    replace_names: bool = False,
    remove_candidates: bool = False,
    sort: bool = False,
) -> None:
    """Insert the candidates from the CSV at input_path into the scores sheet of the spreadsheet, creating the sheet if
    needed, fill in the blank cells of candidates already there and, if remove_candidates, delete the sheet's
    candidates that the CSV lacks in the CSV's electorates (see the module docstring)."""
    local_columns, local_rows = read_candidates(input_path)
    new_sheet_columns = local_columns + [c for c in (RECOMMENDATION_COLUMN,) if c not in local_columns]
    worksheet, created = get_or_create_worksheet(
        spreadsheet, SCORES_SHEET_TITLE, rows=len(local_rows) + 1, cols=len(new_sheet_columns)
    )

    if created:
        header, existing_rows = new_sheet_columns, []
        worksheet.update([header], "A1")
        worksheet.freeze(rows=1)
        print(f'Created sheet "{worksheet.title}" in "{spreadsheet.title}" ({worksheet.url})')
    else:
        header, *existing_rows = worksheet.get_all_values() or [[]]
        missing = [c for c in KEY_COLUMNS if c not in header]
        if missing:
            sys.exit(f'Sheet "{worksheet.title}" is missing required column(s): {", ".join(missing)}')
        ignored = [c for c in local_columns if c not in header]
        if ignored:
            print(f"Ignoring column(s) not present in the sheet: {', '.join(ignored)}")
        print(f'Found existing sheet "{worksheet.title}" in "{spreadsheet.title}" ({worksheet.url})')

    existing_keys = [sort_key(identity(dict(zip(header, row)))) for row in existing_rows]
    if not sort and existing_keys != sorted(existing_keys):
        print("Warning: the sheet isn't sorted by Electorate, Candidate, Party; new rows may be out of order.")

    local_keys = [sort_key(identity(row)) for row in local_rows]
    matches = match_existing(local_keys, existing_keys)
    candidate_column = header.index("Candidate")
    for n, i in sorted(matches.items()):
        electorate, local_name, party = identity(local_rows[n])
        existing_name = existing_rows[i][candidate_column].strip()
        if local_keys[n] != existing_keys[i]:
            print(f'Treating "{local_name}" as the existing "{existing_name}" ({electorate}, {party})')

    # Existing rows to delete: those in the CSV's electorates that no CSV row matches. A row whose key is in the CSV is
    # kept even if unmatched (i.e. it duplicates another row), so as not to lose anything entered in it.
    removed: set[int] = set()
    if remove_candidates:
        electorate_column = KEY_COLUMNS.index("Electorate")
        local_electorates = {key[electorate_column] for key in local_keys}
        local_key_set = set(local_keys)
        matched = set(matches.values())
        for i, key in enumerate(existing_keys):
            if key[electorate_column] in local_electorates and i not in matched and key not in local_key_set:
                removed.add(i)
                record = dict(zip(header, existing_rows[i]))
                electorate, name, party = identity(record)
                recommendation = (record.get(RECOMMENDATION_COLUMN) or "").strip()
                print(
                    f'Removing "{name}" ({electorate}, {party}), who isn\'t in {input_path}; '
                    + (f'their recommendation was "{recommendation}"' if recommendation else "they had no recommendation")
                )

    # Candidates not yet in the sheet, deduplicated and sorted.
    seen: set[Key] = set()
    new_rows: list[tuple[Key, list[str]]] = []
    new_row_index: dict[Key, int] = {}  # each new row's (first) CSV row index
    for n, row in enumerate(local_rows):
        key = local_keys[n]
        if n in matches or key in seen:
            continue
        seen.add(key)
        new_row_index[key] = n
        new_rows.append((key, [(row.get(c) or "").strip() for c in header]))
        electorate, name, party = identity(row)
        print(f'Inserting "{name}" ({electorate}, {party})')
    new_rows.sort(key=lambda item: item[0])

    # Group the new rows by where they belong among the existing (data) rows.
    groups: dict[int, list[tuple[Key, list[str]]]] = {}
    for key, values in new_rows:
        groups.setdefault(bisect.bisect_right(existing_keys, key), []).append((key, values))

    # The sheet's data rows once the new rows are inserted, in order.
    # With --sort, each row also gets a rank giving its place in the CSV's order: a row the CSV has comes at its (first)
    # CSV row; an unmatched row whose key the CSV has (a duplicate) comes just after that CSV row; and any other row
    # (in an electorate that the CSV lacks) comes after all of the CSV's rows, in its current order.
    first_local_index: dict[Key, int] = {}
    for n, key in enumerate(local_keys):
        first_local_index.setdefault(key, n)
    matched_local_index: dict[int, int] = {}
    for n, i in matches.items():
        matched_local_index[i] = min(n, matched_local_index.get(i, n))
    final_keys: list[Key] = []
    final_ranks: list[tuple[int, int, int]] = []
    for position in range(len(existing_keys) + 1):
        for key, _ in groups.get(position, []):
            final_keys.append(key)
            final_ranks.append((new_row_index[key], 0, 0))
        if position < len(existing_keys) and position not in removed:
            key = existing_keys[position]
            final_keys.append(key)
            if position in matched_local_index:
                final_ranks.append((matched_local_index[position], 0, 0))
            elif key in first_local_index:
                final_ranks.append((first_local_index[key], 1, position))
            else:
                final_ranks.append((len(local_rows), 0, position))

    # Blank cells of existing rows that the CSV can fill in, keyed by (existing row index, column index), plus
    # candidate names to respell if --replace-names was given.
    fills: dict[tuple[int, int], str] = {}
    renamed = 0
    for n, i in sorted(matches.items()):
        row = local_rows[n]
        existing_row = existing_rows[i]
        local_name = identity(row)[KEY_COLUMNS.index("Candidate")]
        if replace_names and local_name and existing_row[candidate_column].strip() != local_name:
            if (i, candidate_column) not in fills:
                fills[(i, candidate_column)] = local_name
                renamed += 1
        for j, column in enumerate(header):
            if column in KEY_COLUMNS or (j < len(existing_row) and existing_row[j].strip()):
                continue
            value = (row.get(column) or "").strip()
            if value:
                fills.setdefault((i, j), value)

    if new_rows and not created:
        # Insert blank rows bottom-up, so that earlier insertion points are unaffected; the +1 skips the header row.
        insert_requests = [
            {
                "insertDimension": {
                    "range": {
                        "sheetId": worksheet.id,
                        "dimension": "ROWS",
                        "startIndex": position + 1,
                        "endIndex": position + 1 + len(group),
                    },
                    "inheritFromBefore": position > 0,
                }
            }
            for position, group in sorted(groups.items(), reverse=True)
        ]
        spreadsheet.batch_update({"requests": insert_requests})

    # Fill the blank rows; each group has shifted down by the rows inserted above it.
    value_updates = []
    shift = 0
    for position, group in sorted(groups.items()):
        first_row = position + shift + 2  # 1-based, after the header
        value_updates.append({"range": f"A{first_row}", "values": [values for _, values in group]})
        shift += len(group)

    # Fill in the blank cells of existing rows, at their positions after the insertions.
    inserted_up_to = []  # number of new rows inserted at or before each existing row
    running = 0
    for position in range(len(existing_keys)):
        running += len(groups.get(position, []))
        inserted_up_to.append(running)
    for (i, j), value in sorted(fills.items()):
        row_number = i + inserted_up_to[i] + 2  # 1-based, after the header
        value_updates.append({"range": rowcol_to_a1(row_number, j + 1), "values": [[value]]})

    if value_updates:
        worksheet.batch_update(value_updates, value_input_option="USER_ENTERED")

    if removed:
        # Delete bottom-up, at the rows' positions after the insertions, so that earlier rows are unaffected.
        delete_requests = []
        for i in sorted(removed, reverse=True):
            start = i + inserted_up_to[i] + 1  # 0-based, after the header
            delete_requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": worksheet.id,
                            "dimension": "ROWS",
                            "startIndex": start,
                            "endIndex": start + 1,
                        }
                    }
                }
            )
        spreadsheet.batch_update({"requests": delete_requests})

    sorted_rows = 0
    if sort:
        order = sorted(range(len(final_keys)), key=lambda p: final_ranks[p])
        sorted_rows = sum(1 for k, p in enumerate(order) if k != p)
        if sorted_rows:
            sort_rows(spreadsheet, worksheet, order)
            final_keys = [final_keys[p] for p in order]
    add_electorate_cell_borders(spreadsheet, worksheet, final_keys, len(header))

    updated_rows = len({i for i, _ in fills})
    print(
        f"Inserted {len(new_rows)} new candidate(s); {len(existing_rows)} were already present, "
        f"of which {updated_rows} had {len(fills) - renamed} blank cell(s) filled in"
        + (f" and {renamed} name(s) respelt" if replace_names else "")
        + (f"; removed {len(removed)} candidate(s) not in the CSV" if remove_candidates else "")
        + (f"; moved {sorted_rows} row(s) into the CSV's order" if sort else "")
        + "."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_url_argument(parser)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(DEFAULT_INPUT),
        help=f"path of the CSV file to read (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--replace-names",
        action="store_true",
        help="replace the sheet's spelling of an existing candidate's name with the CSV's",
    )
    parser.add_argument(
        "--with-candidate-removal",
        action="store_true",
        help="delete the sheet's candidates that aren't in the CSV, in the electorates that the CSV has candidates for",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="reorder the sheet's rows to follow the CSV's row order; requires --with-candidate-removal",
    )
    args = parser.parse_args()
    if args.sort and not args.with_candidate_removal:
        parser.error("--sort requires --with-candidate-removal")

    spreadsheet = open_spreadsheet(parser, args.url)
    update_remote_candidates(
        spreadsheet,
        args.input,
        replace_names=args.replace_names,
        remove_candidates=args.with_candidate_removal,
        sort=args.sort,
    )


if __name__ == "__main__":
    main()
