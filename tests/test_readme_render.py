"""Integration checks for the generated README and its source snapshots."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts import render
from scripts.refresh import CatalogError


CATALOG = {
    "categories": [
        {
            "id": "images",
            "title": "🎨 Image making",
            "description": "Make and remix images.",
        }
    ],
    "projects": [
        {
            "repo": "example/brush",
            "name": "Brush",
            "category": "images",
            "description": "Paint with AI.",
        }
    ],
}
STATS = {
    "fetched_at": "2026-10-16T18:00:00Z",
    "repositories": {
        "example/brush": {"stars": 1200, "license": "MIT", "archived": False}
    },
}
RADAR = {
    "fetched_at": "2026-10-16T18:01:00Z",
    "source_updated_at": "2026-10-16T17:59:00Z",
    "source_url": render.MONTHLY_SOURCE,
    "period": "monthly",
    "repositories": [
        {
            "repo": "example/brush",
            "rank": 1,
            "stars_this_month": 123,
            "stars_total": 1200,
            "license": "MIT",
            "description": "Creative | local AI",
            "curated": True,
            "github_url": "https://github.com/example/brush",
            "source_url": render.MONTHLY_SOURCE,
            "evidence_urls": [
                render.MONTHLY_SOURCE,
                "https://github.com/example/brush",
                "https://api.github.com/repos/example/brush",
            ],
        }
    ],
}
LAUNCHES = {
    "fetched_at": "2026-10-16T18:02:00Z",
    "window_days": 90,
    "complete": True,
    "warnings": [],
    "sources": [
        {
            "url": url,
            "feed_updated_at": "2026-10-16T17:58:00Z",
            "entry_count": 50,
        }
        for url in render.PH_FEEDS
    ],
    "candidates": [
        {
            "repo": "new/tool",
            "name": "Tool Launch",
            "description": "A tool | for AI",
            "stars": 42,
            "license": "Apache-2.0",
            "launch_at": "2026-10-15T00:00:00Z",
            "product_hunt_url": "https://www.producthunt.com/products/tool-launch",
            "github_url": "https://github.com/new/tool",
            "source_feeds": list(render.PH_FEEDS),
            "evidence": {
                "product_page_url": "https://www.producthunt.com/products/tool-launch",
                "product_page_field": "githubUrl",
                "github_api_url": "https://api.github.com/repos/new/tool",
                "product_hunt_post_id": "123456",
            },
        }
    ],
}
README = """# Guide

<!-- STATUS:START -->
old status
<!-- STATUS:END -->

<!-- RADAR:START -->
old radar
<!-- RADAR:END -->

<!-- LAUNCHES:START -->
old launches
<!-- LAUNCHES:END -->

<!-- CATALOG:START -->
old catalog
<!-- CATALOG:END -->
"""


def write_fixture(root: Path, launches: dict | None = None) -> Path:
    (root / "data").mkdir()
    data = {
        "projects.json": CATALOG,
        "stats.json": STATS,
        "radar.json": RADAR,
        "producthunt-recent.json": LAUNCHES if launches is None else launches,
    }
    for name, value in data.items():
        (root / "data" / name).write_text(json.dumps(value), encoding="utf-8")
    readme_path = root / "README.md"
    readme_path.write_text(README, encoding="utf-8")
    return readme_path


class ReadmeRenderTests(unittest.TestCase):
    def test_all_sections_rebuild_from_dated_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme_path = write_fixture(root)

            self.assertTrue(render.render_all(root))
            result = readme_path.read_text(encoding="utf-8")

            self.assertIn("**1 selected project**", result)
            self.assertIn("**1 monthly signal**", result)
            self.assertIn("**1 new launch lead**", result)
            self.assertIn("**+123**", result)
            self.assertIn("Creative \\| local AI", result)
            self.assertIn("[Tool Launch · Oct 15, 2026]", result)
            self.assertIn("A tool \\| for AI", result)
            self.assertIn("⭐ 42 · Apache-2.0", result)
            self.assertIn("**1 pick.** Make and remix images.", result)
            self.assertIn("**1,200**", result)
            self.assertNotIn("old status", result)
            self.assertFalse(render.render_all(root, check=True))

    def test_check_reports_drift_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme_path = write_fixture(root)
            original = readme_path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(CatalogError, "out of date"):
                render.render_all(root, check=True)

            self.assertEqual(readme_path.read_text(encoding="utf-8"), original)

    def test_incomplete_source_cannot_publish_a_readme(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incomplete = copy.deepcopy(LAUNCHES)
            incomplete["complete"] = False
            incomplete["warnings"] = ["Product Hunt feed unavailable"]
            readme_path = write_fixture(root, launches=incomplete)
            original = readme_path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(CatalogError, "complete warning-free"):
                render.render_all(root)

            self.assertEqual(readme_path.read_text(encoding="utf-8"), original)

    def test_launch_link_must_match_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = copy.deepcopy(LAUNCHES)
            bad["candidates"][0]["product_hunt_url"] = (
                "https://www.producthunt.com/products/other-launch"
            )
            readme_path = write_fixture(root, launches=bad)
            original = readme_path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(CatalogError, "evidence is incomplete"):
                render.render_all(root)

            self.assertEqual(readme_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
