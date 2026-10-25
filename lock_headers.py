#!/usr/bin/env python3
"""Add the explanatory header to the generated files in requirements/ (run by `make lock`)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = (
    "# Exact versions used to build the container image and run CI (Python 3.12, Linux).\n"
    "# Regenerate after changing dependencies in pyproject.toml with:\n"
    "#   make lock\n"
    "# which runs:\n"
    "#   uv pip compile pyproject.toml{extra} --python-version 3.12 \\\n"
    "#     --python-platform x86_64-manylinux_2_28 -o requirements/{name} --no-header\n"
)


def main() -> None:
    for name, extra in (("constraints.txt", ""), ("constraints-dev.txt", " --extra dev")):
        path = ROOT / "requirements" / name
        # uv writes no column zero comments with --no-header, so every such line is ours.
        body = "".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith("#")
        )
        path.write_text(HEADER.format(extra=extra, name=name) + body, encoding="utf-8")


if __name__ == "__main__":
    main()
