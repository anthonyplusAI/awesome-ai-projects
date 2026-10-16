#!/usr/bin/env python3
"""Accept a verified Product Hunt capture, or retain the last valid snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

if __package__:
    from .refresh import CatalogError, validate_catalog
    from .render import _validate_launches
else:
    from refresh import CatalogError, validate_catalog
    from render import _validate_launches


def _read_json(path: Path) -> tuple[object, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CatalogError(f"Could not read {path}: {error}") from error
    except UnicodeError as error:
        raise CatalogError(f"Invalid UTF-8 in {path}: {error}") from error
    try:
        return json.loads(content), content
    except json.JSONDecodeError as error:
        raise CatalogError(f"Invalid JSON in {path}: {error.msg} at line {error.lineno}") from error


def _replace_atomically(path: Path, content: str) -> None:
    """Write beside the destination, then swap the complete file into place."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


_HTTP_STATUS = re.compile(r"\bHTTP\s+([1-5][0-9]{2})\b", re.IGNORECASE)
_COVERAGE_MODES = {"product_page", "feed_direct_github"}


def _safe_warning(message: object) -> tuple[str, str]:
    """Classify source text without echoing URLs, credentials, or long messages."""
    if not isinstance(message, str):
        return "other", "invalid warning entry"
    lower = message.casefold()
    if "feeds lack matching direct link" in lower:
        stage = "feed"
    elif "direct link" in lower or "/r/p/" in lower:
        stage = "redirect"
    elif "product page" in lower or "/products/" in lower or "focused product" in lower:
        stage = "page"
    elif "github" in lower:
        stage = "github"
    elif "feed" in lower or "atom" in lower:
        stage = "feed"
    else:
        stage = "other"

    label = {
        "redirect": "Product Hunt /r/p redirect",
        "page": "Product Hunt product page",
        "github": "GitHub API",
        "feed": "Product Hunt feed",
        "other": "unclassified source",
    }[stage]
    status = _HTTP_STATUS.search(message)
    if status:
        return stage, f"{label} HTTP {status.group(1)}"
    if stage == "feed" and "stale" in lower:
        return stage, "Product Hunt feed is stale"
    if stage == "feed" and "malformed" in lower:
        return stage, "Product Hunt feed has malformed entries"
    if stage == "redirect" and "no location" in lower:
        return stage, "Product Hunt /r/p redirect lacks a Location"
    if stage == "redirect" and "no external" in lower:
        return stage, "Product Hunt /r/p redirect lacks an external destination"
    if stage == "feed" and "matching direct link" in lower:
        return stage, "Product Hunt feed Link evidence does not match"
    return stage, f"{label} issue"


def _safe_diagnostic(candidate: object, error: CatalogError) -> str:
    """Give one bounded, useful reason for a rejected well-formed capture."""
    if not isinstance(candidate, dict):
        return "coverage=unavailable; warnings=unknown; candidates=unknown; first_issue=missing or malformed capture"

    coverage = candidate.get("coverage")
    mode = coverage.get("mode") if isinstance(coverage, dict) else None
    mode = mode if isinstance(mode, str) and mode in _COVERAGE_MODES else "unknown"
    warnings = candidate.get("warnings")
    count = str(len(warnings)) if isinstance(warnings, list) else "invalid"
    candidates = candidate.get("candidates")
    candidate_count = str(len(candidates)) if isinstance(candidates, list) else "invalid"
    issues = [_safe_warning(item) for item in warnings] if isinstance(warnings, list) else []

    if issues:
        if mode == "feed_direct_github":
            relevant = next((item for item in issues if item[0] == "redirect"), None)
            relevant = relevant or next((item for item in issues if item[0] != "page"), None)
            relevant = relevant or issues[0]
        else:
            relevant = issues[0]
        first_issue = relevant[1]
    elif candidate.get("complete") is not True:
        first_issue = "capture marked incomplete"
    elif "duplicate or already curated" in str(error):
        first_issue = "candidate duplicates a listed repository"
    elif "evidence" in str(error) or "redirect" in str(error):
        first_issue = "candidate evidence failed validation"
    else:
        first_issue = "snapshot validation failed"

    detail = f"coverage={mode}; warnings={count}; candidates={candidate_count}; first_issue={first_issue}"
    if mode == "feed_direct_github":
        page_issue = next((summary for stage, summary in issues if stage == "page"), None)
        if page_issue:
            detail += f"; product_page={page_issue}"
    return detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="New capture to check")
    parser.add_argument("--output", required=True, type=Path, help="Published snapshot path")
    args = parser.parse_args(argv)

    try:
        catalog, _ = _read_json(args.output.parent / "projects.json")
        validate_catalog(catalog)
    except CatalogError as error:
        print(f"error: cannot validate the project catalog: {error}", file=sys.stderr)
        return 1

    candidate: object = None
    try:
        candidate, content = _read_json(args.input)
        _validate_launches(candidate, catalog)
    except CatalogError as error:
        detail = _safe_diagnostic(candidate, error)
        try:
            previous, _ = _read_json(args.output)
            _validate_launches(previous, catalog)
        except CatalogError:
            print(
                "error: Product Hunt capture was rejected and there is no valid previous "
                f"snapshot ({detail})",
                file=sys.stderr,
            )
            return 1
        print(
            f"::warning::Product Hunt capture was rejected ({detail}); "
            "keeping the last verified snapshot",
            file=sys.stderr,
        )
        return 0

    try:
        _replace_atomically(args.output, content)
    except OSError as error:
        print(f"error: could not update Product Hunt snapshot: {error}", file=sys.stderr)
        return 1
    print("Product Hunt snapshot updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
