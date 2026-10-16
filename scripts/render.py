#!/usr/bin/env python3
"""Render the README from the curated catalog and dated discovery snapshots."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

if __package__:
    from .refresh import (
        ROOT,
        CatalogError,
        _parse_utc_timestamp,
        _read_json,
        _read_text,
        _validate_repo_slug,
        _write_if_changed,
        escape_table_text,
        is_open_source_license,
        render_catalog,
        replace_catalog,
        validate_stats,
    )
else:
    from refresh import (
        ROOT,
        CatalogError,
        _parse_utc_timestamp,
        _read_json,
        _read_text,
        _validate_repo_slug,
        _write_if_changed,
        escape_table_text,
        is_open_source_license,
        render_catalog,
        replace_catalog,
        validate_stats,
    )


MONTHLY_SOURCE = "https://github.com/trending?since=monthly"
PH_FEEDS = (
    "https://www.producthunt.com/feed?category=artificial-intelligence",
    "https://www.producthunt.com/feed?category=open-source",
)
STATUS_START = "<!-- STATUS:START -->"
STATUS_END = "<!-- STATUS:END -->"
RADAR_START = "<!-- RADAR:START -->"
RADAR_END = "<!-- RADAR:END -->"
LAUNCHES_START = "<!-- LAUNCHES:START -->"
LAUNCHES_END = "<!-- LAUNCHES:END -->"


def _date_label(value: str) -> str:
    date = _parse_utc_timestamp(value).astimezone(ZoneInfo("America/Los_Angeles"))
    return f"{date.strftime('%b')} {date.day}, {date.year} PT"


def _launch_label(value: str) -> str:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise CatalogError("Product Hunt launch_at must be an ISO 8601 timestamp") from error
    if date.tzinfo is None:
        raise CatalogError("Product Hunt launch_at must include a timezone")
    return f"{date.strftime('%b')} {date.day}, {date.year}"


def _validate_radar(snapshot: object, catalog: dict) -> list[dict]:
    if not isinstance(snapshot, dict):
        raise CatalogError("radar.json must be an object")
    _parse_utc_timestamp(snapshot.get("fetched_at"))
    _parse_utc_timestamp(snapshot.get("source_updated_at"))
    if snapshot.get("source_url") != MONTHLY_SOURCE or snapshot.get("period") != "monthly":
        raise CatalogError("radar.json must identify GitHub Trending's monthly page")
    rows = snapshot.get("repositories")
    if not isinstance(rows, list) or not rows or len(rows) > 8:
        raise CatalogError("radar.json needs 1 to 8 validated repositories")
    curated = {project["repo"].casefold() for project in catalog["projects"]}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CatalogError("radar.json repository must be an object")
        slug = _validate_repo_slug(row.get("repo"))
        if slug.casefold() in seen:
            raise CatalogError(f"duplicate radar repository {slug}")
        seen.add(slug.casefold())
        if row.get("github_url") != f"https://github.com/{slug}":
            raise CatalogError(f"{slug}: radar GitHub link does not match repository")
        if row.get("source_url") != MONTHLY_SOURCE:
            raise CatalogError(f"{slug}: radar source is not monthly Trending")
        for field in ("rank", "stars_this_month", "stars_total"):
            value = row.get(field)
            if type(value) is not int or value < (1 if field != "stars_total" else 0):
                raise CatalogError(f"{slug}: invalid {field}")
        if not is_open_source_license(row.get("license")):
            raise CatalogError(f"{slug}: radar license is not recognized as open source")
        if not isinstance(row.get("description"), str) or not row["description"].strip():
            raise CatalogError(f"{slug}: radar description is empty")
        if type(row.get("curated")) is not bool or row["curated"] != (slug.casefold() in curated):
            raise CatalogError(f"{slug}: radar curation label does not match the catalog")
    return rows


def _validate_launches(snapshot: object, catalog: dict) -> list[dict]:
    if not isinstance(snapshot, dict):
        raise CatalogError("producthunt-recent.json must be an object")
    _parse_utc_timestamp(snapshot.get("fetched_at"))
    if snapshot.get("complete") is not True or snapshot.get("warnings") != []:
        raise CatalogError("producthunt-recent.json must be a complete warning-free snapshot")
    sources = snapshot.get("sources")
    if not isinstance(sources, list) or len(sources) != len(PH_FEEDS):
        raise CatalogError("producthunt-recent.json must identify both Product Hunt feeds")
    source_urls: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("url"), str):
            raise CatalogError("Product Hunt source must include a feed URL")
        source_urls.add(source["url"])
        _parse_utc_timestamp(source.get("feed_updated_at"))
        if type(source.get("entry_count")) is not int or source["entry_count"] < 1:
            raise CatalogError("Product Hunt source must include its entry count")
    if source_urls != set(PH_FEEDS):
        raise CatalogError("producthunt-recent.json must identify both Product Hunt feeds")
    window_days = snapshot.get("window_days")
    if type(window_days) is not int or window_days < 1:
        raise CatalogError("producthunt-recent.json needs a positive window_days")
    rows = snapshot.get("candidates")
    if not isinstance(rows, list):
        raise CatalogError("producthunt-recent.json needs a candidates list")
    curated = {project["repo"].casefold() for project in catalog["projects"]}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CatalogError("Product Hunt candidate must be an object")
        slug = _validate_repo_slug(row.get("repo"))
        if slug.casefold() in curated or slug.casefold() in seen:
            raise CatalogError(f"{slug}: launch candidate is duplicate or already curated")
        seen.add(slug.casefold())
        if row.get("github_url") != f"https://github.com/{slug}":
            raise CatalogError(f"{slug}: launch GitHub link does not match repository")
        ph_url = row.get("product_hunt_url")
        if not isinstance(ph_url, str):
            raise CatalogError(f"{slug}: launch needs a Product Hunt product URL")
        try:
            ph_parts = urlsplit(ph_url)
        except ValueError as error:
            raise CatalogError(f"{slug}: launch needs a Product Hunt product URL") from error
        if (
            ph_parts.scheme != "https"
            or ph_parts.netloc != "www.producthunt.com"
            or not re.fullmatch(r"/products/[a-z0-9-]+", ph_parts.path)
            or ph_parts.query
            or ph_parts.fragment
        ):
            raise CatalogError(f"{slug}: launch needs a Product Hunt product URL")
        _launch_label(row.get("launch_at"))
        for field in ("name", "description"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise CatalogError(f"{slug}: launch {field} is empty")
        if type(row.get("stars")) is not int or row["stars"] < 0:
            raise CatalogError(f"{slug}: launch stars must be a nonnegative integer")
        if not is_open_source_license(row.get("license")):
            raise CatalogError(f"{slug}: launch license is not recognized as open source")
        if row.get("source_feeds") != list(PH_FEEDS):
            raise CatalogError(f"{slug}: launch needs both source feeds")
        evidence = row.get("evidence")
        if (
            not isinstance(evidence, dict)
            or evidence.get("product_page_url") != ph_url
            or evidence.get("github_api_url") != f"https://api.github.com/repos/{slug}"
            or not isinstance(evidence.get("product_hunt_post_id"), str)
            or not evidence["product_hunt_post_id"].isdigit()
        ):
            raise CatalogError(f"{slug}: launch evidence is incomplete")
    return rows


def render_status(catalog: dict, stats: dict, radar: dict, launches: dict) -> str:
    count = len(catalog["projects"])
    radar_count = len(radar["repositories"])
    launch_count = len(launches["candidates"])
    project_word = "project" if count == 1 else "projects"
    signal_word = "signal" if radar_count == 1 else "signals"
    lead_word = "lead" if launch_count == 1 else "leads"
    return (
        f"**{count} selected {project_word}** · "
        f"**{radar_count} monthly {signal_word}** · "
        f"**{launch_count} new launch {lead_word}**\n\n"
        f"_Stars checked {_date_label(stats['fetched_at'])} · "
        f"Monthly Trending checked {_date_label(radar['source_updated_at'])} · "
        f"Product Hunt checked {_date_label(launches['fetched_at'])}_\n"
    )


def render_radar(radar: dict) -> str:
    lines = [
        f"_Source: [GitHub Trending · this month]({MONTHLY_SOURCE}), captured "
        f"**{_date_label(radar['source_updated_at'])}**. GitHub reports the star gains. "
        "Descriptions come from repository metadata and may include unverified claims. "
        "New leads are screened automatically and have not been selected for the collection._",
        "",
        "| Trending project | ⭐ This month | GitHub description / license |",
        "| --- | ---: | --- |",
    ]
    for row in radar["repositories"]:
        slug = row["repo"]
        label = "✓ Curated" if row["curated"] else "🔎 New lead"
        lines.append(
            f"| #{row['rank']} [{escape_table_text(slug)}]({row['github_url']}) "
            f"· {label} | "
            f"**+{row['stars_this_month']:,}** "
            f"({row['stars_total']:,} total) | "
            f"{escape_table_text(row['description'])} · {escape_table_text(row['license'])} |"
        )
    return "\n".join(lines) + "\n"


def render_launches(launches: dict) -> str:
    feed_links = (
        f"[AI feed]({PH_FEEDS[0]}) and [open-source feed]({PH_FEEDS[1]})"
    )
    lines = [
        f"_Source: Product Hunt's {feed_links}, checked "
        f"**{_date_label(launches['fetched_at'])}**. Launch dates come from each "
        "product page; GitHub links and open-source licenses are checked. "
        "Descriptions come from repository metadata. "
        "These are automated leads awaiting collection review._",
        "",
    ]
    rows = sorted(
        launches["candidates"],
        key=lambda row: (row["launch_at"], row["stars"], row["repo"].casefold()),
        reverse=True,
    )
    shown = rows[:8]
    if len(rows) > len(shown):
        lines.extend(
            [
                f"Showing the {len(shown)} newest of {len(rows)} verified leads. "
                "The full snapshot is linked below.",
                "",
            ]
        )
    if not rows:
        lines.append(
            f"No verified new open-source AI launches appeared in the last "
            f"{launches['window_days']} days of these feeds."
        )
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            "| GitHub project | Product Hunt launch | Snapshot |",
            "| --- | --- | --- |",
        ]
    )
    for row in shown:
        lines.append(
            f"| [{escape_table_text(row['repo'])}]({row['github_url']}) | "
            f"[{escape_table_text(row['name'])} · {_launch_label(row['launch_at'])}]"
            f"({row['product_hunt_url']}) | "
            f"{escape_table_text(row['description'])} · "
            f"⭐ {row['stars']:,} · {escape_table_text(row['license'])} |"
        )
    return "\n".join(lines) + "\n"


def replace_block(readme: str, start_marker: str, end_marker: str, rendered: str) -> str:
    if readme.count(start_marker) != 1 or readme.count(end_marker) != 1:
        raise CatalogError(f"README.md needs exactly one {start_marker} and {end_marker}")
    start = readme.index(start_marker) + len(start_marker)
    end = readme.index(end_marker)
    if start > end:
        raise CatalogError(f"README.md markers are reversed: {start_marker}")
    return readme[:start] + "\n" + rendered.rstrip("\n") + "\n" + readme[end:]


def render_all(root: Path, check: bool = False) -> bool:
    root = Path(root)
    catalog = _read_json(root / "data" / "projects.json")
    stats = _read_json(root / "data" / "stats.json")
    radar = _read_json(root / "data" / "radar.json")
    launches = _read_json(root / "data" / "producthunt-recent.json")
    validate_stats(stats, catalog)
    _validate_radar(radar, catalog)
    _validate_launches(launches, catalog)
    readme_path = root / "README.md"
    current = _read_text(readme_path)
    updated = replace_catalog(current, render_catalog(catalog, stats))
    updated = replace_block(updated, STATUS_START, STATUS_END, render_status(catalog, stats, radar, launches))
    updated = replace_block(updated, RADAR_START, RADAR_END, render_radar(radar))
    updated = replace_block(updated, LAUNCHES_START, LAUNCHES_END, render_launches(launches))
    if updated == current:
        return False
    if check:
        raise CatalogError("README.md is out of date; run python3 scripts/render.py --write")
    return _write_if_changed(readme_path, updated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Render README from local JSON")
    mode.add_argument("--check", action="store_true", help="Check README without writing")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        changed = render_all(args.root, check=args.check)
    except CatalogError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print("README is current." if args.check or not changed else "README updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
