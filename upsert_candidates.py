"""Insert candidates from a local CSV file into the "All candidate scores" sheet of a Google Spreadsheet.

The sheet is created (with the CSV's columns, plus a Recommendation column) if it doesn't exist. A candidate is
identified by the combination of Electorate, Candidate and Party; candidates already in the sheet are left untouched.
New rows are inserted so that the sheet stays sorted by Electorate, then Candidate, then Party.
"""

import argparse
import bisect
import csv
import sys
from pathlib import Path

import gspread

from sheet import SCORES_SHEET_TITLE, add_url_argument, get_or_create_worksheet, open_spreadsheet

DEFAULT_INPUT = "candidates.csv"
KEY_COLUMNS = ("Electorate", "Candidate", "Party")
# Added to a newly created sheet even if the CSV lacks it.
RECOMMENDATION_COLUMN = "Recommendation"

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


def add_electorate_borders(
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_url_argument(parser)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(DEFAULT_INPUT),
        help=f"path of the CSV file to read (default: {DEFAULT_INPUT})",
    )
    args = parser.parse_args()

    local_columns, local_rows = read_candidates(args.input)
    new_sheet_columns = local_columns + [c for c in (RECOMMENDATION_COLUMN,) if c not in local_columns]
    spreadsheet = open_spreadsheet(parser, args.url)
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
    if existing_keys != sorted(existing_keys):
        print("Warning: the sheet isn't sorted by Electorate, Candidate, Party; new rows may be out of order.")

    # Candidates not yet in the sheet, deduplicated and sorted.
    seen = set(existing_keys)
    new_rows: list[tuple[Key, list[str]]] = []
    for row in local_rows:
        key = sort_key(identity(row))
        if key in seen:
            continue
        seen.add(key)
        new_rows.append((key, [(row.get(c) or "").strip() for c in header]))
    new_rows.sort(key=lambda item: item[0])

    # Group the new rows by where they belong among the existing (data) rows.
    groups: dict[int, list[tuple[Key, list[str]]]] = {}
    for key, values in new_rows:
        groups.setdefault(bisect.bisect_right(existing_keys, key), []).append((key, values))

    # The sheet's data rows once the new rows are inserted, in order.
    final_keys: list[Key] = []
    for position in range(len(existing_keys) + 1):
        final_keys.extend(key for key, _ in groups.get(position, []))
        if position < len(existing_keys):
            final_keys.append(existing_keys[position])

    if not new_rows:
        print("No new candidates to insert.")
        add_electorate_borders(spreadsheet, worksheet, final_keys, len(header))
        return

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
    if not created:
        spreadsheet.batch_update({"requests": insert_requests})

    # Fill the blank rows; each group has shifted down by the rows inserted above it.
    value_updates = []
    shift = 0
    for position, group in sorted(groups.items()):
        first_row = position + shift + 2  # 1-based, after the header
        value_updates.append({"range": f"A{first_row}", "values": [values for _, values in group]})
        shift += len(group)
    worksheet.batch_update(value_updates, value_input_option="USER_ENTERED")
    add_electorate_borders(spreadsheet, worksheet, final_keys, len(header))

    print(f"Inserted {len(new_rows)} new candidate(s); {len(existing_rows)} were already present.")


if __name__ == "__main__":
    main()
