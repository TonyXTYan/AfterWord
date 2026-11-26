"""One version's section of CHANGELOG.md.

Standard library only and free of package imports, so the release workflow
(.github/workflows/release.yml) runs it on a bare runner:

    python3 -m afterword.changelog 0.3.6 CHANGELOG.md > notes.md

It prints the body of the `## 0.3.6 (...)` section (without its heading)
and exits 1 with a message on stderr when there is no such section or it is
empty. The updater uses `section()` for the notes of an available update.
"""

from __future__ import annotations

import re
import sys

_HEADING = re.compile(r"^##\s+v?(\d+\.\d+\.\d+)\b")


def section(text: str, version: str) -> str | None:
    """The body of `version`'s `## X.Y.Z ...` section, trimmed; None when
    the changelog has no such section or it is empty. A leading "v" on
    `version` is ignored. The section ends at the next `## ` heading (any
    level-2 heading, a version or not)."""
    want = version[1:] if version.startswith("v") else version
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = _HEADING.match(line)
        if m and m.group(1) == want:
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    body = "\n".join(lines[start:end]).strip()
    return body or None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 2):
        print("usage: python3 -m afterword.changelog <version> [CHANGELOG.md]", file=sys.stderr)
        return 2
    path = args[1] if len(args) == 2 else "CHANGELOG.md"
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 1
    body = section(text, args[0])
    if body is None:
        print(f"{path} has no (or an empty) section for {args[0]}: add a `## {args[0].lstrip('v')} (date)` "
              "entry before releasing", file=sys.stderr)
        return 1
    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
