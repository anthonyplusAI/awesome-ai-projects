"""The Product Hunt handoff must preserve a known-good snapshot on capture failure."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from scripts import accept_producthunt, render


CATALOG = {
    "categories": [
        {"id": "agents", "title": "Agents", "description": "AI agents."}
    ],
    "projects": [
        {
            "repo": "curated/agent",
            "name": "Curated Agent",
            "category": "agents",
            "description": "An AI agent.",
        }
    ],
}

SNAPSHOT = {
    "fetched_at": "2026-10-16T18:00:00Z",
    "window_days": 90,
    "complete": True,
    "warnings": [],
    "coverage": {"mode": "product_page", "date_basis": "actual_launch"},
    "sources": [
        {
            "url": url,
            "feed_updated_at": "2026-10-16T17:00:00Z",
            "entry_count": 50,
        }
        for url in render.PH_FEEDS
    ],
    "candidates": [
        {
            "repo": "new/agent",
            "name": "New Agent",
            "description": "A new AI agent.",
            "stars": 42,
            "license": "MIT",
            "launch_at": "2026-10-15T00:00:00Z",
            "source_date": "2026-10-15T00:00:00Z",
            "date_basis": "actual_launch",
            "feed_published_at": "2026-10-14T00:00:00Z",
            "product_hunt_url": "https://www.producthunt.com/products/new-agent",
            "github_url": "https://github.com/new/agent",
            "source_feeds": list(render.PH_FEEDS),
            "evidence": {
                "product_page_url": "https://www.producthunt.com/products/new-agent",
                "product_page_field": "product.githubUrl",
                "github_api_url": "https://api.github.com/repos/new/agent",
                "product_hunt_post_id": "123456",
            },
        }
    ],
}


def write_json(path: Path, value: object) -> str:
    content = json.dumps(value, indent=2) + "\n"
    path.write_text(content, encoding="utf-8")
    return content


class AcceptProductHuntTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        write_json(self.data / "projects.json", CATALOG)
        self.previous = self.data / "producthunt-recent.json"
        self.input = self.root / "candidate.json"

    def run_accept(self) -> tuple[int, str]:
        errors = io.StringIO()
        with redirect_stderr(errors):
            status = accept_producthunt.main(
                ["--input", str(self.input), "--output", str(self.previous)]
            )
        return status, errors.getvalue()

    def test_valid_candidate_atomically_replaces_previous_snapshot(self) -> None:
        old = copy.deepcopy(SNAPSHOT)
        old["fetched_at"] = "2026-10-15T18:00:00Z"
        write_json(self.previous, old)
        replacement = write_json(self.input, SNAPSHOT)

        status, errors = self.run_accept()

        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        self.assertEqual(self.previous.read_text(encoding="utf-8"), replacement)
        self.assertEqual(list(self.data.glob(".producthunt-recent.json.*.tmp")), [])

    def test_incomplete_candidate_keeps_the_verified_snapshot(self) -> None:
        previous = write_json(self.previous, SNAPSHOT)
        incomplete = copy.deepcopy(SNAPSHOT)
        incomplete["complete"] = False
        incomplete["warnings"] = ["Product Hunt feed was unavailable"]
        write_json(self.input, incomplete)

        status, errors = self.run_accept()

        self.assertEqual(status, 0)
        self.assertIn("warning", errors.lower())
        self.assertIn("keeping", errors.lower())
        self.assertEqual(self.previous.read_text(encoding="utf-8"), previous)

    def test_duplicate_or_bad_evidence_cannot_replace_the_snapshot(self) -> None:
        previous = write_json(self.previous, SNAPSHOT)
        duplicate = copy.deepcopy(SNAPSHOT)
        duplicate["candidates"].append(copy.deepcopy(duplicate["candidates"][0]))
        bad_evidence = copy.deepcopy(SNAPSHOT)
        bad_evidence["candidates"][0]["evidence"]["github_api_url"] = (
            "https://api.github.com/repos/unrelated/project"
        )

        for candidate in (duplicate, bad_evidence):
            with self.subTest(candidate=candidate["candidates"][0]["evidence"]):
                write_json(self.input, candidate)
                status, errors = self.run_accept()
                self.assertEqual(status, 0)
                self.assertIn("warning", errors.lower())
                self.assertEqual(self.previous.read_text(encoding="utf-8"), previous)

    def test_missing_or_malformed_candidate_keeps_the_verified_snapshot(self) -> None:
        previous = write_json(self.previous, SNAPSHOT)
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                if malformed:
                    self.input.write_text("{bad json", encoding="utf-8")
                else:
                    self.input.unlink(missing_ok=True)
                status, errors = self.run_accept()
                self.assertEqual(status, 0)
                self.assertIn("warning", errors.lower())
                self.assertEqual(self.previous.read_text(encoding="utf-8"), previous)

    def test_rejected_candidate_without_a_valid_prior_snapshot_fails(self) -> None:
        self.input.write_text("{bad json", encoding="utf-8")

        status, errors = self.run_accept()

        self.assertEqual(status, 1)
        self.assertIn("no valid previous snapshot", errors)
        self.assertFalse(self.previous.exists())

    def test_failed_atomic_replace_preserves_previous_snapshot(self) -> None:
        old = copy.deepcopy(SNAPSHOT)
        old["fetched_at"] = "2026-10-15T18:00:00Z"
        previous = write_json(self.previous, old)
        write_json(self.input, SNAPSHOT)

        with mock.patch.object(accept_producthunt.os, "replace", side_effect=OSError("disk error")):
            status, errors = self.run_accept()

        self.assertEqual(status, 1)
        self.assertIn("disk error", errors)
        self.assertEqual(self.previous.read_text(encoding="utf-8"), previous)
        self.assertEqual(list(self.data.glob(".producthunt-recent.json.*.tmp")), [])

    def test_programming_errors_are_not_hidden_as_source_warnings(self) -> None:
        write_json(self.input, SNAPSHOT)
        with mock.patch.object(
            accept_producthunt, "_validate_launches", side_effect=RuntimeError("coding bug")
        ):
            with self.assertRaisesRegex(RuntimeError, "coding bug"):
                self.run_accept()


if __name__ == "__main__":
    unittest.main()
