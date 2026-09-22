#!/usr/bin/env python3
"""Bump the hassio-cardio2e add-on to a newly tagged cardio2e release.

Run by the ``bump-addon`` GitHub workflow after a ``vX.Y.Z`` tag is pushed.
Given the tag, a checkout of this repository and a checkout of the add-on
repository, it:

1. checks that the tag matches ``VERSION`` in ``cardio2e.py``;
2. extracts the tag's section from this repository's ``CHANGELOG.md``;
3. bumps the add-on patch version in ``config.yaml``, pins the tag in
   ``build.yaml`` (``CARDIO2E_VERSION``) and prepends a changelog entry.

Usage::

    bump_addon.py <tag> <cardio2e-dir> <addon-dir>

``<addon-dir>`` is the add-on folder inside the hassio-cardio2e checkout
(the one holding ``config.yaml``). The new add-on version is printed to
stdout as ``addon_version=X.Y.Z``.
"""

import re
import sys
from pathlib import Path


class AlreadyPinned(Exception):
    """The add-on already pins the requested cardio2e tag."""


def extract_section(changelog: str, version: str) -> str:
    """Return the body of the ``## <version>`` section, without its heading."""
    match = re.search(
        rf"^## {re.escape(version)}\b[^\n]*\n(.*?)(?=^## |\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"no section '## {version}' in CHANGELOG.md")
    return match.group(1).strip()


def render_addon_entry(addon_version: str, cardio_version: str, section: str) -> str:
    """Render the add-on changelog entry for a pulled cardio2e release.

    ``### Heading`` lines become ``- Heading:`` bullets and the section's own
    bullets are nested under them. Without headings the bullets nest directly
    under the ``Pull cardio2e`` line.
    """
    lines = [f"## {addon_version}", "", f"- Pull cardio2e {cardio_version}:"]
    indent = "  "
    for raw in section.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("### "):
            lines.append(f"  - {line[4:].strip()}:")
            indent = "    "
        else:
            lines.append(indent + line)
    return "\n".join(lines) + "\n"


def bump_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {version!r}")
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def read_version_constant(cardio2e_py: str) -> str:
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', cardio2e_py, re.MULTILINE)
    if not match:
        raise ValueError("VERSION constant not found in cardio2e.py")
    return match.group(1)


def update_config_version(config_yaml: str, new_version: str) -> str:
    text, count = re.subn(
        r'^version:\s*"[^"]*"',
        f'version: "{new_version}"',
        config_yaml,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("no 'version: \"...\"' line in config.yaml")
    return text


def update_build_arg(build_yaml: str, tag: str) -> str:
    pattern = re.compile(r"^(\s*CARDIO2E_VERSION:\s*)(\S+)", re.MULTILINE)
    match = pattern.search(build_yaml)
    if not match:
        raise ValueError("no CARDIO2E_VERSION argument in build.yaml")
    if match.group(2) == tag:
        raise AlreadyPinned(f"build.yaml already pins {tag}")
    return build_yaml[: match.start(2)] + tag + build_yaml[match.end(2) :]


def insert_changelog_entry(addon_changelog: str, entry: str) -> str:
    title = "# Changelog\n\n"
    if not addon_changelog.startswith(title):
        raise ValueError("add-on CHANGELOG.md does not start with '# Changelog'")
    return title + entry + "\n" + addon_changelog[len(title) :]


def run(tag: str, cardio_dir: Path, addon_dir: Path) -> str:
    """Apply the bump for ``tag``; returns the new add-on version.

    All inputs are validated and all new contents rendered before any file is
    written, so a failure leaves the add-on checkout untouched.
    """
    if not tag.startswith("v"):
        raise ValueError(f"tag must look like vX.Y.Z, got {tag!r}")
    constant = read_version_constant((cardio_dir / "cardio2e.py").read_text())
    if tag != f"v{constant}":
        raise ValueError(f"tag {tag} does not match VERSION = \"{constant}\" in cardio2e.py")
    section = extract_section((cardio_dir / "CHANGELOG.md").read_text(), tag)

    config_path = addon_dir / "config.yaml"
    build_path = addon_dir / "build.yaml"
    changelog_path = addon_dir / "CHANGELOG.md"

    new_build = update_build_arg(build_path.read_text(), tag)
    config_text = config_path.read_text()
    current = read_addon_version(config_text)
    new_version = bump_patch(current)
    new_config = update_config_version(config_text, new_version)
    entry = render_addon_entry(new_version, tag, section)
    new_changelog = insert_changelog_entry(changelog_path.read_text(), entry)

    build_path.write_text(new_build)
    config_path.write_text(new_config)
    changelog_path.write_text(new_changelog)
    return new_version


def read_addon_version(config_yaml: str) -> str:
    match = re.search(r'^version:\s*"([^"]+)"', config_yaml, re.MULTILINE)
    if not match:
        raise ValueError("no 'version: \"...\"' line in config.yaml")
    return match.group(1)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    tag, cardio_dir, addon_dir = argv[1], Path(argv[2]), Path(argv[3])
    try:
        new_version = run(tag, cardio_dir, addon_dir)
    except AlreadyPinned as exc:
        print(f"::warning::{exc}; nothing to do", file=sys.stderr)
        return 3
    except (ValueError, OSError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(f"addon_version={new_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
