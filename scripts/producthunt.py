#!/usr/bin/env python3
"""Find Product Hunt AI/open-source leads with verified GitHub links.

Product Hunt's public Atom feeds are the discovery source. When product pages
are available, the matching post's scheduledAt and explicit GitHub link provide
the launch evidence. If product pages are blocked, the limited fallback checks
each feed-provided, post-specific outbound Link redirect and admits only direct
GitHub repository destinations. Its Atom published value is a feed publication
timestamp, not a verified launch date. GitHub confirms public, non-fork, unarchived repos
with a recognized open-source license. Results remain review candidates.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener, urlopen

if __package__:
    from .refresh import (
        API_ROOT,
        ROOT,
        CatalogError,
        _fallback_ssl_context,
        _validate_repo_slug,
        fetch_json,
        fetch_repository_stats,
        validate_catalog,
    )
else:
    from refresh import (
        API_ROOT,
        ROOT,
        CatalogError,
        _fallback_ssl_context,
        _validate_repo_slug,
        fetch_json,
        fetch_repository_stats,
        validate_catalog,
    )


AI_FEED = "https://www.producthunt.com/feed?category=artificial-intelligence"
OPEN_SOURCE_FEED = "https://www.producthunt.com/feed?category=open-source"
FEEDS = (AI_FEED, OPEN_SOURCE_FEED)
ATOM = "{http://www.w3.org/2005/Atom}"
POST_ID_RE = re.compile(r"tag:www\.producthunt\.com,2005:Post/([0-9]+)\Z")
MAX_RESPONSE_BYTES = 2_500_000


class ProductHuntError(CatalogError):
    """Product Hunt source is unavailable or has an unexpected format."""


@dataclass(frozen=True)
class FeedEntry:
    post_id: str
    name: str
    product_hunt_url: str
    tagline: str
    feed_published_at: datetime
    direct_link_url: str | None


@dataclass(frozen=True)
class ParsedFeed:
    entries: dict[str, FeedEntry]
    updated_at: datetime
    skipped_entries: int


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ProductHuntError("timestamp needs a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _timestamp(value: str, context: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ProductHuntError(f"{context} has an invalid timestamp") from error
    if result.tzinfo is None:
        raise ProductHuntError(f"{context} has a timestamp without a timezone")
    return result.astimezone(timezone.utc)


def _product_url(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or parts.hostname not in {"producthunt.com", "www.producthunt.com"}
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or not re.fullmatch(r"/products/[a-z0-9-]+/?", parts.path)
    ):
        return None
    return f"https://www.producthunt.com{parts.path.rstrip('/')}"


def _tagline(content: str) -> str:
    match = re.search(r"<p\b[^>]*>(.*?)</p>", content, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", match.group(1))).split())


class _FeedContentLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.urls.extend(value for name, value in attrs if name == "href" and value)


def _post_redirect_url(value: str, post_id: str | None = None) -> str | None:
    """Preserve a post-specific feed Link on either Product Hunt hostname."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    match = re.fullmatch(r"/r/p/([0-9]+)", parts.path)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"producthunt.com", "www.producthunt.com"}
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.fragment
        or not match
        or (post_id is not None and match.group(1) != post_id)
    ):
        return None
    return value


def _feed_direct_link(content: str, post_id: str) -> str | None:
    links = _FeedContentLinks()
    links.feed(content)
    matched = {_post_redirect_url(url, post_id) for url in links.urls}
    matched.discard(None)
    return next(iter(matched)) if len(matched) == 1 else None


def parse_atom_feed(payload: str, url: str) -> ParsedFeed:
    """Read the official Atom feed; reject an HTML block or empty feed."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise ProductHuntError(f"Product Hunt feed {url} is not valid Atom XML") from error
    if root.tag != ATOM + "feed":
        raise ProductHuntError(f"Product Hunt feed {url} did not return an Atom feed")
    updated_text = root.findtext(ATOM + "updated")
    if not updated_text:
        raise ProductHuntError(f"Product Hunt feed {url} has no updated timestamp")
    updated = _timestamp(updated_text, f"Product Hunt feed {url}")
    entries: dict[str, FeedEntry] = {}
    skipped = 0
    for element in root.findall(ATOM + "entry"):
        id_text = element.findtext(ATOM + "id") or ""
        match = POST_ID_RE.fullmatch(id_text)
        name = (element.findtext(ATOM + "title") or "").strip()
        product_links = [
            link.attrib.get("href", "")
            for link in element.findall(ATOM + "link")
            if link.attrib.get("rel") == "alternate"
        ]
        product_urls = {_product_url(link) for link in product_links}
        product_urls.discard(None)
        if not match or not name or len(product_urls) != 1:
            skipped += 1
            continue
        post_id = match.group(1)
        try:
            feed_published_at = _timestamp(
                element.findtext(ATOM + "published") or "", f"Product Hunt post {post_id}"
            )
        except ProductHuntError:
            skipped += 1
            continue
        content = element.findtext(ATOM + "content") or ""
        entries[post_id] = FeedEntry(
            post_id=post_id,
            name=name,
            product_hunt_url=next(iter(product_urls)),
            tagline=_tagline(content),
            feed_published_at=feed_published_at,
            direct_link_url=_feed_direct_link(content, post_id),
        )
    if not entries:
        raise ProductHuntError(f"Product Hunt feed {url} had no valid launch entries")
    return ParsedFeed(entries=entries, updated_at=updated, skipped_entries=skipped)


def fetch_text(url: str, opener=None) -> str:
    """Fetch one public Product Hunt page, with a size cap and verified TLS."""
    if url not in FEEDS and _product_url(url) != url:
        raise ProductHuntError(f"Unexpected Product Hunt URL: {url}")
    accept = "application/atom+xml" if url in FEEDS else "text/html"
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "awesome-ai-projects/1.0 (+https://github.com/anthonyplusAI/awesome-ai-projects)",
        },
    )
    kwargs = {"timeout": 20}
    if opener is None:
        opener = urlopen
        context = _fallback_ssl_context()
        if context is not None:
            kwargs["context"] = context
    try:
        with opener(request, **kwargs) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise ProductHuntError(f"Product Hunt returned HTTP {error.code} for {url}") from error
    except (URLError, TimeoutError) as error:
        raise ProductHuntError(f"Could not reach Product Hunt at {url}: {error}") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProductHuntError(f"Product Hunt response exceeded {MAX_RESPONSE_BYTES} bytes: {url}")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProductHuntError(f"Product Hunt returned invalid UTF-8 for {url}") from error


def fetch_direct_link_location(url: str, opener=None) -> str:
    """Read the feed Link's first HTTP redirect without visiting its target."""
    if _post_redirect_url(url) != url:
        raise ProductHuntError(f"Unexpected Product Hunt direct Link URL: {url}")
    request = Request(
        url,
        headers={
            "Accept": "*/*",
            "User-Agent": "awesome-ai-projects/1.0 (+https://github.com/anthonyplusAI/awesome-ai-projects)",
        },
    )
    if opener is None:
        class _NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, request, fp, code, msg, headers, newurl):
                return None

        context = _fallback_ssl_context()
        handler = HTTPSHandler(context=context) if context is not None else HTTPSHandler()
        opener = build_opener(_NoRedirect(), handler).open
    try:
        with opener(request, timeout=20) as response:
            raise ProductHuntError(
                f"Product Hunt direct Link did not redirect (HTTP {response.status}): {url}"
            )
    except HTTPError as error:
        if error.code not in {301, 302, 303, 307, 308}:
            error.close()
            raise ProductHuntError(
                f"Product Hunt direct Link returned HTTP {error.code} for {url}"
            ) from error
        location = error.headers.get("Location")
        error.close()
    except (URLError, TimeoutError) as error:
        raise ProductHuntError(f"Could not reach Product Hunt direct Link at {url}: {error}") from error
    if not isinstance(location, str) or not location:
        raise ProductHuntError(f"Product Hunt direct Link had no Location: {url}")
    try:
        parts = urlsplit(location)
        port = parts.port
    except ValueError as error:
        raise ProductHuntError(f"Product Hunt direct Link had invalid Location: {url}") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.hostname in {"producthunt.com", "www.producthunt.com"}
    ):
        raise ProductHuntError(f"Product Hunt direct Link had no external HTTP location: {url}")
    return location


def _github_slug(value: str) -> str | None:
    """Accept a GitHub repository root URL, including HTTP and .git variants."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or parts.hostname != "github.com"
        or parts.username is not None
        or parts.password is not None
        or port is not None
    ):
        return None
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    slug = path.removeprefix("/")
    try:
        return _validate_repo_slug(slug)
    except CatalogError:
        return None


def _name_key(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))


def _focused_product_github_slug(payload: str, product_id: str) -> str | None:
    """Read githubUrl only from a Product object with the focused product ID."""
    values: set[str | None] = set()
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"product":\s*(?=\{)', payload):
        try:
            product, _ = decoder.raw_decode(payload[match.end() :])
        except ValueError:
            continue
        if (
            not isinstance(product, dict)
            or product.get("__typename") != "Product"
            or str(product.get("id")) != product_id
            or "githubUrl" not in product
        ):
            continue
        url = product["githubUrl"]
        if url is None:
            values.add(None)
        elif isinstance(url, str):
            values.add(_github_slug(html.unescape(url)) or "<invalid>")
        else:
            values.add("<invalid>")
    if not values:
        raise ProductHuntError(
            f"Could not find the focused Product githubUrl field for product {product_id}"
        )
    if len(values) != 1 or None in values or "<invalid>" in values:
        return None
    return next(iter(values))


def product_page_evidence(
    payload: str, post_id: str, expected_launch_name: str
) -> tuple[str, datetime, str] | None:
    """Tie the GitHub link to the exact launch, never just its umbrella page."""
    # Product Hunt renders the focused launch as JSON in its public page. A
    # product can have many posts, so inspect only the Atom post ID we found.
    launches: set[tuple[str, str, str, str, datetime]] = set()
    marker = '"focusedPageVariant":'
    for match in re.finditer(re.escape(marker), payload):
        try:
            variant, _ = json.JSONDecoder().raw_decode(payload[match.end() :])
            launch = variant["launch"]
            if not isinstance(launch, dict) or str(launch.get("id")) != post_id:
                continue
            launch_name = launch["name"]
            focused_product = launch["product"]
            product_name = focused_product["name"]
            product_id = str(focused_product["id"])
            primary = launch.get("primaryLink")
            primary_url = primary.get("url", "") if isinstance(primary, dict) else ""
            scheduled_at = _timestamp(launch["scheduledAt"], f"Product Hunt post {post_id}")
            if not all(isinstance(value, str) and value for value in (launch_name, product_name, product_id)):
                continue
            if not isinstance(primary_url, str):
                continue
            launches.add((launch_name, product_name, product_id, primary_url, scheduled_at))
        except (KeyError, TypeError, ValueError, ProductHuntError):
            continue
    if len(launches) != 1:
        raise ProductHuntError(
            f"Could not identify one dated focused Product Hunt launch for post {post_id}"
        )
    launch_name, product_name, product_id, primary_url, launch_at = next(iter(launches))
    if _name_key(launch_name) != _name_key(expected_launch_name):
        return None

    # A direct repository link from this exact launch is stronger evidence
    # than the product's Company Info, especially for umbrella product pages.
    primary_slug = _github_slug(primary_url)
    if primary_slug:
        return primary_slug, launch_at, "launch.primaryLink.url"

    # Company Info's githubUrl belongs to the product, not necessarily to a
    # new feature or project launched under it. Require the names to match.
    if _name_key(launch_name) != _name_key(product_name):
        return None
    slug = _focused_product_github_slug(payload, product_id)
    if slug is None:
        return None
    return slug, launch_at, "product.githubUrl"


def _fetch_with_retry(fetch_fn, url: str):
    """Retry brief network/5xx failures, without retrying bad links or limits."""
    for attempt in range(3):
        try:
            return fetch_fn(url)
        except CatalogError as error:
            message = str(error).lower()
            transient = "could not reach" in message or bool(
                re.search(r"http (?:500|502|503|504|520|522|523|524)\b", message)
            )
            if not transient or attempt == 2:
                raise
            time.sleep(0.5 * (2**attempt))


def discover_product_hunt(
    catalog: dict,
    *,
    days: int = 90,
    limit: int = 50,
    now: datetime | None = None,
    fetch_text_fn=None,
    fetch_json_fn=None,
    fetch_direct_link_fn=None,
) -> dict:
    """Return verified launch candidates and source/coverage metadata."""
    validate_catalog(catalog)
    if type(days) is not int or days < 1:
        raise ProductHuntError("days must be a positive integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ProductHuntError("limit must be between 1 and 100")
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        raise ProductHuntError("now needs a timezone")
    now = now.astimezone(timezone.utc)
    fetch_text_fn = fetch_text if fetch_text_fn is None else fetch_text_fn
    fetch_direct_link_fn = (
        fetch_direct_link_location if fetch_direct_link_fn is None else fetch_direct_link_fn
    )
    if fetch_json_fn is None:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        fetch_json_fn = lambda url: fetch_json(url, token=token)

    # Both complete feeds are required. If one is unavailable we cannot know
    # their intersection, so fail rather than publish a misleading empty list.
    parsed = {url: parse_atom_feed(_fetch_with_retry(fetch_text_fn, url), url) for url in FEEDS}
    warnings: list[str] = []
    complete = True
    for url, feed in parsed.items():
        if feed.skipped_entries:
            warnings.append(f"Skipped {feed.skipped_entries} malformed Atom entries from {url}")
            complete = False
        if feed.updated_at < now - timedelta(days=7):
            warnings.append(f"Product Hunt feed appears stale: {url}")
            complete = False
        if feed.updated_at > now + timedelta(days=1):
            warnings.append(f"Product Hunt feed has a future updated timestamp: {url}")
            complete = False

    known = {project["repo"].casefold() for project in catalog["projects"]}
    shared_ids = parsed[AI_FEED].entries.keys() & parsed[OPEN_SOURCE_FEED].entries.keys()
    matched: list[tuple[str, FeedEntry, FeedEntry]] = []
    for post_id in sorted(shared_ids):
        ai_entry = parsed[AI_FEED].entries[post_id]
        open_entry = parsed[OPEN_SOURCE_FEED].entries[post_id]
        if ai_entry.product_hunt_url != open_entry.product_hunt_url:
            warnings.append(f"Product Hunt feeds disagree on product URL for post {post_id}")
            complete = False
            continue
        matched.append((post_id, ai_entry, open_entry))

    page_evidence: dict[str, tuple[str, datetime, str] | None] = {}
    page_issue: str | None = None
    for post_id, ai_entry, _ in matched:
        try:
            page = _fetch_with_retry(fetch_text_fn, ai_entry.product_hunt_url)
            page_evidence[post_id] = product_page_evidence(page, post_id, ai_entry.name)
        except ProductHuntError as error:
            page_issue = str(error)
            break

    direct_mode = page_issue is not None
    mode = "feed_direct_github" if direct_mode else "product_page"
    date_basis = "feed_published" if direct_mode else "actual_launch"
    if direct_mode:
        coverage_note = (
            "Product Hunt product pages could not be verified: "
            f"{page_issue}. This limited policy includes only posts whose feed Link "
            "redirects directly to a GitHub repository. Dates are Atom feed publication "
            "timestamps, not verified launch dates. The feeds expose bounded recent slices."
        )
        method = (
            "Post appears in both official Atom feeds with matching ID, product URL, "
            "name, feed publication timestamp, and outbound Link. The Link redirects directly "
            "to a GitHub repository; GitHub API verifies its public open-source status."
        )
    else:
        coverage_note = (
            "Product Hunt's public feeds expose bounded recent slices. This review "
            "queue does not cover every launch. Dates are verified product-page launch dates."
        )
        method = (
            "Post appears in both official Atom feeds; matching launch gives its actual "
            "scheduledAt and either a direct GitHub primaryLink or a same-name product "
            "page githubUrl; GitHub API confirms a public, non-fork, unarchived "
            "repository with a recognized open-source license."
        )

    # Build evidence under exactly one policy. If a product page is unavailable,
    # do not mix page-derived and feed-derived rows or claim a partial page scan
    # is complete. Every shared post's direct Link must resolve in fallback mode.
    leads: list[tuple[str, FeedEntry, FeedEntry, str, datetime, datetime | None, dict]] = []
    for post_id, ai_entry, open_entry in matched:
        if direct_mode:
            direct_url = ai_entry.direct_link_url
            if (
                not direct_url
                or direct_url != open_entry.direct_link_url
                or _name_key(ai_entry.name) != _name_key(open_entry.name)
                or ai_entry.feed_published_at != open_entry.feed_published_at
            ):
                warnings.append(f"Product Hunt feeds lack matching direct Link evidence for post {post_id}")
                complete = False
                continue
            try:
                location = _fetch_with_retry(fetch_direct_link_fn, direct_url)
            except ProductHuntError as error:
                warnings.append(str(error))
                complete = False
                continue
            if not isinstance(location, str) or not location:
                warnings.append(f"Product Hunt direct Link had no Location for post {post_id}")
                complete = False
                continue
            try:
                parts = urlsplit(location)
                port = parts.port
            except ValueError:
                parts = None
                port = None
            if (
                parts is None
                or parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or port is not None
                or parts.hostname in {"producthunt.com", "www.producthunt.com"}
            ):
                warnings.append(f"Product Hunt direct Link had no external HTTP location for post {post_id}")
                complete = False
                continue
            slug = _github_slug(location)
            if slug is None:
                # A checked redirect to a product website is outside this
                # explicit direct-to-GitHub policy, not a source failure.
                continue
            source_date = ai_entry.feed_published_at
            launch_at = None
            # Keep the exact PH Location for audit. GitHub root links can use
            # HTTP or a .git suffix; the API supplies the canonical repo name.
            evidence_fields = {
                "feed_link_url": direct_url,
                "redirect_location": location,
            }
        else:
            evidence = page_evidence[post_id]
            if evidence is None:
                continue
            slug, launch_at, field = evidence
            source_date = launch_at
            evidence_fields = {
                "product_page_url": ai_entry.product_hunt_url,
                "product_page_field": field,
            }
        leads.append(
            (post_id, ai_entry, open_entry, slug, source_date, launch_at, evidence_fields)
        )

    candidates: list[dict] = []
    for post_id, ai_entry, open_entry, slug, source_date, launch_at, evidence_fields in leads:
        if not now - timedelta(days=days) <= source_date <= now:
            continue
        if slug.casefold() in known:
            continue
        api_url = f"{API_ROOT}/repos/{slug}"
        try:
            repository = _fetch_with_retry(fetch_json_fn, api_url)
        except CatalogError as error:
            warnings.append(str(error))
            complete = False
            continue
        if not isinstance(repository, dict):
            warnings.append(f"GitHub API returned a non-object response for {api_url}")
            complete = False
            continue
        if repository.get("private") is not False:
            continue
        try:
            stats = fetch_repository_stats(slug, lambda _url: repository)
        except CatalogError:
            continue
        description = repository.get("description")
        if not isinstance(description, str) or not description.strip():
            description = ai_entry.tagline or open_entry.tagline or "No description provided"
        candidates.append(
            {
                "repo": repository["full_name"],
                "name": ai_entry.name,
                "description": description,
                "stars": stats["stars"],
                "license": stats["license"],
                "source_date": _utc_iso(source_date),
                "date_basis": date_basis,
                "feed_published_at": _utc_iso(ai_entry.feed_published_at),
                "launch_at": _utc_iso(launch_at) if launch_at is not None else None,
                "product_hunt_url": ai_entry.product_hunt_url,
                "github_url": f"https://github.com/{repository['full_name']}",
                "evidence": {
                    **evidence_fields,
                    "github_api_url": f"{API_ROOT}/repos/{repository['full_name']}",
                    "product_hunt_post_id": post_id,
                },
                "source_feeds": list(FEEDS),
            }
        )
    candidates.sort(key=lambda item: (item["source_date"], item["stars"]), reverse=True)
    deduplicated: list[dict] = []
    seen_repositories: set[str] = set()
    for candidate in candidates:
        canonical = candidate["repo"].casefold()
        if canonical in seen_repositories:
            continue
        seen_repositories.add(canonical)
        deduplicated.append(candidate)
    if page_issue is not None and not complete:
        warnings.insert(0, page_issue)
    return {
        "fetched_at": _utc_iso(now),
        "window_days": days,
        "complete": complete,
        "method": method,
        "matching_posts": len(shared_ids),
        "sources": [
            {
                "url": url,
                "feed_updated_at": _utc_iso(parsed[url].updated_at),
                "entry_count": len(parsed[url].entries),
            }
            for url in FEEDS
        ],
        "coverage": {
            "mode": mode,
            "date_basis": date_basis,
            "note": coverage_note,
        },
        "coverage_note": coverage_note,
        "candidates": deduplicated[:limit],
        "warnings": warnings,
    }


def _write_json(path: Path, snapshot: dict) -> None:
    """Write only a complete JSON document; preserve the last file on error."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".producthunt-", delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90, help="Source-date lookback in days (default: 90)")
    parser.add_argument("--limit", type=int, default=50, help="Maximum candidates (1–100)")
    parser.add_argument("--output", type=Path, help="Write a JSON snapshot to this path; stdout by default")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        catalog = json.loads((args.root / "data" / "projects.json").read_text(encoding="utf-8"))
        snapshot = discover_product_hunt(catalog, days=args.days, limit=args.limit)
        if args.output:
            _write_json(args.output, snapshot)
        else:
            print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    except (CatalogError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
