"""Set the Recommendation of every candidate of a party in the "All candidate scores" sheet of a Google Spreadsheet."""

import argparse
import sys

import gspread
from gspread.utils import rowcol_to_a1

from sheet import SCORES_SHEET_TITLE, add_url_argument, open_spreadsheet

PARTY_COLUMN = "Party"
RECOMMENDATION_COLUMN = "Recommendation"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_url_argument(parser)
    parser.add_argument("--party", required=True, help="party whose candidates to update (ignoring case), eg Liberal")
    parser.add_argument("--recommendation", required=True, help="recommendation to give them, eg R")
    args = parser.parse_args()

    spreadsheet = open_spreadsheet(parser, args.url)
    try:
        worksheet = spreadsheet.worksheet(SCORES_SHEET_TITLE)
    except gspread.WorksheetNotFound:
        sys.exit(f'No sheet named "{SCORES_SHEET_TITLE}" in "{spreadsheet.title}".')

    header, *rows = worksheet.get_all_values() or [[]]
    missing = [c for c in (PARTY_COLUMN, RECOMMENDATION_COLUMN) if c not in header]
    if missing:
        sys.exit(f'Sheet "{worksheet.title}" is missing required column(s): {", ".join(missing)}')
    party_index, recommendation_index = header.index(PARTY_COLUMN), header.index(RECOMMENDATION_COLUMN)

    party = args.party.strip().casefold()
    matching = 0
    updates = []
    for row_number, row in enumerate(rows, start=2):  # 1-based, after the header
        cells = row + [""] * (len(header) - len(row))
        if cells[party_index].strip().casefold() != party:
            continue
        matching += 1
        if cells[recommendation_index] != args.recommendation:
            updates.append({"range": rowcol_to_a1(row_number, recommendation_index + 1),
                            "values": [[args.recommendation]]})

    if not matching:
        sys.exit(f'No candidates of party "{args.party}" in sheet "{worksheet.title}".')
    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
    print(f'Set {RECOMMENDATION_COLUMN} to "{args.recommendation}" for {len(updates)} of the {matching} candidate(s) '
          f'of party "{args.party}" ({matching - len(updates)} already had it).')


if __name__ == "__main__":
    main()
