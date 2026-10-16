#!/usr/bin/env python3
"""Capture the public GitHub monthly Trending page as raw discovery evidence.

Run ``python3 scripts/trending.py`` for JSON on stdout or pass
``--output data/trending-monthly.json`` for an atomic file update. Entries are
the page's repositories in page order. This command does not decide whether
they are AI-related, genuinely open source, or fit for the curated catalog.
GitHub does not offer this Trending page as an official API, so malformed or
changed HTML is an error rather than a reason to silently publish empty data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if __package__:
    from .refresh import CatalogError, _fallback_ssl_context, _validate_repo_slug
else:
    from refresh import CatalogError, _fallback_ssl_context, _validate_repo_slug


SOURCE_URL = "https://github.com/trending?since=monthly"
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
MONTHLY_STARS_RE = re.compile(rf"\s*({NUMBER})\s+stars?\s+this\s+month\s*\Z", re.I)
TOTAL_STARS_RE = re.compile(rf"\s*({NUMBER})\s*\Z")


class TrendingError(ValueError):
    """The monthly Trending page could not be read safely."""


def _clean_text(chunks: list[str]) -> str:
    return " ".join("".join(chunks).split())


def _slug_from_heading_href(href: str | None) -> str | None:
    if not href or not href.startswith("/") or href.endswith("/"):
        return None
    slug = href[1:]
    try:
        _validate_repo_slug(slug)
    except CatalogError:
        return None
    return slug


class _TrendingPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._seen: set[str] = set()
        self._current: dict | None = None
        self._article_depth = 0
        self._in_heading = False
        self._in_description = False
        self._in_total = False
        self._language_depth = 0
        self._monthly_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "article":
            if self._current is None and "Box-row" in classes:
                self._current = {
                    "repo": None,
                    "description_text": [],
                    "language_text": [],
                    "total_text": [],
                    "monthly_text": [],
                }
                self._article_depth = 1
                return
            if self._current is not None:
                self._article_depth += 1
        if self._current is None:
            return

        if tag == "h2":
            self._in_heading = True
        elif tag == "a":
            href = attributes.get("href")
            if self._in_heading and self._current["repo"] is None:
                self._current["repo"] = _slug_from_heading_href(href)
            elif self._current["repo"] is not None:
                expected = f"/{self._current['repo']}/stargazers"
                if href and href.casefold() == expected.casefold():
                    self._in_total = True
        elif tag == "p" and "col-9" in classes:
            self._in_description = True
        elif tag == "span":
            if self._language_depth:
                self._language_depth += 1
            elif attributes.get("itemprop") == "programmingLanguage":
                self._language_depth = 1
            if self._monthly_depth:
                self._monthly_depth += 1
            elif "float-sm-right" in classes:
                self._monthly_depth = 1

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._in_description:
            self._current["description_text"].append(data)
        if self._language_depth:
            self._current["language_text"].append(data)
        if self._in_total:
            self._current["total_text"].append(data)
        if self._monthly_depth:
            self._current["monthly_text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        if tag == "h2":
            self._in_heading = False
        elif tag == "p":
            self._in_description = False
        elif tag == "a":
            self._in_total = False
        elif tag == "span":
            if self._language_depth:
                self._language_depth -= 1
            if self._monthly_depth:
                self._monthly_depth -= 1
        elif tag == "article":
            self._article_depth -= 1
            if self._article_depth == 0:
                self._finish_row()

    def _finish_row(self) -> None:
        assert self._current is not None
        rank = len(self.rows) + 1
        slug = self._current["repo"]
        if slug is None:
            raise TrendingError(f"Trending row #{rank} has no valid repository link")
        if slug.casefold() in self._seen:
            raise TrendingError(f"Trending page has duplicate repository {slug}")
        total_match = TOTAL_STARS_RE.fullmatch("".join(self._current["total_text"]))
        if total_match is None:
            raise TrendingError(f"{slug}: missing or malformed total star count")
        monthly_match = MONTHLY_STARS_RE.fullmatch("".join(self._current["monthly_text"]))
        if monthly_match is None:
            raise TrendingError(f"{slug}: missing or malformed monthly star count")
        description = _clean_text(self._current["description_text"])
        language = _clean_text(self._current["language_text"])
        self.rows.append(
            {
                "repo": slug,
                "rank": rank,
                "stars_this_month": int(monthly_match[1].replace(",", "")),
                "stars_total": int(total_match[1].replace(",", "")),
                "description": description or None,
                "language": language or None,
            }
        )
        self._seen.add(slug.casefold())
        self._current = None
        self._in_heading = self._in_description = self._in_total = False
        self._language_depth = self._monthly_depth = 0


def parse_trending_html(page: str) -> list[dict]:
    """Read the page's complete ordered rows; reject broken page layouts."""
    if re.search(r"</html\s*>\s*\Z", page, re.I) is None:
        raise TrendingError("GitHub Trending page is incomplete")
    parser = _TrendingPageParser()
    parser.feed(page)
    parser.close()
    if parser._current is not None:
        raise TrendingError("GitHub Trending HTML ended mid-row")
    if not parser.rows:
        raise TrendingError("GitHub Trending page has no repository rows")
    return parser.rows


def build_snapshot(page: str, fetched_at: datetime) -> dict:
    """Record page data with the exact source and observation time."""
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise TrendingError("fetched_at must include a timezone")
    return {
        "source_url": SOURCE_URL,
        "period": "monthly",
        "fetched_at": fetched_at.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
        "repositories": parse_trending_html(page),
    }


def fetch_trending_html() -> str:
    """Fetch the public page over verified HTTPS, without a browser dependency."""
    request = Request(
        SOURCE_URL,
        headers={"Accept": "text/html", "User-Agent": "awesome-ai-projects-trending"},
    )
    options = {"timeout": 20}
    context = _fallback_ssl_context()
    if context is not None:
        options["context"] = context
    try:
        with urlopen(request, **options) as response:
            content = response.read()
    except HTTPError as error:
        raise TrendingError(f"GitHub Trending returned HTTP {error.code}") from error
    except URLError as error:
        raise TrendingError(f"Could not reach GitHub Trending: {error.reason}") from error
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TrendingError("GitHub Trending returned invalid UTF-8") from error


def _write_snapshot(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Atomically write JSON to this path")
    args = parser.parse_args(argv)
    try:
        page = fetch_trending_html()
        snapshot = build_snapshot(page, datetime.now(timezone.utc))
        rendered = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            _write_snapshot(args.output, rendered)
        else:
            print(rendered, end="")
    except (TrendingError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
