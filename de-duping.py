"""Find and delete duplicate candidates in the "All candidate scores" sheet of a Google Spreadsheet.

Two rows are the same candidate if they have the same Electorate and Party (ignoring case) and their Candidate names
are identical or similar (see similar_names in update_remote_candidates.py, which uses the same rule), e.g. "Adrian King" and
"Adrian William King". Rows are paired off greedily, most shared words first, so a row is in at most one pair per
run. Of each pair, the row with more empty cells is deleted (the lower row, if they have as many). The planned
deletions are listed and must be confirmed (or pass --yes) before anything is deleted.
"""

import argparse
import sys

import gspread

from sheet import SCORES_SHEET_TITLE, add_url_argument, open_spreadsheet
from update_remote_candidates import KEY_COLUMNS, Key, add_electorate_cell_borders, identity, get_name_words, are_names_similar, sort_key



def duplicate_pairs(keys: list[Key]) -> list[tuple[int, int]]:
    """Pairs of row indices that are the same candidate, each row in at most one pair."""
    electorate, candidate, party = (KEY_COLUMNS.index(c) for c in ("Electorate", "Candidate", "Party"))
    groups: dict[tuple[str, str], list[int]] = {}
    for i, key in enumerate(keys):
        groups.setdefault((key[electorate], key[party]), []).append(i)

    scored = []  # (score, i, j); an identical name beats any number of shared words
    for rows in groups.values():
        for a, i in enumerate(rows):
            for j in rows[a + 1:]:
                if keys[i][candidate] == keys[j][candidate]:
                    scored.append((float("inf"), i, j))
                elif are_names_similar(keys[i][candidate], keys[j][candidate]):
                    overlap = len(get_name_words(keys[i][candidate]) & get_name_words(keys[j][candidate]))
                    scored.append((overlap, i, j))
    scored.sort(key=lambda s: (-s[0], s[1], s[2]))

    pairs, used = [], set()
    for _, i, j in scored:
        if i not in used and j not in used:
            pairs.append((i, j))
            used.update((i, j))
    return sorted(pairs)


def count_empty_cells(row: list[str], width: int) -> int:
    return sum(1 for j in range(width) if j >= len(row) or not row[j].strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_url_argument(parser)
    parser.add_argument("-y", "--yes", action="store_true", help="delete without asking for confirmation")
    args = parser.parse_args()

    spreadsheet = open_spreadsheet(parser, args.url)
    try:
        worksheet = spreadsheet.worksheet(SCORES_SHEET_TITLE)
    except gspread.WorksheetNotFound:
        sys.exit(f'Sheet "{SCORES_SHEET_TITLE}" not found in "{spreadsheet.title}"')

    header, *rows = worksheet.get_all_values() or [[]]
    missing = [c for c in KEY_COLUMNS if c not in header]
    if missing:
        sys.exit(f'Sheet "{worksheet.title}" is missing required column(s): {", ".join(missing)}')
    records = [dict(zip(header, row)) for row in rows]
    keys = [sort_key(identity(r)) for r in records]

    to_delete = []
    for i, j in duplicate_pairs(keys):
        delete, keep = (j, i) if count_empty_cells(rows[j], len(header)) >= count_empty_cells(rows[i], len(header)) else (i, j)
        to_delete.append(delete)
        electorate, kept_name, party = identity(records[keep])
        print(
            f'Row {delete + 2}: deleting "{records[delete]["Candidate"].strip()}", keeping row {keep + 2} '
            f'"{kept_name}" ({electorate}, {party})'
        )

    if not to_delete:
        print("No duplicate candidates found.")
        return
    if not args.yes:
        try:
            answer = input(f"Delete these {len(to_delete)} row(s)? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing deleted.")
            return

    # Delete bottom-up, so that earlier row indices are unaffected; the +1 skips the header row.
    requests = [
        {"deleteDimension": {"range": {"sheetId": worksheet.id, "dimension": "ROWS", "startIndex": i + 1, "endIndex": i + 2}}}
        for i in sorted(to_delete, reverse=True)
    ]
    spreadsheet.batch_update({"requests": requests})
    deleted = set(to_delete)
    remaining = [key for i, key in enumerate(keys) if i not in deleted]
    add_electorate_cell_borders(spreadsheet, worksheet, remaining, len(header))
    print(f"Deleted {len(to_delete)} duplicate row(s); {len(remaining)} candidate(s) remain.")


if __name__ == "__main__":
    main()
