#!/usr/bin/env python3
"""Suggest recently created AI repositories for human review.

This command does not edit data/projects.json. Topic matches and star counts
help surface candidates; curators still check product quality and license scope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

if __package__:
    from .refresh import (
        API_ROOT,
        ROOT,
        CatalogError,
        _validate_repo_slug,
        escape_table_text,
        fetch_json,
        is_open_source_license,
        validate_catalog,
    )
else:
    from refresh import (
        API_ROOT,
        ROOT,
        CatalogError,
        _validate_repo_slug,
        escape_table_text,
        fetch_json,
        is_open_source_license,
        validate_catalog,
    )


DEFAULT_TOPICS = ("artificial-intelligence", "generative-ai", "ai-agent", "llm")
TOPIC_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def find_candidates(
    catalog: dict,
    since: date,
    topics: tuple[str, ...] | list[str],
    min_stars: int,
    limit: int,
    fetch_json_fn,
) -> list[dict]:
    """Search each topic and return deduplicated, licensed public projects."""
    validate_catalog(catalog)
    if not isinstance(since, date):
        raise CatalogError("since must be a date")
    if type(min_stars) is not int or min_stars < 0:
        raise CatalogError("min_stars must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CatalogError("limit must be between 1 and 100")
    if not topics or any(not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic) for topic in topics):
        raise CatalogError("topics must be GitHub topic names")

    known = {project["repo"].casefold() for project in catalog["projects"]}
    seen: set[str] = set()
    candidates: list[dict] = []
    for topic in topics:
        query = f"topic:{topic} created:>={since.isoformat()} stars:>={min_stars} is:public"
        url = f"{API_ROOT}/search/repositories?" + urlencode(
            {"q": query, "sort": "stars", "order": "desc", "per_page": 100}
        )
        response = fetch_json_fn(url)
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise CatalogError(f"GitHub search returned invalid results for topic:{topic}")
        if response.get("incomplete_results") is True:
            print(f"warning: GitHub search results for topic:{topic} may be incomplete", file=sys.stderr)
        for item in response["items"]:
            if not isinstance(item, dict):
                continue
            slug = item.get("full_name")
            try:
                _validate_repo_slug(slug)
            except CatalogError:
                continue
            key = slug.casefold()
            if key in known or key in seen:
                continue
            if item.get("fork") is not False or item.get("archived") is not False:
                continue
            if item.get("private") is True:
                continue
            stars = item.get("stargazers_count")
            if type(stars) is not int or stars < min_stars:
                continue
            license_info = item.get("license")
            license_id = license_info.get("spdx_id") if isinstance(license_info, dict) else None
            if not is_open_source_license(license_id):
                continue
            description = item.get("description")
            if not isinstance(description, str) or not description.strip():
                description = "No description provided"
            item_topics = item.get("topics")
            if not isinstance(item_topics, list):
                item_topics = []
            item_topics = [value for value in item_topics if isinstance(value, str)]
            candidates.append(
                {
                    "full_name": slug,
                    "description": description,
                    "stargazers_count": stars,
                    "license": license_id,
                    "topics": item_topics,
                }
            )
            seen.add(key)
    candidates.sort(key=lambda item: (-item["stargazers_count"], item["full_name"].casefold()))
    return candidates[:limit]


def render_candidates(candidates: list[dict], since: date) -> str:
    """Print a compact review table; none of these rows are auto-curated."""
    lines = [
        "## Fresh AI repository candidates",
        "",
        f"_Created since {since.isoformat()} UTC. Review fit and license before adding one to the catalog._",
        "",
        "| Project | Stars | License | Description | Topics |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for candidate in candidates:
        slug = candidate["full_name"]
        topics = ", ".join(candidate["topics"]) if candidate["topics"] else "—"
        lines.append(
            f"| [{escape_table_text(slug)}](https://github.com/{slug}) | "
            f"{candidate['stargazers_count']:,} | "
            f"{escape_table_text(candidate['license'])} | "
            f"{escape_table_text(candidate['description'])} | "
            f"{escape_table_text(topics)} |"
        )
    if not candidates:
        lines.append("| No candidates matched | — | — | — | — |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="Look back this many days (default: 30)")
    parser.add_argument("--topic", action="append", help="GitHub topic to search; repeat for more")
    parser.add_argument("--min-stars", type=int, default=25, help="Minimum stars (default: 25)")
    parser.add_argument("--limit", type=int, default=30, help="Maximum candidates (1–100)")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    since = datetime.now(timezone.utc).date() - timedelta(days=args.days)
    try:
        with (args.root / "data" / "projects.json").open(encoding="utf-8") as file:
            catalog = json.load(file)
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        candidates = find_candidates(
            catalog,
            since=since,
            topics=args.topic or DEFAULT_TOPICS,
            min_stars=args.min_stars,
            limit=args.limit,
            fetch_json_fn=lambda url: fetch_json(url, token=token),
        )
    except (CatalogError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(render_candidates(candidates, since), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
