#!/usr/bin/env python3
"""Validate the curated catalog and refresh its GitHub metadata snapshot.

The editable list lives in data/projects.json. GitHub statistics live in
data/stats.json so the README can render without making requests on page load.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SYSTEM_CA_BUNDLE = Path("/etc/ssl/cert.pem")
API_ROOT = "https://api.github.com"
START_MARKER = "<!-- CATALOG:START -->"
END_MARKER = "<!-- CATALOG:END -->"
REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9_.-]+\Z")
CATEGORY_RE = re.compile(r"[a-z][a-z0-9-]*\Z")

# GitHub's license.spdx_id can also identify noncommercial and source-available
# terms. Keep a conservative list of software/open-source SPDX identifiers.
# A simple SPDX OR expression is accepted when every choice is on this list.
OPEN_SOURCE_LICENSES = frozenset(
    {
        "0BSD",
        "AFL-3.0",
        "AGPL-3.0",
        "AGPL-3.0-only",
        "AGPL-3.0-or-later",
        "Apache-2.0",
        "Artistic-2.0",
        "BSL-1.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BSD-3-Clause-Clear",
        "BSD-4-Clause",
        "CC0-1.0",
        "CDDL-1.0",
        "ECL-2.0",
        "EPL-1.0",
        "EPL-2.0",
        "EUPL-1.1",
        "EUPL-1.2",
        "GPL-2.0",
        "GPL-2.0-only",
        "GPL-2.0-or-later",
        "GPL-3.0",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "ISC",
        "LGPL-2.1",
        "LGPL-2.1-only",
        "LGPL-2.1-or-later",
        "LGPL-3.0",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MIT",
        "MPL-2.0",
        "MS-PL",
        "NCSA",
        "OFL-1.1",
        "OSL-3.0",
        "PostgreSQL",
        "Python-2.0",
        "Unlicense",
        "Zlib",
        "ZPL-2.1",
    }
)


class CatalogError(ValueError):
    """Invalid catalog input or a GitHub API failure."""


def _fallback_ssl_context() -> ssl.SSLContext | None:
    """Use the macOS trust bundle if this Python has no default CA file."""
    if sys.platform != "darwin" or "SSL_CERT_FILE" in os.environ:
        return None
    if ssl.get_default_verify_paths().cafile is not None:
        return None
    if not SYSTEM_CA_BUNDLE.is_file():
        return None
    return ssl.create_default_context(cafile=str(SYSTEM_CA_BUNDLE))


def is_open_source_license(value: object) -> bool:
    """Accept recognized SPDX IDs or a simple OR of recognized IDs."""
    return (
        isinstance(value, str)
        and bool(value)
        and all(part in OPEN_SOURCE_LICENSES for part in value.split(" OR "))
    )


def _required_text(item: dict, field: str, label: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{label} needs a nonempty {field}")
    return value


def _validate_repo_slug(slug: object) -> str:
    if not isinstance(slug, str) or not REPO_RE.fullmatch(slug):
        raise CatalogError(f"invalid repository slug {slug!r}; use owner/name")
    owner, name = slug.split("/", 1)
    if name in {".", ".."} or len(owner) > 39 or len(name) > 100:
        raise CatalogError(f"invalid repository slug {slug!r}; use owner/name")
    return slug


def _validate_product_hunt_url(url: object, slug: str) -> None:
    if not isinstance(url, str) or any(ch.isspace() for ch in url):
        raise CatalogError(f"{slug}: product_hunt must be an HTTPS Product Hunt URL")
    try:
        parts = urlsplit(url)
    except ValueError as error:
        raise CatalogError(f"{slug}: product_hunt must be an HTTPS Product Hunt URL") from error
    if (
        parts.scheme != "https"
        or parts.netloc not in {"producthunt.com", "www.producthunt.com"}
        or any(char in url for char in '<>"')
    ):
        raise CatalogError(f"{slug}: product_hunt must be an HTTPS Product Hunt URL")


def validate_catalog(catalog: object) -> None:
    """Check that editorial data is complete and unambiguous."""
    if not isinstance(catalog, dict):
        raise CatalogError("projects.json must be a JSON object")
    categories = catalog.get("categories")
    projects = catalog.get("projects")
    if not isinstance(categories, list) or not categories:
        raise CatalogError("projects.json needs a nonempty categories list")
    if not isinstance(projects, list) or not projects:
        raise CatalogError("projects.json needs a nonempty projects list")

    category_ids: set[str] = set()
    for index, category in enumerate(categories, 1):
        if not isinstance(category, dict):
            raise CatalogError(f"category #{index} must be an object")
        category_id = _required_text(category, "id", f"category #{index}")
        if not CATEGORY_RE.fullmatch(category_id):
            raise CatalogError(f"invalid category id {category_id!r}")
        if category_id in category_ids:
            raise CatalogError(f"duplicate category {category_id!r}")
        category_ids.add(category_id)
        _required_text(category, "title", f"category {category_id}")
        _required_text(category, "description", f"category {category_id}")

    seen_repositories: set[str] = set()
    for index, project in enumerate(projects, 1):
        if not isinstance(project, dict):
            raise CatalogError(f"project #{index} must be an object")
        slug = _validate_repo_slug(project.get("repo"))
        if slug.casefold() in seen_repositories:
            raise CatalogError(f"duplicate repository {slug!r}")
        seen_repositories.add(slug.casefold())
        _required_text(project, "name", slug)
        _required_text(project, "description", slug)
        category = _required_text(project, "category", slug)
        if category not in category_ids:
            raise CatalogError(f"{slug}: unknown category {category!r}")
        if "product_hunt" in project:
            _validate_product_hunt_url(project["product_hunt"], slug)
        if "license_override" in project:
            override = project["license_override"]
            if (
                not isinstance(override, dict)
                or set(override) != {"spdx_id", "evidence_url"}
                or not is_open_source_license(override.get("spdx_id"))
            ):
                raise CatalogError(f"{slug}: license_override needs an open-source SPDX ID")
            evidence = override.get("evidence_url")
            if not isinstance(evidence, str) or any(char.isspace() for char in evidence):
                raise CatalogError(f"{slug}: license_override needs a repository license link")
            parts = urlsplit(evidence)
            if (
                parts.scheme != "https"
                or parts.netloc != "github.com"
                or not parts.path.startswith(f"/{slug}/blob/")
                or parts.query
                or parts.fragment
                or any(char in evidence for char in '<>"')
            ):
                raise CatalogError(f"{slug}: license_override needs a repository license link")


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise CatalogError("stats.json fetched_at must be an ISO 8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CatalogError("stats.json fetched_at must be an ISO 8601 UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CatalogError("stats.json fetched_at must be an ISO 8601 UTC timestamp")
    return parsed


def validate_stats(stats: object, catalog: dict) -> None:
    """Check that the snapshot covers exactly the curated repositories."""
    validate_catalog(catalog)
    if not isinstance(stats, dict):
        raise CatalogError("stats.json must be a JSON object")
    _parse_utc_timestamp(stats.get("fetched_at"))
    repositories = stats.get("repositories")
    if not isinstance(repositories, dict):
        raise CatalogError("stats.json needs a repositories object")
    expected = {project["repo"] for project in catalog["projects"]}
    missing = expected - repositories.keys()
    extra = repositories.keys() - expected
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unexpected " + ", ".join(sorted(extra)))
        raise CatalogError("stats.json repositories are out of sync: " + "; ".join(details))
    for slug, snapshot in repositories.items():
        if not isinstance(snapshot, dict):
            raise CatalogError(f"{slug}: stats entry must be an object")
        stars = snapshot.get("stars")
        if type(stars) is not int or stars < 0:
            raise CatalogError(f"{slug}: stars must be a nonnegative integer")
        if not is_open_source_license(snapshot.get("license")):
            raise CatalogError(f"{slug}: license must be a recognized open-source SPDX ID")
        if snapshot.get("archived") is not False:
            raise CatalogError(f"{slug}: archived must be false")


def escape_table_text(value: str) -> str:
    """Keep editorial text from changing Markdown table cells or injecting HTML."""
    collapsed = " ".join(value.split())
    escaped = html.escape(collapsed, quote=False)
    for character in ("\\", "|", "[", "]", "*", "_", "`"):
        escaped = escaped.replace(character, "\\" + character)
    return escaped


def render_catalog(catalog: dict, stats: dict) -> str:
    """Render the generated README block from checked-in JSON."""
    validate_stats(stats, catalog)
    fetched_date = (
        _parse_utc_timestamp(stats["fetched_at"])
        .astimezone(ZoneInfo("America/Los_Angeles"))
        .date()
        .isoformat()
    )
    lines = [
        f"_GitHub metadata refreshed on **{fetched_date} PT**. "
        "Documented license exceptions are not automatically rechecked._",
        "",
    ]
    for category in catalog["categories"]:
        category_projects = [
            project for project in catalog["projects"] if project["category"] == category["id"]
        ]
        pick_label = "pick" if len(category_projects) == 1 else "picks"
        lines.extend(
            [
                f'<a id="{category["id"]}"></a>',
                "",
                f"### {escape_table_text(category['title'])}",
                "",
                f"**{len(category_projects)} {pick_label}.** {escape_table_text(category['description'])}",
                "",
                "| Project | Why try it | ⭐ Stars |",
                "| --- | --- | ---: |",
            ]
        )
        for project in category_projects:
            slug = project["repo"]
            snapshot = stats["repositories"][slug]
            repository_url = f"https://github.com/{slug}"
            launch = ""
            if "product_hunt" in project:
                product_hunt_url = project["product_hunt"].replace("(", "%28").replace(")", "%29")
                launch = f" · [🏹 Product Hunt]({product_hunt_url})"
            lines.append(
                "| "
                + f"[{escape_table_text(project['name'])}]({repository_url})"
                + f" · `{snapshot['license']}`{launch} | "
                + f"{escape_table_text(project['description'])} | "
                + f"**{snapshot['stars']:,}** |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def replace_catalog(readme: str, rendered: str) -> str:
    """Replace only the content between the two README markers."""
    if readme.count(START_MARKER) != 1 or readme.count(END_MARKER) != 1:
        raise CatalogError("README.md must contain exactly one of each catalog marker")
    start = readme.index(START_MARKER) + len(START_MARKER)
    end = readme.index(END_MARKER)
    if start > end:
        raise CatalogError("README.md catalog markers are in the wrong order")
    return readme[:start] + "\n" + rendered.rstrip("\n") + "\n" + readme[end:]


def fetch_json(url: str, token: str | None = None, opener=None) -> dict:
    """Fetch one GitHub REST response with actionable API errors."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "awesome-ai-projects-catalog",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    open_kwargs = {"timeout": 20}
    if opener is None:
        opener = urlopen
        context = _fallback_ssl_context()
        if context is not None:
            open_kwargs["context"] = context
    try:
        for attempt in range(2):
            try:
                with opener(request, **open_kwargs) as response:
                    payload = json.load(response)
                break
            except HTTPError:
                raise
            except URLError:
                if attempt == 1:
                    raise
                time.sleep(0.5)
    except HTTPError as error:
        try:
            raw_error = error.read()
        finally:
            error.close()
        try:
            message = json.loads(raw_error.decode("utf-8")).get("message", "")
        except (ValueError, UnicodeDecodeError, AttributeError):
            message = ""
        remaining = error.headers.get("X-RateLimit-Remaining", "") if error.headers else ""
        if error.code == 429 or remaining == "0" or "rate limit" in message.lower():
            raise CatalogError(
                f"GitHub API rate limit reached (HTTP {error.code}) for {url}; "
                "set GITHUB_TOKEN or try again later"
            ) from error
        detail = f": {message}" if message else ""
        raise CatalogError(f"GitHub API HTTP {error.code} for {url}{detail}") from error
    except URLError as error:
        raise CatalogError(f"Could not reach GitHub API for {url}: {error.reason}") from error
    except (ValueError, UnicodeDecodeError) as error:
        raise CatalogError(f"GitHub API returned invalid JSON for {url}") from error
    if not isinstance(payload, dict):
        raise CatalogError(f"GitHub API returned a non-object response for {url}")
    return payload


def fetch_repository_stats(
    slug: str, fetch_json_fn, existing_license: str | None = None
) -> dict:
    """Read one live repository, preserving a verified dual license if needed."""
    _validate_repo_slug(slug)
    response = fetch_json_fn(f"{API_ROOT}/repos/{slug}")
    if not isinstance(response, dict):
        raise CatalogError(f"{slug}: GitHub API response must be an object")
    full_name = response.get("full_name")
    if not isinstance(full_name, str) or full_name.casefold() != slug.casefold():
        raise CatalogError(f"{slug}: GitHub API points to {full_name!r}; update the slug")
    if response.get("private") is not False:
        raise CatalogError(f"{slug}: repository is private or GitHub API omitted public status")
    if response.get("archived") is True:
        raise CatalogError(f"{slug}: repository is archived; remove it from the catalog")
    if response.get("archived") is not False:
        raise CatalogError(f"{slug}: GitHub API omitted archived status")
    if response.get("fork") is True:
        raise CatalogError(f"{slug}: repository is a fork; remove it from the catalog")
    if response.get("fork") is not False:
        raise CatalogError(f"{slug}: GitHub API omitted fork status")
    stars = response.get("stargazers_count")
    if type(stars) is not int or stars < 0:
        raise CatalogError(f"{slug}: GitHub API returned invalid star count")
    license_info = response.get("license")
    license_id = license_info.get("spdx_id") if isinstance(license_info, dict) else None
    if license_id == "NOASSERTION" and is_open_source_license(existing_license):
        license_id = existing_license
    elif (
        is_open_source_license(existing_license)
        and " OR " in existing_license
        and license_id in existing_license.split(" OR ")
    ):
        license_id = existing_license
    if not is_open_source_license(license_id):
        raise CatalogError(
            f"{slug}: GitHub API did not identify a recognized open-source license"
        )
    return {"stars": stars, "license": license_id, "archived": False}


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CatalogError(f"Could not read {path}: {error.strerror}") from error
    except json.JSONDecodeError as error:
        raise CatalogError(f"Invalid JSON in {path}: {error.msg} at line {error.lineno}") from error


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CatalogError(f"Could not read {path}: {error.strerror}") from error


def _write_if_changed(path: Path, content: str) -> bool:
    current = _read_text(path) if path.exists() else None
    if current == content:
        return False
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as error:
        raise CatalogError(f"Could not write {path}: {error.strerror}") from error
    return True


def offline_sync(root: Path, check: bool = False) -> bool:
    """Render from local data or fail if the checked-in README has drifted."""
    root = Path(root)
    catalog = _read_json(root / "data" / "projects.json")
    stats = _read_json(root / "data" / "stats.json")
    readme_path = root / "README.md"
    readme = _read_text(readme_path)
    updated = replace_catalog(readme, render_catalog(catalog, stats))
    if updated == readme:
        return False
    if check:
        raise CatalogError("README.md catalog is out of date; run python scripts/refresh.py --offline")
    return _write_if_changed(readme_path, updated)


def _existing_licenses(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    old_stats = _read_json(path)
    if not isinstance(old_stats, dict) or not isinstance(old_stats.get("repositories"), dict):
        raise CatalogError(f"{path} needs a repositories object")
    return {
        slug: snapshot["license"]
        for slug, snapshot in old_stats["repositories"].items()
        if isinstance(snapshot, dict)
        and "license" in snapshot
        and is_open_source_license(snapshot["license"])
    }


def refresh_sync(root: Path, fetch_json_fn=None, now: datetime | None = None) -> bool:
    """Fetch every project first, then replace the snapshot and generated block."""
    root = Path(root)
    catalog = _read_json(root / "data" / "projects.json")
    validate_catalog(catalog)
    readme_path = root / "README.md"
    readme = _read_text(readme_path)
    replace_catalog(readme, "")  # Fail before network calls if markers are broken.
    stats_path = root / "data" / "stats.json"
    existing_licenses = _existing_licenses(stats_path)
    if fetch_json_fn is None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        fetch_json_fn = lambda url: fetch_json(url, token=token)

    repositories = {}
    for project in catalog["projects"]:
        slug = project["repo"]
        override = project.get("license_override")
        verified_license = (
            override["spdx_id"] if override is not None else existing_licenses.get(slug)
        )
        repositories[slug] = fetch_repository_stats(
            slug, fetch_json_fn, existing_license=verified_license
        )

    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise CatalogError("refresh timestamp must have a timezone")
    fetched_at = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    stats = {"fetched_at": fetched_at, "repositories": repositories}
    rendered = render_catalog(catalog, stats)
    updated_readme = replace_catalog(readme, rendered)
    updated_stats = json.dumps(stats, indent=2, ensure_ascii=False) + "\n"
    stats_changed = _write_if_changed(stats_path, updated_stats)
    readme_changed = _write_if_changed(readme_path, updated_readme)
    return stats_changed or readme_changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--refresh", action="store_true", help="Fetch current GitHub metadata")
    mode.add_argument("--offline", action="store_true", help="Regenerate README from local JSON")
    mode.add_argument("--check", action="store_true", help="Check local README without writing")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.refresh:
            changed = refresh_sync(args.root)
        else:
            changed = offline_sync(args.root, check=args.check)
    except CatalogError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.check:
        print("Catalog is current.")
    else:
        print("Catalog updated." if changed else "Catalog already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
