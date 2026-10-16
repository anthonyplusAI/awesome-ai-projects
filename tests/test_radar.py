"""Monthly Trending radar behavior against complete, controlled GitHub responses."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from scripts import radar


TRENDING_URL = "https://github.com/trending?since=monthly"
SOURCE_TIME = "2026-10-16T12:00:00Z"
NOW = datetime(2026, 10, 16, 13, 4, 5, tzinfo=timezone.utc)

CATALOG = {
    "categories": [
        {"id": "voice", "title": "Voice", "description": "Speech tools."}
    ],
    "projects": [
        {
            "repo": "example/voice",
            "name": "Voice",
            "category": "voice",
            "description": "AI voice studio",
        }
    ],
}
STATS = {
    "fetched_at": "2026-10-15T12:00:00Z",
    "repositories": {
        "example/voice": {
            "stars": 500,
            "license": "MIT OR Apache-2.0",
            "archived": False,
        }
    },
}


def trending_row(slug: str, rank: int, monthly: int = 120) -> dict:
    return {
        "repo": slug,
        "rank": rank,
        "stars_this_month": monthly,
        "stars_total": 1000,
        "description": f"Trending description of {slug}",
        "language": "Python",
    }


def source(*rows: dict) -> dict:
    return {
        "source_url": TRENDING_URL,
        "period": "monthly",
        "fetched_at": SOURCE_TIME,
        "repositories": list(rows),
    }


def repository(
    slug: str,
    *,
    description: str = "An AI assistant",
    topics: list[str] | None = None,
    license_id: str = "MIT",
    stars: int = 1200,
    fork: bool = False,
    archived: bool = False,
    private: bool = False,
) -> dict:
    return {
        "full_name": slug,
        "html_url": f"https://github.com/{slug}",
        "description": description,
        "topics": topics if topics is not None else ["artificial-intelligence"],
        "license": {"spdx_id": license_id},
        "stargazers_count": stars,
        "fork": fork,
        "archived": archived,
        "private": private,
    }


def api_fixture(responses: dict[str, dict]):
    def fetch(url: str) -> dict:
        prefix = "https://api.github.com/repos/"
        if not url.startswith(prefix):
            raise AssertionError(f"Unexpected API URL: {url}")
        return responses[url.removeprefix(prefix)]

    return fetch


class RadarSelectionTests(unittest.TestCase):
    def test_uses_page_rank_monthly_gain_and_fresh_repository_metadata(self) -> None:
        snapshot = radar.build_radar(
            source(
                trending_row("example/voice", 3, 45),
                trending_row("new/agent", 1, 530),
                trending_row("new/non-ai", 2, 900),
            ),
            CATALOG,
            STATS,
            api_fixture(
                {
                    "new/agent": repository(
                        "new/agent",
                        description="Build <script> | [agents] with LLMs",
                        topics=[],
                        license_id="Apache-2.0",
                        stars=4321,
                    ),
                    "new/non-ai": repository(
                        "new/non-ai", description="A fast database", topics=[]
                    ),
                    "example/voice": repository(
                        "example/voice",
                        description="Creative speech tool",
                        topics=[],
                        license_id="NOASSERTION",
                        stars=777,
                    ),
                }
            ),
            now=NOW,
        )

        self.assertEqual(snapshot["source_url"], TRENDING_URL)
        self.assertEqual(snapshot["period"], "monthly")
        self.assertEqual(snapshot["source_updated_at"], SOURCE_TIME)
        self.assertEqual(snapshot["fetched_at"], "2026-10-16T13:04:05Z")
        self.assertIn("not editorial endorsements", snapshot["signal"])
        self.assertEqual(
            [item["repo"] for item in snapshot["repositories"]],
            ["new/agent", "example/voice"],
        )
        first, second = snapshot["repositories"]
        self.assertEqual((first["rank"], first["stars_this_month"], first["stars_total"]), (1, 530, 4321))
        self.assertEqual(first["license"], "Apache-2.0")
        self.assertEqual(first["description"], "Build <script> | [agents] with LLMs")
        self.assertFalse(first["curated"])
        self.assertEqual(
            first["evidence_urls"],
            [
                TRENDING_URL,
                "https://github.com/new/agent",
                "https://api.github.com/repos/new/agent",
            ],
        )
        self.assertTrue(second["curated"])
        self.assertEqual(second["license"], "MIT OR Apache-2.0")
        self.assertEqual(second["stars_total"], 500)

    def test_curated_total_matches_catalog_snapshot_when_live_stars_change(self) -> None:
        snapshot = radar.build_radar(
            source(trending_row("example/voice", 1, monthly=45)),
            CATALOG,
            STATS,
            api_fixture(
                {
                    "example/voice": repository(
                        "example/voice",
                        description="AI voice studio",
                        license_id="MIT",
                        stars=501,
                    )
                }
            ),
            now=NOW,
        )

        row = snapshot["repositories"][0]
        self.assertTrue(row["curated"])
        self.assertEqual(row["stars_this_month"], 45)
        self.assertEqual(row["stars_total"], STATS["repositories"]["example/voice"]["stars"])

    def test_rejects_forks_archives_private_repos_non_oss_and_wrong_slugs(self) -> None:
        rows = [
            trending_row("new/fork", 1),
            trending_row("new/archived", 2),
            trending_row("new/private", 3),
            trending_row("new/non-oss", 4),
            trending_row("new/redirect", 5),
            trending_row("new/valid", 6),
        ]
        responses = {
            "new/fork": repository("new/fork", fork=True),
            "new/archived": repository("new/archived", archived=True),
            "new/private": repository("new/private", private=True),
            "new/non-oss": repository("new/non-oss", license_id="NOASSERTION"),
            "new/redirect": repository("new/renamed"),
            "new/valid": repository("new/valid"),
        }

        snapshot = radar.build_radar(source(*rows), CATALOG, STATS, api_fixture(responses), now=NOW)

        self.assertEqual([entry["repo"] for entry in snapshot["repositories"]], ["new/valid"])

    def test_ignores_generic_topics_and_near_matches_to_ai(self) -> None:
        snapshot = radar.build_radar(
            source(
                trending_row("new/maintainer", 1),
                trending_row("new/automations", 2),
                trending_row("new/llm", 3),
            ),
            CATALOG,
            STATS,
            api_fixture(
                {
                    "new/maintainer": repository(
                        "new/maintainer", description="Maintainer tools", topics=["python"]
                    ),
                    "new/automations": repository(
                        "new/automations", description="Task automation", topics=["automation"]
                    ),
                    "new/llm": repository(
                        "new/llm", description="A local language model", topics=[]
                    ),
                }
            ),
            now=NOW,
        )

        self.assertEqual([entry["repo"] for entry in snapshot["repositories"]], ["new/llm"])

    def test_reserves_a_slot_for_a_qualified_curated_repository(self) -> None:
        rows = [trending_row(f"new/agent-{number}", number) for number in range(1, 10)]
        rows.append(trending_row("example/voice", 10))
        responses = {
            f"new/agent-{number}": repository(f"new/agent-{number}")
            for number in range(1, 10)
        }
        responses["example/voice"] = repository(
            "example/voice", description="Speech tool", topics=[], license_id="NOASSERTION"
        )

        snapshot = radar.build_radar(source(*rows), CATALOG, STATS, api_fixture(responses), now=NOW)

        self.assertEqual([entry["rank"] for entry in snapshot["repositories"]], [1, 2, 3, 4, 5, 6, 7, 10])
        self.assertEqual(snapshot["repositories"][-1]["repo"], "example/voice")
        self.assertTrue(snapshot["repositories"][-1]["curated"])

    def test_limits_long_descriptions_and_collapses_whitespace(self) -> None:
        text = "AI  \n" + ("explorer " * 50) + "<img src=x> | [link](https://evil.test)"
        snapshot = radar.build_radar(
            source(trending_row("new/long", 1)),
            CATALOG,
            STATS,
            api_fixture({"new/long": repository("new/long", description=text, topics=[])}),
            now=NOW,
        )

        description = snapshot["repositories"][0]["description"]
        self.assertLessEqual(len(description), 160)
        self.assertTrue(description.endswith("…"))
        self.assertTrue(description.startswith("AI explorer"))
        self.assertNotIn("\n", description)
        self.assertNotIn("<img", description)
        self.assertNotIn("https://evil.test", description)

    def test_removes_unprintable_controls_from_plain_description(self) -> None:
        snapshot = radar.build_radar(
            source(trending_row("new/agent", 1)),
            CATALOG,
            STATS,
            api_fixture(
                {"new/agent": repository("new/agent", description="AI agent\x00\nwith\ttools", topics=[])}
            ),
            now=NOW,
        )

        self.assertEqual(snapshot["repositories"][0]["description"], "AI agent with tools")

    def test_uses_placeholder_when_description_has_only_controls(self) -> None:
        row = trending_row("example/voice", 1)
        row["description"] = "\x00"
        snapshot = radar.build_radar(
            source(row),
            CATALOG,
            STATS,
            api_fixture(
                {"example/voice": repository("example/voice", description="\x00", topics=[])}
            ),
            now=NOW,
        )

        self.assertEqual(snapshot["repositories"][0]["description"], "No description provided")

    def test_retries_one_transient_transport_error(self) -> None:
        calls = 0

        def temporarily_unavailable(_url: str) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise radar.CatalogError("Could not reach GitHub API for test: TLS EOF")
            return repository("new/agent")

        snapshot = radar.build_radar(
            source(trending_row("new/agent", 1)),
            CATALOG,
            STATS,
            temporarily_unavailable,
            now=NOW,
        )

        self.assertEqual(snapshot["repositories"][0]["repo"], "new/agent")
        self.assertEqual(calls, 2)

    def test_skips_a_repository_that_disappeared_after_trending_capture(self) -> None:
        def missing_first(url: str) -> dict:
            if url.endswith("/new/gone"):
                raise radar.CatalogError(f"GitHub API HTTP 404 for {url}: Not Found")
            return repository("new/agent")

        snapshot = radar.build_radar(
            source(trending_row("new/gone", 1), trending_row("new/agent", 2)),
            CATALOG,
            STATS,
            missing_first,
            now=NOW,
        )

        self.assertEqual([row["repo"] for row in snapshot["repositories"]], ["new/agent"])
        self.assertEqual(snapshot["repositories"][0]["rank"], 2)


class RadarFailureTests(unittest.TestCase):
    def test_rejects_empty_and_malformed_trending_sources_before_fetching(self) -> None:
        bad_sources = [
            source(),
            {**source(trending_row("new/agent", 1)), "source_url": "https://example.com/trending"},
            {**source(trending_row("new/agent", 1)), "period": "daily"},
            source(trending_row("new/agent", 1), trending_row("NEW/AGENT", 2)),
            source({**trending_row("new/agent", 1), "stars_this_month": "100"}),
            source({**trending_row("new/agent", 1), "stars_this_month": 0}),
            source({**trending_row("new/agent", 1), "rank": 0}),
            source({**trending_row("new/agent", 1), "rank": 2}),
        ]
        def should_not_fetch(_url: str) -> dict:
            raise AssertionError("Malformed source must be rejected before network access")

        for malformed in bad_sources:
            with self.subTest(malformed=malformed):
                with self.assertRaises(radar.CatalogError):
                    radar.build_radar(malformed, CATALOG, STATS, should_not_fetch, now=NOW)

    def test_rejects_a_valid_source_with_no_qualifying_projects(self) -> None:
        with self.assertRaisesRegex(radar.CatalogError, "no qualifying"):
            radar.build_radar(
                source(trending_row("new/database", 1)),
                CATALOG,
                STATS,
                api_fixture(
                    {"new/database": repository("new/database", description="Fast database", topics=[])}
                ),
                now=NOW,
            )

    def test_cli_preserves_a_good_snapshot_when_a_fetch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "projects.json").write_text(json.dumps(CATALOG), encoding="utf-8")
            (root / "data" / "stats.json").write_text(json.dumps(STATS), encoding="utf-8")
            (root / "data" / "trending-monthly.json").write_text(
                json.dumps(source(trending_row("new/agent", 1))), encoding="utf-8"
            )
            output = root / "data" / "radar.json"
            output.write_text('{"known":"good"}\n', encoding="utf-8")

            def failed_fetch(_url: str) -> dict:
                raise radar.CatalogError("API unavailable")

            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                status = radar.main(
                    ["--root", str(root), "--input", "data/trending-monthly.json", "--output", "data/radar.json"],
                    fetch_json_fn=failed_fetch,
                    now=NOW,
                )

            self.assertEqual(status, 1)
            self.assertEqual(output.read_text(encoding="utf-8"), '{"known":"good"}\n')
            self.assertEqual(sorted(p.name for p in output.parent.iterdir() if p.name.startswith(".radar")), [])

    def test_cli_writes_snapshot_and_defaults_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data" / "projects.json").write_text(json.dumps(CATALOG), encoding="utf-8")
            (root / "data" / "stats.json").write_text(json.dumps(STATS), encoding="utf-8")
            (root / "data" / "trending-monthly.json").write_text(
                json.dumps(source(trending_row("new/agent", 1))), encoding="utf-8"
            )
            args = ["--root", str(root), "--input", "data/trending-monthly.json"]
            fetch = api_fixture({"new/agent": repository("new/agent")})
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = radar.main(args, fetch_json_fn=fetch, now=NOW)

            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout.getvalue())["repositories"][0]["repo"], "new/agent")
            self.assertFalse((root / "data" / "radar.json").exists())
            with redirect_stdout(io.StringIO()):
                status = radar.main(args + ["--output", "data/radar.json"], fetch_json_fn=fetch, now=NOW)
            self.assertEqual(status, 0)
            self.assertEqual(
                json.loads((root / "data" / "radar.json").read_text(encoding="utf-8"))["repositories"][0]["rank"],
                1,
            )


if __name__ == "__main__":
    unittest.main()
