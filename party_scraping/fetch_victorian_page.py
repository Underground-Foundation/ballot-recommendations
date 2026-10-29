#!/usr/bin/env python3
"""Download the VEC's list of currently registered Victorian parties and save its HTML locally."""

import argparse
import sys
import urllib.request
from pathlib import Path

DEFAULT_URL = (
    "https://www.vec.vic.gov.au/candidates-and-parties/registered-political-parties/"
    "currently-registered-parties"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "Currently registered parties - VEC.html"
# The VEC serves the full page (party names, logos) as static HTML, so no browser is needed,
# but it's polite to look like one.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
)


def fetch(url: str, user_agent: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset)


def scrape(url: str, output: Path, user_agent: str, timeout: float) -> None:
    try:
        html = fetch(url, user_agent, timeout)
    except OSError as e:
        sys.exit(f"Could not download {url}: {e}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    print(f"Saved {url} to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="Page to download (default: %(default)s)")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                        help="Where to save the HTML (default: %(default)s)")
    parser.add_argument("-u", "--user-agent", default=DEFAULT_USER_AGENT,
                        help="User-Agent string to send (default: a desktop Firefox)")
    parser.add_argument("--timeout", type=float, default=30, help="Request timeout in seconds")
    args = parser.parse_args()

    scrape(args.url, args.output, args.user_agent, args.timeout)


if __name__ == "__main__":
    main()
