"""Download the "All candidate scores" sheet of a Google Spreadsheet as a local CSV file."""

import argparse
import csv
import sys
from pathlib import Path

import gspread

from sheet import SCORES_SHEET_TITLE, add_url_argument, open_spreadsheet

DEFAULT_OUTPUT = "candidates_with_scores.csv"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_url_argument(parser)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT),
        help=f"path of the CSV file to write (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    spreadsheet = open_spreadsheet(parser, args.url)
    try:
        worksheet = spreadsheet.worksheet(SCORES_SHEET_TITLE)
    except gspread.WorksheetNotFound:
        sys.exit(f'No sheet named "{SCORES_SHEET_TITLE}" in "{spreadsheet.title}".')

    rows = worksheet.get_all_values()
    with args.output.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    print(f'Wrote {len(rows)} rows from sheet "{worksheet.title}" to {args.output}')


if __name__ == "__main__":
    main()
