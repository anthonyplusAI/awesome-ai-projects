"""Offline tests for the Product Hunt launch candidate pipeline."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import producthunt


NOW = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
CATALOG = {
    "categories": [{"id": "agents", "title": "Agents", "description": "Agent projects."}],
    "projects": [
        {
            "repo": "already/listed",
            "name": "Listed",
            "category": "agents",
            "description": "An existing catalog project.",
        }
    ],
}


def feed(entries: list[tuple[str, str, str]], *, updated: str = "2026-10-16T07:01:00Z") -> str:
    items = []
    for post_id, name, product_slug in entries:
        items.append(
            "<entry>"
            f"<id>tag:www.producthunt.com,2005:Post/{post_id}</id>"
            f"<title>{name}</title>"
            f'<link rel="alternate" href="https://www.producthunt.com/products/{product_slug}"/>'
            "<published>2026-08-01T00:00:00Z</published>"
            "<content type=\"html\">&lt;p&gt;An AI project from the feed&lt;/p&gt;</content>"
            "</entry>"
        )
    return (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
        f"<updated>{updated}</updated>{''.join(items)}</feed>"
    )


def page(
    post_id: str,
    github_url: str | None,
    launch_at: str = "2026-10-15T00:01:00-07:00",
    *,
    launch_name: str = "New Agent",
    product_name: str = "New Agent",
    primary_url: str = "https://example.com",
    include_product_record: bool = True,
) -> str:
    product_id = "focused-123"
    payload = {
        "focusedPageVariant": {
            "launch": {
                "__typename": "Post",
                "id": post_id,
                "name": launch_name,
                "product": {"__typename": "Product", "id": product_id, "name": product_name},
                "primaryLink": {"url": primary_url},
                "scheduledAt": launch_at,
            }
        },
    }
    if include_product_record:
        payload["product"] = {
            "__typename": "Product",
            "id": product_id,
            "githubUrl": github_url,
        }
    return "<script>" + json.dumps(payload, separators=(",", ":")) + "</script>"


def github_repo(slug: str, *, license_id: str = "MIT", **updates) -> dict:
    result = {
        "full_name": slug,
        "description": "GitHub's project description",
        "stargazers_count": 120,
        "license": {"spdx_id": license_id},
        "private": False,
        "archived": False,
        "fork": False,
    }
    result.update(updates)
    return result


class ProductHuntParsingTests(unittest.TestCase):
    def test_parses_atom_entry_and_treats_published_as_submission_date_only(self) -> None:
        result = producthunt.parse_atom_feed(feed([("101", "New Agent", "new-agent")]), producthunt.AI_FEED)

        self.assertEqual(set(result.entries), {"101"})
        self.assertEqual(result.entries["101"].name, "New Agent")
        self.assertEqual(result.entries["101"].tagline, "An AI project from the feed")
        self.assertEqual(result.updated_at, datetime(2026, 10, 16, 7, 1, tzinfo=timezone.utc))

    def test_rejects_html_error_page_instead_of_treating_it_as_empty_feed(self) -> None:
        with self.assertRaisesRegex(producthunt.ProductHuntError, "did not return an Atom feed"):
            producthunt.parse_atom_feed("<html><body>Unavailable</body></html>", producthunt.AI_FEED)

    def test_only_explicit_github_url_field_counts_as_link_evidence(self) -> None:
        raw = page("101", None) + '<a href="https://github.com/random/comment-repo">comment</a>'
        self.assertIsNone(producthunt.product_page_evidence(raw, "101", "New Agent"))

    def test_unrelated_product_github_field_is_not_focused_launch_evidence(self) -> None:
        raw = page("101", None) + json.dumps(
            {
                "related": {
                    "product": {
                        "__typename": "Product",
                        "id": "unrelated-999",
                        "githubUrl": "https://github.com/unrelated/repo",
                    }
                }
            },
            separators=(",", ":"),
        )

        self.assertIsNone(producthunt.product_page_evidence(raw, "101", "New Agent"))

    def test_missing_focused_product_link_field_is_source_failure_not_no_link(self) -> None:
        raw = page("101", None, include_product_record=False) + json.dumps(
            {
                "related": {
                    "product": {
                        "__typename": "Product",
                        "id": "unrelated-999",
                        "githubUrl": "https://github.com/unrelated/repo",
                    }
                }
            },
            separators=(",", ":"),
        )

        with self.assertRaisesRegex(producthunt.ProductHuntError, "focused Product githubUrl"):
            producthunt.product_page_evidence(raw, "101", "New Agent")

    def test_rejects_ambiguous_or_non_repository_github_fields(self) -> None:
        ambiguous = page("101", "https://github.com/team/one") + page(
            "101", "https://github.com/team/two"
        )
        self.assertIsNone(producthunt.product_page_evidence(ambiguous, "101", "New Agent"))
        self.assertIsNone(
            producthunt.product_page_evidence(
                page("101", "https://github.com/team/one/tree/main"), "101", "New Agent"
            )
        )

    def test_launch_date_must_match_feed_post_id_not_an_older_product_launch(self) -> None:
        raw = page("100", "https://github.com/team/agent", "2020-01-01T00:00:00Z")
        raw += page("101", "https://github.com/team/agent", "2026-10-15T00:01:00-07:00")

        slug, launch_at, field = producthunt.product_page_evidence(raw, "101", "New Agent")

        self.assertEqual(slug, "team/agent")
        self.assertEqual(launch_at, datetime(2026, 10, 15, 7, 1, tzinfo=timezone.utc))
        self.assertEqual(field, "product.githubUrl")

    def test_umbrella_product_github_field_cannot_be_assigned_to_different_launch(self) -> None:
        raw = page(
            "101",
            "https://github.com/CopilotKit/CopilotKit",
            launch_name="OpenMuse",
            product_name="CopilotKit",
            primary_url="https://www.copilotkit.ai/openmuse",
        )

        self.assertIsNone(producthunt.product_page_evidence(raw, "101", "OpenMuse"))

    def test_launch_specific_github_primary_link_overrides_umbrella_product_link(self) -> None:
        raw = page(
            "101",
            "https://github.com/CopilotKit/CopilotKit",
            launch_name="OpenMuse",
            product_name="CopilotKit",
            primary_url="https://github.com/CopilotKit/openmuse",
        )

        slug, _, field = producthunt.product_page_evidence(raw, "101", "OpenMuse")

        self.assertEqual(slug, "CopilotKit/openmuse")
        self.assertEqual(field, "launch.primaryLink.url")


class ProductHuntDiscoveryTests(unittest.TestCase):
    def _sources(self, *, extra_ai=(), extra_open=(), pages=None, repositories=None, updated=None):
        ai_entries = [("101", "New Agent", "new-agent"), *extra_ai]
        open_entries = [("101", "New Agent", "new-agent"), *extra_open]
        values = {
            producthunt.AI_FEED: feed(ai_entries, updated=updated or "2026-10-16T07:01:00Z"),
            producthunt.OPEN_SOURCE_FEED: feed(open_entries, updated=updated or "2026-10-16T07:01:00Z"),
            "https://www.producthunt.com/products/new-agent": page(
                "101", "https://github.com/new/agent"
            ),
        }
        values.update(pages or {})
        repos = {"new/agent": github_repo("new/agent")}
        repos.update(repositories or {})
        requested = []

        def fetch_text(url):
            requested.append(url)
            return values[url]

        def fetch_json(url):
            requested.append(url)
            slug = url.removeprefix("https://api.github.com/repos/")
            return repos[slug]

        return fetch_text, fetch_json, requested

    def test_emits_only_feed_intersection_with_product_page_and_github_evidence(self) -> None:
        fetch_text, fetch_json, requested = self._sources(
            extra_ai=[("102", "AI only", "ai-only")],
            extra_open=[("103", "Open only", "open-only")],
        )

        snapshot = producthunt.discover_product_hunt(
            CATALOG, now=NOW, fetch_text_fn=fetch_text, fetch_json_fn=fetch_json
        )

        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["matching_posts"], 1)
        self.assertEqual(len(snapshot["candidates"]), 1)
        candidate = snapshot["candidates"][0]
        self.assertEqual(candidate["repo"], "new/agent")
        self.assertEqual(candidate["launch_at"], "2026-10-15T07:01:00Z")
        self.assertEqual(candidate["product_hunt_url"], "https://www.producthunt.com/products/new-agent")
        self.assertEqual(candidate["evidence"]["product_page_field"], "product.githubUrl")
        self.assertEqual(candidate["evidence"]["github_api_url"], "https://api.github.com/repos/new/agent")
        self.assertEqual(candidate["source_feeds"], list(producthunt.FEEDS))
        self.assertNotIn("https://www.producthunt.com/products/ai-only", requested)
        self.assertNotIn("https://www.producthunt.com/products/open-only", requested)

    def test_known_catalog_repo_is_skipped_before_github_api_request(self) -> None:
        fetch_text, fetch_json, requested = self._sources(
            pages={"https://www.producthunt.com/products/new-agent": page("101", "https://github.com/already/listed")}
        )

        snapshot = producthunt.discover_product_hunt(
            CATALOG, now=NOW, fetch_text_fn=fetch_text, fetch_json_fn=fetch_json
        )

        self.assertEqual(snapshot["candidates"], [])
        self.assertFalse(any(url.startswith("https://api.github.com") for url in requested))

    def test_rejects_future_and_old_actual_launch_dates(self) -> None:
        for launch_at in ("2026-10-17T00:00:00Z", "2026-07-01T00:00:00Z"):
            with self.subTest(launch_at=launch_at):
                fetch_text, fetch_json, requested = self._sources(
                    pages={"https://www.producthunt.com/products/new-agent": page(
                        "101", "https://github.com/new/agent", launch_at
                    )}
                )
                snapshot = producthunt.discover_product_hunt(
                    CATALOG, days=90, now=NOW, fetch_text_fn=fetch_text, fetch_json_fn=fetch_json
                )
                self.assertEqual(snapshot["candidates"], [])
                self.assertFalse(any(url.startswith("https://api.github.com") for url in requested))

    def test_filters_private_forks_archives_and_unknown_licenses(self) -> None:
        for patch in (
            {"private": True},
            {"fork": True},
            {"archived": True},
            {"license": {"spdx_id": "NOASSERTION"}},
        ):
            with self.subTest(patch=patch):
                fetch_text, fetch_json, _ = self._sources(
                    repositories={"new/agent": github_repo("new/agent", **patch)}
                )
                snapshot = producthunt.discover_product_hunt(
                    CATALOG, now=NOW, fetch_text_fn=fetch_text, fetch_json_fn=fetch_json
                )
                self.assertEqual(snapshot["candidates"], [])

    def test_source_page_error_is_reported_as_partial_not_no_launches(self) -> None:
        fetch_text, fetch_json, _ = self._sources()

        def fail_page(url):
            if url.startswith("https://www.producthunt.com/products/"):
                raise producthunt.ProductHuntError("Product Hunt returned HTTP 503")
            return fetch_text(url)

        with mock.patch.object(producthunt.time, "sleep"):
            snapshot = producthunt.discover_product_hunt(
                CATALOG, now=NOW, fetch_text_fn=fail_page, fetch_json_fn=fetch_json
            )

        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["candidates"], [])
        self.assertIn("HTTP 503", snapshot["warnings"][0])

    def test_changed_product_page_format_is_reported_as_partial(self) -> None:
        fetch_text, fetch_json, _ = self._sources(
            pages={"https://www.producthunt.com/products/new-agent": "<html>New page layout</html>"}
        )

        snapshot = producthunt.discover_product_hunt(
            CATALOG, now=NOW, fetch_text_fn=fetch_text, fetch_json_fn=fetch_json
        )

        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["candidates"], [])
        self.assertTrue(any("focused Product Hunt launch" in message for message in snapshot["warnings"]))

    def test_missing_focused_github_field_is_partial_but_explicit_null_is_complete(self) -> None:
        product_url = "https://www.producthunt.com/products/new-agent"
        for raw, expected_complete in (
            (page("101", None, include_product_record=False), False),
            (page("101", None), True),
        ):
            with self.subTest(expected_complete=expected_complete):
                fetch_text, fetch_json, _ = self._sources(pages={product_url: raw})

                snapshot = producthunt.discover_product_hunt(
                    CATALOG, now=NOW, fetch_text_fn=fetch_text, fetch_json_fn=fetch_json
                )

                self.assertEqual(snapshot["candidates"], [])
                self.assertEqual(snapshot["complete"], expected_complete)
                if expected_complete:
                    self.assertEqual(snapshot["warnings"], [])
                else:
                    self.assertTrue(
                        any("focused Product githubUrl" in message for message in snapshot["warnings"])
                    )

    def test_retries_transient_github_network_error_once(self) -> None:
        fetch_text, fetch_json, _ = self._sources()
        attempts = 0

        def flaky_github(url):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise producthunt.CatalogError("Could not reach GitHub API: temporary TLS EOF")
            return fetch_json(url)

        with mock.patch.object(producthunt.time, "sleep") as pause:
            snapshot = producthunt.discover_product_hunt(
                CATALOG, now=NOW, fetch_text_fn=fetch_text, fetch_json_fn=flaky_github
            )

        self.assertTrue(snapshot["complete"])
        self.assertEqual(len(snapshot["candidates"]), 1)
        self.assertEqual(attempts, 2)
        pause.assert_called_once()

    def test_stale_feed_is_labeled_incomplete_even_if_matches_are_valid(self) -> None:
        fetch_text, fetch_json, _ = self._sources(updated="2026-10-01T00:00:00Z")

        snapshot = producthunt.discover_product_hunt(
            CATALOG, now=NOW, fetch_text_fn=fetch_text, fetch_json_fn=fetch_json
        )

        self.assertFalse(snapshot["complete"])
        self.assertEqual(len(snapshot["candidates"]), 1)
        self.assertTrue(any("stale" in warning for warning in snapshot["warnings"]))

    def test_feed_failure_does_not_replace_existing_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "projects.json").write_text(json.dumps(CATALOG))
            output = root / "recent.json"
            output.write_text("previous-good-snapshot")

            with mock.patch.object(producthunt, "fetch_text", side_effect=producthunt.ProductHuntError("feed unavailable")):
                with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    result = producthunt.main(["--root", str(root), "--output", str(output)])

            self.assertEqual(result, 1)
            self.assertIn("feed unavailable", stderr.getvalue())
            self.assertEqual(output.read_text(), "previous-good-snapshot")


if __name__ == "__main__":
    unittest.main()
