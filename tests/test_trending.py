"""Behavior tests for the raw GitHub monthly Trending snapshot."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import trending


# Reduced from the structure of GitHub's live monthly Trending page on
# 2026-10-16. The login link before the heading and nested SVGs are deliberate:
# neither should be mistaken for the repository or star count.
TRENDING_HTML = """<!doctype html>
<html><body><main>
  <article class="Box-row">
    <a href="/login?return_to=%2Fcloudflare%2Fsecurity-audit-skill">Star</a>
    <h2 class="h3 lh-condensed">
      <a href="/cloudflare/security-audit-skill" class="Link">
        <svg aria-hidden="true"><path d="M2 2" /></svg>
        <span class="text-normal">cloudflare /</span> security-audit-skill
      </a>
    </h2>
    <p class="col-9 color-fg-muted my-1">A coding-agent skill for multi-phase security audits</p>
    <span itemprop="programmingLanguage">JavaScript</span>
    <a href="/cloudflare/security-audit-skill/stargazers">
      <svg aria-label="star"><path d="M8 .25" /></svg> 30,217
    </a>
    <span class="d-inline-block float-sm-right">
      <svg aria-hidden="true"><path d="M8 .25" /></svg> 25,242 stars this month
    </span>
  </article>
  <article class="Box-row">
    <a href="/login?return_to=%2Fanthropics%2Ffinancial-services">Star</a>
    <h2 class="h3 lh-condensed"><a href="/anthropics/financial-services">anthropics / financial-services</a></h2>
    <span itemprop="programmingLanguage">Python</span>
    <a href="/anthropics/financial-services/stargazers">40,086</a>
    <span class="d-inline-block float-sm-right">5,252 stars this month</span>
  </article>
</main></body></html>"""


class TrendingParserTests(unittest.TestCase):
    def test_extracts_rank_and_both_star_counts_without_filtering(self) -> None:
        rows = trending.parse_trending_html(TRENDING_HTML)

        self.assertEqual(
            rows,
            [
                {
                    "repo": "cloudflare/security-audit-skill",
                    "rank": 1,
                    "stars_this_month": 25242,
                    "stars_total": 30217,
                    "description": "A coding-agent skill for multi-phase security audits",
                    "language": "JavaScript",
                },
                {
                    "repo": "anthropics/financial-services",
                    "rank": 2,
                    "stars_this_month": 5252,
                    "stars_total": 40086,
                    "description": None,
                    "language": "Python",
                },
            ],
        )

    def test_rejects_non_trending_or_empty_response(self) -> None:
        with self.assertRaisesRegex(trending.TrendingError, "no repository rows"):
            trending.parse_trending_html("<html><h1>Sign in</h1></html>")

    def test_rejects_truncated_page_after_a_complete_row(self) -> None:
        truncated = TRENDING_HTML.split("</article>", 1)[0] + "</article>"

        with self.assertRaisesRegex(trending.TrendingError, "incomplete"):
            trending.parse_trending_html(truncated)

    def test_rejects_missing_monthly_metric_instead_of_using_daily_stars(self) -> None:
        daily_page = TRENDING_HTML.replace("25,242 stars this month", "25,242 stars today")

        with self.assertRaisesRegex(trending.TrendingError, "monthly star count"):
            trending.parse_trending_html(daily_page)

    def test_rejects_missing_total_stars(self) -> None:
        no_total = TRENDING_HTML.replace(
            "/cloudflare/security-audit-skill/stargazers",
            "/cloudflare/security-audit-skill/forks",
        )

        with self.assertRaisesRegex(trending.TrendingError, "total star count"):
            trending.parse_trending_html(no_total)

    def test_rejects_bad_heading_link(self) -> None:
        bad_heading = TRENDING_HTML.replace(
            'href="/cloudflare/security-audit-skill" class="Link"',
            'href="/search" class="Link"',
        )

        with self.assertRaisesRegex(trending.TrendingError, "repository link"):
            trending.parse_trending_html(bad_heading)

    def test_rejects_duplicate_repositories(self) -> None:
        duplicate = TRENDING_HTML.replace(
            "anthropics/financial-services", "cloudflare/security-audit-skill"
        )

        with self.assertRaisesRegex(trending.TrendingError, "duplicate"):
            trending.parse_trending_html(duplicate)


class TrendingSnapshotTests(unittest.TestCase):
    def test_snapshot_records_exact_source_and_utc_observation_time(self) -> None:
        observed_at = datetime(2026, 10, 16, 17, 4, 31, tzinfo=timezone.utc)

        snapshot = trending.build_snapshot(TRENDING_HTML, observed_at)

        self.assertEqual(snapshot["source_url"], "https://github.com/trending?since=monthly")
        self.assertEqual(snapshot["period"], "monthly")
        self.assertEqual(snapshot["fetched_at"], "2026-10-16T17:04:31Z")
        self.assertEqual(len(snapshot["repositories"]), 2)

    def test_cli_prints_json_by_default(self) -> None:
        output = io.StringIO()
        with mock.patch.object(trending, "fetch_trending_html", return_value=TRENDING_HTML):
            with redirect_stdout(output):
                result = trending.main([])

        self.assertEqual(result, 0)
        snapshot = json.loads(output.getvalue())
        self.assertEqual(snapshot["repositories"][0]["repo"], "cloudflare/security-audit-skill")
        observed_at = datetime.fromisoformat(snapshot["fetched_at"].replace("Z", "+00:00"))
        self.assertEqual(observed_at.utcoffset(), timezone.utc.utcoffset(observed_at))

    def test_cli_writes_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output_path = Path(folder) / "snapshot.json"
            with mock.patch.object(trending, "fetch_trending_html", return_value=TRENDING_HTML):
                result = trending.main(["--output", str(output_path)])

            self.assertEqual(result, 0)
            snapshot = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(len(snapshot["repositories"]), 2)

    def test_bad_page_does_not_overwrite_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output_path = Path(folder) / "snapshot.json"
            output_path.write_text("previous-good-snapshot\n", encoding="utf-8")
            errors = io.StringIO()
            with mock.patch.object(
                trending, "fetch_trending_html", return_value="<html>GitHub unavailable</html>"
            ):
                with redirect_stderr(errors):
                    result = trending.main(["--output", str(output_path)])

            self.assertEqual(result, 1)
            self.assertIn("no repository rows", errors.getvalue())
            self.assertEqual(output_path.read_text(encoding="utf-8"), "previous-good-snapshot\n")


if __name__ == "__main__":
    unittest.main()
