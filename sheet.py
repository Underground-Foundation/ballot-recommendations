"""Shared logic for accessing the candidate-scores Google Spreadsheet."""

import argparse
import os
import sys
from pathlib import Path

import gspread
from dotenv import load_dotenv

SCORES_SHEET_TITLE = "All candidate scores"


def load_client() -> gspread.Client:
    """Authenticate as the service account whose key file is named by DEFAULT_GOOGLE_KEY."""
    load_dotenv()
    key = os.environ.get("DEFAULT_GOOGLE_KEY")
    if not key:
        sys.exit("DEFAULT_GOOGLE_KEY is not set (expected in .env).")
    key_path = Path(key).expanduser()
    if not key_path.is_file():
        sys.exit(f"Service-account key file not found: {key_path.resolve()}")
    return gspread.service_account(filename=key_path)


def add_url_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--url",
        nargs="?",
        type=str,
        help="URL of the Google Sheet (defaults to SPREADSHEET_URL from .env)",
    )


def open_spreadsheet(parser: argparse.ArgumentParser, url: str | None) -> gspread.Spreadsheet:
    """Open the spreadsheet at the given URL, falling back to SPREADSHEET_URL from .env."""
    client = load_client()
    url = url or os.environ.get("SPREADSHEET_URL")
    if not url:
        parser.error("pass a spreadsheet URL or set SPREADSHEET_URL in .env")

    try:
        return client.open_by_url(url)
    except (gspread.SpreadsheetNotFound, PermissionError):
        sys.exit(
            "Could not open the spreadsheet. Check the URL, and that it is shared (as Editor) with the "
            f"service account ({client.http_client.auth.service_account_email})."
        )


def get_or_create_worksheet(
    spreadsheet: gspread.Spreadsheet, title: str, rows: int = 1000, cols: int = 26
) -> tuple[gspread.Worksheet, bool]:
    """Return the worksheet with the given title, creating it if absent, and whether it was created."""
    try:
        return spreadsheet.worksheet(title), False
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols), True
