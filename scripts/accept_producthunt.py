#!/usr/bin/env python3
"""Accept a verified Product Hunt capture, or retain the last valid snapshot."""

from __future__ import annotations

import argparse
import json
import os
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

    try:
        candidate, content = _read_json(args.input)
        _validate_launches(candidate, catalog)
    except CatalogError as error:
        try:
            previous, _ = _read_json(args.output)
            _validate_launches(previous, catalog)
        except CatalogError as previous_error:
            print(
                "error: Product Hunt capture was rejected and there is no valid previous "
                f"snapshot: {error}; previous snapshot: {previous_error}",
                file=sys.stderr,
            )
            return 1
        print(
            f"::warning::Product Hunt capture was rejected ({error}); "
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
