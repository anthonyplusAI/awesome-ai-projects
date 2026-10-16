"""Offline behavior tests for catalog maintenance commands."""

from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from scripts import discover, refresh


CATALOG = {
    "categories": [
        {
            "id": "images",
            "title": "Image making",
            "description": "Tools for images.",
        }
    ],
    "projects": [
        {
            "repo": "example/brush",
            "name": "Brush | Bloom",
            "category": "images",
            "description": "Photo <craft>\nwith AI",
            "product_hunt": "https://www.producthunt.com/products/brush",
        }
    ],
}

STATS = {
    "fetched_at": "2026-10-13T12:00:00Z",
    "repositories": {
        "example/brush": {"stars": 12345, "license": "MIT", "archived": False}
    },
}


class CatalogValidationTests(unittest.TestCase):
    def test_rejects_same_repository_with_different_case(self) -> None:
        catalog = copy.deepcopy(CATALOG)
        catalog["projects"].append(
            {
                "repo": "Example/BRUSH",
                "name": "Duplicate",
                "category": "images",
                "description": "Another listing",
            }
        )

        with self.assertRaisesRegex(refresh.CatalogError, "duplicate repository"):
            refresh.validate_catalog(catalog)

    def test_rejects_project_with_unknown_category(self) -> None:
        catalog = copy.deepcopy(CATALOG)
        catalog["projects"][0]["category"] = "audio"

        with self.assertRaisesRegex(refresh.CatalogError, "unknown category"):
            refresh.validate_catalog(catalog)

    def test_rejects_unknown_or_non_open_license_in_stats(self) -> None:
        stats = copy.deepcopy(STATS)
        stats["repositories"]["example/brush"]["license"] = "NOASSERTION"

        with self.assertRaisesRegex(refresh.CatalogError, "license"):
            refresh.validate_stats(stats, CATALOG)

    def test_accepts_simple_open_source_dual_license_expression(self) -> None:
        stats = copy.deepcopy(STATS)
        stats["repositories"]["example/brush"]["license"] = "MIT OR Apache-2.0"

        refresh.validate_stats(stats, CATALOG)

        stats["repositories"]["example/brush"]["license"] = "MIT OR CC-BY-NC-4.0"
        with self.assertRaisesRegex(refresh.CatalogError, "license"):
            refresh.validate_stats(stats, CATALOG)


class CatalogRenderingTests(unittest.TestCase):
    def test_renders_category_table_with_escaped_text_and_exact_stars(self) -> None:
        rendered = refresh.render_catalog(CATALOG, STATS)

        self.assertIn("2026-10-13 PT", rendered)
        self.assertIn('<a id="images"></a>\n\n### Image making', rendered)
        self.assertIn("**1 pick.** Tools for images.", rendered)
        self.assertIn("| Project | Why try it | ⭐ Stars |", rendered)
        self.assertIn(
            "| [Brush \\| Bloom](https://github.com/example/brush) · `MIT` · "
            "[🏹 Product Hunt](https://www.producthunt.com/products/brush) | "
            "Photo &lt;craft&gt; with AI | **12,345** |",
            rendered,
        )

    def test_omits_launch_link_when_project_has_no_product_hunt_listing(self) -> None:
        catalog = copy.deepcopy(CATALOG)
        catalog["projects"].append(
            {
                "repo": "new/voice",
                "name": "Voice",
                "category": "images",
                "description": "Local audio editing",
            }
        )
        stats = copy.deepcopy(STATS)
        stats["repositories"]["new/voice"] = {
            "stars": 25,
            "license": "MIT",
            "archived": False,
        }

        rendered = refresh.render_catalog(catalog, stats)

        self.assertIn(
            "| [Voice](https://github.com/new/voice) · `MIT` | "
            "Local audio editing | **25** |",
            rendered,
        )

    def test_uses_pacific_calendar_date_for_utc_evening_snapshot(self) -> None:
        stats = copy.deepcopy(STATS)
        stats["fetched_at"] = "2026-10-14T01:00:00Z"

        rendered = refresh.render_catalog(CATALOG, stats)

        self.assertIn("2026-10-13 PT", rendered)
        self.assertNotIn("2026-10-14 PT", rendered)
        self.assertIn("Documented license exceptions are not automatically rechecked.", rendered)

    def test_replaces_only_content_between_markers(self) -> None:
        old = (
            "# Title\n\nIntro\n<!-- CATALOG:START -->\n"
            "Old list\n<!-- CATALOG:END -->\n\nTail\n"
        )

        updated = refresh.replace_catalog(old, "New list\n")

        self.assertEqual(
            updated,
            "# Title\n\nIntro\n<!-- CATALOG:START -->\n"
            "New list\n<!-- CATALOG:END -->\n\nTail\n",
        )

    def test_rejects_missing_or_duplicate_markers(self) -> None:
        with self.assertRaisesRegex(refresh.CatalogError, "exactly one"):
            refresh.replace_catalog("# No markers\n", "content")
        with self.assertRaisesRegex(refresh.CatalogError, "exactly one"):
            refresh.replace_catalog(
                "<!-- CATALOG:START --><!-- CATALOG:START --><!-- CATALOG:END -->",
                "content",
            )


class GithubApiTests(unittest.TestCase):
    def test_fetches_repository_snapshot_from_api_fixture(self) -> None:
        def fixture(url: str) -> dict:
            self.assertEqual(url, "https://api.github.com/repos/example/brush")
            return {
                "full_name": "example/brush",
                "stargazers_count": 12345,
                "private": False,
                "archived": False,
                "fork": False,
                "license": {"spdx_id": "MIT", "name": "MIT License"},
            }

        self.assertEqual(
            refresh.fetch_repository_stats("example/brush", fixture),
            {"stars": 12345, "license": "MIT", "archived": False},
        )

    def test_rejects_archived_repository_from_api_fixture(self) -> None:
        def fixture(_url: str) -> dict:
            return {
                "full_name": "example/brush",
                "stargazers_count": 12345,
                "private": False,
                "archived": True,
                "fork": False,
                "license": {"spdx_id": "MIT", "name": "MIT License"},
            }

        with self.assertRaisesRegex(refresh.CatalogError, "archived"):
            refresh.fetch_repository_stats("example/brush", fixture)

    def test_rejects_forked_repository_from_api_fixture(self) -> None:
        def fixture(_url: str) -> dict:
            return {
                "full_name": "example/brush",
                "stargazers_count": 12345,
                "private": False,
                "archived": False,
                "fork": True,
                "license": {"spdx_id": "MIT"},
            }

        with self.assertRaisesRegex(refresh.CatalogError, "fork"):
            refresh.fetch_repository_stats("example/brush", fixture)

    def test_rejects_private_repository_even_with_valid_license(self) -> None:
        def fixture(_url: str) -> dict:
            return {
                "full_name": "example/brush",
                "stargazers_count": 12345,
                "private": True,
                "archived": False,
                "fork": False,
                "license": {"spdx_id": "MIT"},
            }

        with self.assertRaisesRegex(refresh.CatalogError, "private"):
            refresh.fetch_repository_stats("example/brush", fixture)

    def test_preserves_verified_license_when_github_reports_noassertion(self) -> None:
        def fixture(_url: str) -> dict:
            return {
                "full_name": "example/brush",
                "stargazers_count": 12345,
                "private": False,
                "archived": False,
                "fork": False,
                "license": {"spdx_id": "NOASSERTION"},
            }

        self.assertEqual(
            refresh.fetch_repository_stats(
                "example/brush", fixture, existing_license="MIT OR Apache-2.0"
            )["license"],
            "MIT OR Apache-2.0",
        )
        with self.assertRaisesRegex(refresh.CatalogError, "license"):
            refresh.fetch_repository_stats("example/brush", fixture)

    def test_documented_license_override_survives_github_noassertion(self) -> None:
        catalog = copy.deepcopy(CATALOG)
        catalog["projects"][0]["license_override"] = {
            "spdx_id": "MIT",
            "evidence_url": "https://github.com/example/brush/blob/main/LICENSE",
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "projects.json").write_text(json.dumps(catalog))
            (root / "data" / "stats.json").write_text(
                json.dumps({"fetched_at": "2026-10-13T12:00:00Z", "repositories": {}})
            )
            (root / "README.md").write_text(
                "<!-- CATALOG:START -->\nold\n<!-- CATALOG:END -->\n"
            )

            def fixture(_url: str) -> dict:
                return {
                    "full_name": "example/brush",
                    "stargazers_count": 42,
                    "private": False,
                    "archived": False,
                    "fork": False,
                    "license": {"spdx_id": "NOASSERTION"},
                }

            try:
                refresh.refresh_sync(root, fixture)
            except refresh.CatalogError as error:
                self.fail(f"documented MIT license was not used: {error}")
            saved = json.loads((root / "data" / "stats.json").read_text())

        self.assertEqual(saved["repositories"]["example/brush"]["license"], "MIT")

    def test_preserves_dual_license_when_github_picks_one_branch(self) -> None:
        for reported in ("MIT", "Apache-2.0"):
            with self.subTest(reported=reported):
                def fixture(_url: str) -> dict:
                    return {
                        "full_name": "example/brush",
                        "stargazers_count": 12345,
                        "private": False,
                        "archived": False,
                        "fork": False,
                        "license": {"spdx_id": reported},
                    }

                self.assertEqual(
                    refresh.fetch_repository_stats(
                        "example/brush", fixture, existing_license="MIT OR Apache-2.0"
                    )["license"],
                    "MIT OR Apache-2.0",
                )

    def test_rate_limit_response_has_clear_error(self) -> None:
        def limited(_request, timeout=None):
            raise HTTPError(
                "https://api.github.com/repos/example/brush",
                403,
                "Forbidden",
                {"X-RateLimit-Remaining": "0"},
                io.BytesIO(b'{"message":"API rate limit exceeded"}'),
            )

        with self.assertRaisesRegex(refresh.CatalogError, "rate limit"):
            refresh.fetch_json(
                "https://api.github.com/repos/example/brush", opener=limited
            )

    def test_transient_transport_failure_retries_and_returns_repository(self) -> None:
        attempts = 0

        def flaky(_request, timeout=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise URLError("TLS connection closed unexpectedly")
            return io.BytesIO(b'{"full_name":"example/brush","stargazers_count":42}')

        with mock.patch("time.sleep"):
            try:
                result = refresh.fetch_json(
                    "https://api.github.com/repos/example/brush", opener=flaky
                )
            except refresh.CatalogError as error:
                self.fail(f"transient transport failure was not retried: {error}")

        self.assertEqual(result["full_name"], "example/brush")
        self.assertEqual(result["stargazers_count"], 42)
        self.assertEqual(attempts, 2)

    def test_default_urlopen_receives_verified_fallback_context(self) -> None:
        context = object()
        received_contexts = []

        def fake_urlopen(_request, timeout=None, context=None):
            received_contexts.append(context)
            return io.BytesIO(b'{"ok":true}')

        with mock.patch.object(refresh, "_fallback_ssl_context", return_value=context):
            with mock.patch.object(refresh, "urlopen", fake_urlopen):
                response = refresh.fetch_json("https://api.github.com/test")

        self.assertEqual(response, {"ok": True})
        self.assertEqual(received_contexts, [context])

    def test_refresh_writes_nothing_until_all_repositories_are_valid(self) -> None:
        catalog = copy.deepcopy(CATALOG)
        catalog["projects"].append(
            {
                "repo": "new/voice",
                "name": "Voice",
                "category": "images",
                "description": "Audio demo",
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "projects.json").write_text(json.dumps(catalog))
            (root / "data" / "stats.json").write_text(json.dumps(STATS))
            readme = root / "README.md"
            readme.write_text("Before\n<!-- CATALOG:START -->\nold\n<!-- CATALOG:END -->\nAfter\n")
            original_readme = readme.read_text()
            original_stats = (root / "data" / "stats.json").read_text()

            def fixture(url: str) -> dict:
                slug = url.removeprefix("https://api.github.com/repos/")
                return {
                    "full_name": slug,
                    "stargazers_count": 25 if slug == "new/voice" else 12345,
                    "private": False,
                    "archived": slug == "new/voice",
                    "fork": False,
                    "license": {"spdx_id": "MIT"},
                }

            with self.assertRaisesRegex(refresh.CatalogError, "archived"):
                refresh.refresh_sync(
                    root, fixture, now=datetime(2026, 10, 13, 16, 45, tzinfo=timezone.utc)
                )

            self.assertEqual(readme.read_text(), original_readme)
            self.assertEqual((root / "data" / "stats.json").read_text(), original_stats)

            def valid_fixture(url: str) -> dict:
                payload = fixture(url)
                payload["archived"] = False
                if payload["full_name"] == "example/brush":
                    payload["license"] = {"spdx_id": "NOASSERTION"}
                return payload

            refresh.refresh_sync(
                root, valid_fixture, now=datetime(2026, 10, 13, 16, 45, tzinfo=timezone.utc)
            )

            self.assertEqual(
                json.loads((root / "data" / "stats.json").read_text())["fetched_at"],
                "2026-10-13T16:45:00Z",
            )
            self.assertEqual(
                json.loads((root / "data" / "stats.json").read_text())["repositories"]
                ["example/brush"]["license"],
                "MIT",
            )
            self.assertIn("Audio demo | **25** |", readme.read_text())
            self.assertTrue(readme.read_text().endswith("\nAfter\n"))


class OfflineCommandTests(unittest.TestCase):
    def test_check_reports_drift_and_offline_sync_repairs_only_readme(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "projects.json").write_text(json.dumps(CATALOG))
            (root / "data" / "stats.json").write_text(json.dumps(STATS))
            readme = root / "README.md"
            readme.write_text(
                "# Title\n\n<!-- CATALOG:START -->\nold\n"
                "<!-- CATALOG:END -->\n\nTail\n"
            )
            original_stats = (root / "data" / "stats.json").read_text()

            with self.assertRaisesRegex(refresh.CatalogError, "out of date"):
                refresh.offline_sync(root, check=True)
            self.assertIn("\nold\n", readme.read_text())

            refresh.offline_sync(root)

            self.assertIn("12,345", readme.read_text())
            self.assertTrue(readme.read_text().endswith("\n\nTail\n"))
            self.assertEqual((root / "data" / "stats.json").read_text(), original_stats)
            refresh.offline_sync(root, check=True)


class SslFallbackTests(unittest.TestCase):
    def test_macos_missing_python_bundle_uses_system_ca_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            system_ca = Path(directory) / "cert.pem"
            system_ca.write_text("fixture")
            context = object()
            with mock.patch.object(refresh, "SYSTEM_CA_BUNDLE", system_ca), \
                mock.patch.object(refresh, "sys", SimpleNamespace(platform="darwin")), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(
                    refresh.ssl,
                    "get_default_verify_paths",
                    return_value=SimpleNamespace(cafile=None),
                ), \
                mock.patch.object(
                    refresh.ssl, "create_default_context", return_value=context
                ) as create_context:
                selected = refresh._fallback_ssl_context()

            self.assertIs(selected, context)
            create_context.assert_called_once_with(cafile=str(system_ca))

    def test_ssl_cert_file_override_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            system_ca = Path(directory) / "cert.pem"
            system_ca.write_text("fixture")
            with mock.patch.object(refresh, "SYSTEM_CA_BUNDLE", system_ca), \
                mock.patch.object(refresh, "sys", SimpleNamespace(platform="darwin")), \
                mock.patch.dict(os.environ, {"SSL_CERT_FILE": "/custom/ca.pem"}, clear=True), \
                mock.patch.object(
                    refresh.ssl,
                    "get_default_verify_paths",
                    return_value=SimpleNamespace(cafile=None),
                ), \
                mock.patch.object(refresh.ssl, "create_default_context") as create_context:
                selected = refresh._fallback_ssl_context()

            self.assertIsNone(selected)
            create_context.assert_not_called()

    def test_existing_python_bundle_needs_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            system_ca = Path(directory) / "cert.pem"
            system_ca.write_text("fixture")
            with mock.patch.object(refresh, "SYSTEM_CA_BUNDLE", system_ca), \
                mock.patch.object(refresh, "sys", SimpleNamespace(platform="darwin")), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(
                    refresh.ssl,
                    "get_default_verify_paths",
                    return_value=SimpleNamespace(cafile="/python/ca.pem"),
                ), \
                mock.patch.object(refresh.ssl, "create_default_context") as create_context:
                selected = refresh._fallback_ssl_context()

            self.assertIsNone(selected)
            create_context.assert_not_called()

    def test_other_platforms_do_not_use_macos_fallback(self) -> None:
        with mock.patch.object(refresh, "sys", SimpleNamespace(platform="linux")), \
            mock.patch.dict(os.environ, {}, clear=True), \
            mock.patch.object(refresh.ssl, "create_default_context") as create_context:
            selected = refresh._fallback_ssl_context()

        self.assertIsNone(selected)
        create_context.assert_not_called()


class DiscoveryTests(unittest.TestCase):
    def test_searches_recent_topics_and_filters_uncurated_candidates(self) -> None:
        def candidate(slug: str, stars: int, license_id: str | None, **flags) -> dict:
            return {
                "full_name": slug,
                "description": f"About {slug}",
                "stargazers_count": stars,
                "fork": flags.get("fork", False),
                "archived": flags.get("archived", False),
                "license": {"spdx_id": license_id} if license_id else None,
                "topics": ["llm"],
            }

        calls: list[str] = []

        def fixture(url: str) -> dict:
            query = parse_qs(urlparse(url).query)["q"][0]
            calls.append(query)
            if "topic:llm" in query:
                return {
                    "incomplete_results": False,
                    "items": [
                        candidate("example/brush", 1000, "MIT"),
                        candidate("new/agent", 90, "Apache-2.0"),
                        candidate("fork/copy", 900, "MIT", fork=True),
                        candidate("closed/source", 800, None),
                        candidate("unknown/license", 700, "NOASSERTION"),
                    ],
                }
            return {
                "incomplete_results": False,
                "items": [
                    candidate("new/agent", 90, "Apache-2.0"),
                    candidate("new/voice", 70, "MIT"),
                ],
            }

        results = discover.find_candidates(
            CATALOG,
            since=date(2026, 9, 13),
            topics=("llm", "ai-agent"),
            min_stars=50,
            limit=10,
            fetch_json_fn=fixture,
        )

        self.assertEqual([item["full_name"] for item in results], ["new/agent", "new/voice"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("created:>=2026-09-13" in query for query in calls))
        self.assertIn("| [new/agent](https://github.com/new/agent) | 90 | Apache-2.0 |", discover.render_candidates(results, date(2026, 9, 13)))


if __name__ == "__main__":
    unittest.main()
