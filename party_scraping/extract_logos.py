#!/usr/bin/env python3
"""Save each party's logo from the downloaded VEC registered-parties page, named after the party."""

import argparse
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

DEFAULT_INPUT = Path(__file__).resolve().parent / "Currently registered parties - VEC.html"
DEFAULT_LOGO_DIR = Path(__file__).resolve().parent.parent / "party_logos"
# Logo links in the page are relative to the VEC site.
DEFAULT_BASE_URL = "https://www.vec.vic.gov.au/"
DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


def safe_filename(name: str) -> str:
    """Collapse whitespace and drop characters that aren't allowed in filenames."""
    name = re.sub(r"\s+", " ", name).strip()
    return re.sub(r'[/\\:*?"<>|]', "", name)


def extract_logos(html: str, base_url: str) -> list[tuple[str, str]]:
    """Return (party name, absolute logo URL) for each row of the parties table."""
    table = BeautifulSoup(html, "html.parser").find("main").find("table")
    logos = []
    for row in table.find("tbody").find_all("tr"):
        # Some rows wrap the <img> in a second <strong>, so the first one is the name.
        strong, img = row.find("strong"), row.find("img")
        if strong is None or img is None:
            print(f"Warning: skipping row without a name or logo: {row.get_text(' ', strip=True)[:60]!r}",
                  file=sys.stderr)
            continue
        # Drop the query string (resize parameters) to get the original-size image.
        src = urljoin(base_url, img["src"]).split("?")[0]
        logos.append((strong.get_text(" ", strip=True), src))
    return logos


def download(url: str, destination: Path, user_agent: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=30) as response:
        destination.write_bytes(response.read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT,
                        help="HTML page saved by fetch_victorian_page.py (default: %(default)s)")
    parser.add_argument("-o", "--output-dir", type=Path, default=DEFAULT_LOGO_DIR,
                        help="Directory to save logos into (default: %(default)s)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="Site the page came from, for resolving logo links (default: %(default)s)")
    parser.add_argument("-u", "--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent string to send")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, url in extract_logos(args.input.read_text(encoding="utf-8"), args.base_url):
        destination = args.output_dir / (safe_filename(name) + Path(urlparse(url).path).suffix)
        try:
            download(url, destination, args.user_agent)
        except OSError as e:
            print(f"Warning: could not download logo for {name!r} from {url}: {e}", file=sys.stderr)
            continue
        print(f"Saved {name} logo to {destination}")


if __name__ == "__main__":
    main()
