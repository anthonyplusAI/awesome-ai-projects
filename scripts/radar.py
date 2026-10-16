#!/usr/bin/env python3
"""Turn a monthly GitHub Trending snapshot into validated AI discovery signals.

This is an automated radar, not an editorial endorsement. It never modifies
the curated project list. Run after ``scripts/trending.py`` has captured the
public monthly Trending page:

    python3 scripts/radar.py --input data/trending-monthly.json --output data/radar.json

Without ``--output``, the JSON is printed to stdout. An existing output file
is replaced atomically only after the full source and API pass succeeds.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from . import refresh
else:
    import refresh


CatalogError = refresh.CatalogError
TRENDING_URL = "https://github.com/trending?since=monthly"
MAX_CANDIDATES = 8
DESCRIPTION_LIMIT = 160
AI_TOPICS = frozenset(
    {
        "ai",
        "ai-agent",
        "ai-agents",
        "ai-assistant",
        "ai-coding",
        "ai-tools",
        "artificial-intelligence",
        "chatbot",
        "computer-vision",
        "deep-learning",
        "diffusion-models",
        "generative-ai",
        "image-generation",
        "large-language-model",
        "large-language-models",
        "llm",
        "llms",
        "machine-learning",
        "model-inference",
        "multimodal",
        "prompt-engineering",
        "rag",
        "speech-recognition",
        "speech-to-text",
        "stable-diffusion",
        "text-to-speech",
        "transformers",
    }
)
AI_DESCRIPTION_RE = re.compile(
    r"\b(?:AI|LLMs?|GenAI|GPT|RAG|ChatGPT|OpenAI|Claude|agentic|chatbot|"
    r"artificial intelligence|(?:large )?language models?|machine learning|"
    r"deep learning|neural networks?|computer vision|generative models?|"
    r"image generation|speech recognition|speech.to.text|text.to.speech|"
    r"retrieval.augmented generation|diffusion models?)\b",
    re.IGNORECASE,
)


def _validate_source(source: object) -> list[dict]:
    """Reject a broken or unrelated scrape before any API request."""
    if not isinstance(source, dict):
        raise CatalogError("Trending snapshot must be a JSON object")
    if source.get("source_url") != TRENDING_URL or source.get("period") != "monthly":
        raise CatalogError("Trending snapshot must come from the monthly GitHub Trending page")
    try:
        refresh._parse_utc_timestamp(source.get("fetched_at"))
    except CatalogError as error:
        raise CatalogError("Trending snapshot fetched_at must be a UTC ISO timestamp") from error
    rows = source.get("repositories")
    if not isinstance(rows, list) or not rows:
        raise CatalogError("Trending snapshot needs a nonempty repositories list")
    seen_slugs: set[str] = set()
    seen_ranks: set[int] = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise CatalogError(f"Trending row #{index} must be an object")
        slug = refresh._validate_repo_slug(row.get("repo"))
        rank = row.get("rank")
        if type(rank) is not int or rank < 1 or rank in seen_ranks:
            raise CatalogError(f"{slug}: rank must be a unique positive integer")
        for field in ("stars_this_month", "stars_total"):
            number = row.get(field)
            minimum = 1 if field == "stars_this_month" else 0
            if type(number) is not int or number < minimum:
                raise CatalogError(f"{slug}: {field} must be an integer of at least {minimum}")
        if row.get("description") is not None and not isinstance(row.get("description"), str):
            raise CatalogError(f"{slug}: description must be text or null")
        if row.get("language") is not None and not isinstance(row.get("language"), str):
            raise CatalogError(f"{slug}: language must be text or null")
        if slug.casefold() in seen_slugs:
            raise CatalogError(f"Trending snapshot repeats {slug}")
        seen_slugs.add(slug.casefold())
        seen_ranks.add(rank)
    if seen_ranks != set(range(1, len(rows) + 1)):
        raise CatalogError("Trending snapshot ranks must be consecutive from 1")
    return sorted(rows, key=lambda row: row["rank"])


def _is_ai_relevant(response: dict) -> bool:
    topics = response.get("topics")
    if isinstance(topics, list) and any(
        isinstance(topic, str) and topic.casefold() in AI_TOPICS for topic in topics
    ):
        return True
    description = response.get("description")
    return isinstance(description, str) and bool(AI_DESCRIPTION_RE.search(description))


def _normalize_description(response: dict, trending_row: dict) -> str:
    """Keep plain metadata short; the README renderer owns Markdown escaping."""
    description = response.get("description") or trending_row.get("description")
    if not isinstance(description, str) or not description.strip():
        description = "No description provided"
    collapsed = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", description).split())
    if not collapsed:
        return "No description provided"
    if len(collapsed) > DESCRIPTION_LIMIT:
        collapsed = collapsed[: DESCRIPTION_LIMIT - 1].rstrip() + "…"
    return collapsed


def _fetch_metadata(url: str, fetch_json_fn) -> dict | None:
    """Retry a transient error; skip repos deleted since capture."""
    try:
        return fetch_json_fn(url)
    except CatalogError as error:
        detail = str(error)
        if detail.startswith("GitHub API HTTP 404 for "):
            return None
        if detail.startswith("Could not reach GitHub API for ") or re.search(
            r"GitHub API HTTP 5\d\d\b", detail
        ):
            return fetch_json_fn(url)
        raise


def build_radar(
    source: object,
    catalog: dict,
    stats: dict,
    fetch_json_fn,
    *,
    now: datetime | None = None,
    limit: int = MAX_CANDIDATES,
) -> dict:
    """Validate Trending against live GitHub metadata and select review leads."""
    rows = _validate_source(source)
    refresh.validate_stats(stats, catalog)
    if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
        raise CatalogError(f"radar limit must be between 1 and {MAX_CANDIDATES}")
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise CatalogError("radar validation time must have a timezone")
    observed_at = observed_at.astimezone(timezone.utc)
    curated = {project["repo"].casefold(): project["repo"] for project in catalog["projects"]}
    qualified: list[dict] = []

    for row in rows:
        slug = row["repo"]
        metadata_url = f"{refresh.API_ROOT}/repos/{slug}"
        response = _fetch_metadata(metadata_url, fetch_json_fn)
        if response is None:
            continue
        if not isinstance(response, dict):
            raise CatalogError(f"{slug}: GitHub API returned a non-object response")
        # Reject renames or aliases instead of silently attributing another
        # repository's stars and license to the Trending link.
        if response.get("full_name") != slug or response.get("private") is not False:
            continue
        is_curated = slug.casefold() in curated
        if not is_curated and not _is_ai_relevant(response):
            continue
        existing_license = None
        if is_curated:
            existing_license = stats["repositories"][curated[slug.casefold()]]["license"]
        try:
            checked = refresh.fetch_repository_stats(
                slug, lambda _url: response, existing_license=existing_license
            )
        except CatalogError:
            # A Trending position is not proof of an eligible repository.
            continue

        stars_total = checked["stars"]
        if is_curated:
            stars_total = stats["repositories"][curated[slug.casefold()]]["stars"]
        github_url = f"https://github.com/{slug}"
        qualified.append(
            {
                "repo": slug,
                "rank": row["rank"],
                "stars_this_month": row["stars_this_month"],
                "stars_total": stars_total,
                "license": checked["license"],
                "description": _normalize_description(response, row),
                "curated": is_curated,
                "github_url": github_url,
                "source_url": TRENDING_URL,
                "evidence_urls": [TRENDING_URL, github_url, metadata_url],
            }
        )

    if not qualified:
        raise CatalogError("Trending snapshot has no qualifying open-source AI repositories")
    selected = qualified[:limit]
    if not any(row["curated"] for row in selected):
        first_curated = next((row for row in qualified[limit:] if row["curated"]), None)
        if first_curated is not None:
            selected = selected[: limit - 1] + [first_curated]
    selected.sort(key=lambda row: row["rank"])

    return {
        "fetched_at": observed_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source_url": TRENDING_URL,
        "period": "monthly",
        "source_updated_at": source["fetched_at"],
        "signal": "Automated GitHub Trending candidates; not editorial endorsements.",
        "selection_policy": (
            "Highest-ranked qualifying AI repositories, with one slot reserved "
            "for a qualifying curated project when available."
        ),
        "repositories": selected,
    }


def _write_atomic(path: Path, content: str) -> None:
    """Keep the previous snapshot intact through validation and write errors."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise CatalogError(f"Could not write radar snapshot {path}: {error.strerror}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(
    argv: list[str] | None = None, *, fetch_json_fn=None, now: datetime | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, default=Path("data/trending-monthly.json"),
        help="monthly Trending snapshot (default: data/trending-monthly.json)",
    )
    parser.add_argument(
        "--output", type=Path,
        help="write the validated radar JSON atomically; default is stdout",
    )
    parser.add_argument("--limit", type=int, default=MAX_CANDIDATES, help="maximum rows (1–8)")
    parser.add_argument("--root", type=Path, default=refresh.ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    input_path = args.input if args.input.is_absolute() else root / args.input
    output_path = args.output if args.output is None or args.output.is_absolute() else root / args.output
    try:
        source = refresh._read_json(input_path)
        catalog = refresh._read_json(root / "data" / "projects.json")
        stats = refresh._read_json(root / "data" / "stats.json")
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        fetcher = fetch_json_fn or (lambda url: refresh.fetch_json(url, token=token))
        snapshot = build_radar(source, catalog, stats, fetcher, now=now, limit=args.limit)
        serialized = json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n"
        if output_path is None:
            print(serialized, end="")
        else:
            _write_atomic(output_path, serialized)
    except (CatalogError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
