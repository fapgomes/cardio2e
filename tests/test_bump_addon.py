"""Tests for .github/scripts/bump_addon.py (add-on bump on tag push)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".github" / "scripts"))

import bump_addon  # noqa: E402


CHANGELOG = """# Changelog

## v2.4.2 - 2026-09-21

### Fixes
- Re-send HVAC commands that get no `@A` ack.
- Re-query the state of a rejected command.

### Other
- Garbled frames are logged as WARNING.

## v2.4.1 - 2026-09-07

### Fixes
- Re-send light/relay commands that get no `@A` ack.
"""

ADDON_CHANGELOG = """# Changelog

## 1.0.17

- Add add-on store artwork: `icon.png` (128x128) and `logo.png` (250x100)

## 1.0.16

- Add `nscenarios` option
"""


class TestExtractSection:
    def test_returns_body_of_requested_version_only(self):
        body = bump_addon.extract_section(CHANGELOG, "v2.4.2")
        assert body.startswith("### Fixes")
        assert "Garbled frames" in body
        assert "light/relay" not in body
        assert "## v2.4.1" not in body

    def test_last_section_runs_to_end_of_file(self):
        body = bump_addon.extract_section(CHANGELOG, "v2.4.1")
        assert body == "### Fixes\n- Re-send light/relay commands that get no `@A` ack."

    def test_missing_version_raises(self):
        with pytest.raises(ValueError, match="v9.9.9"):
            bump_addon.extract_section(CHANGELOG, "v9.9.9")


class TestRenderAddonEntry:
    def test_headings_become_nested_bullets(self):
        section = bump_addon.extract_section(CHANGELOG, "v2.4.2")
        entry = bump_addon.render_addon_entry("1.0.18", "v2.4.2", section)
        assert entry == (
            "## 1.0.18\n"
            "\n"
            "- Pull cardio2e v2.4.2:\n"
            "  - Fixes:\n"
            "    - Re-send HVAC commands that get no `@A` ack.\n"
            "    - Re-query the state of a rejected command.\n"
            "  - Other:\n"
            "    - Garbled frames are logged as WARNING.\n"
        )

    def test_section_without_headings_nests_bullets_directly(self):
        entry = bump_addon.render_addon_entry("1.0.18", "v2.4.2", "- Only item")
        assert entry == "## 1.0.18\n\n- Pull cardio2e v2.4.2:\n  - Only item\n"

    def test_continuation_lines_keep_bullet_indent(self):
        section = "### Fixes\n- First line\n  continues here"
        entry = bump_addon.render_addon_entry("1.0.18", "v2.4.2", section)
        assert "    - First line\n      continues here\n" in entry


class TestVersions:
    def test_bump_patch(self):
        assert bump_addon.bump_patch("1.0.17") == "1.0.18"

    def test_bump_patch_rejects_non_semver(self):
        with pytest.raises(ValueError):
            bump_addon.bump_patch("1.0")

    def test_read_version_constant(self):
        text = 'import os\nVERSION = "2.4.2"\nX = 1\n'
        assert bump_addon.read_version_constant(text) == "2.4.2"

    def test_read_version_constant_missing_raises(self):
        with pytest.raises(ValueError):
            bump_addon.read_version_constant("X = 1\n")


class TestFileEdits:
    def test_update_config_version_replaces_quoted_version(self):
        text = 'name: Cardio2e\nversion: "1.0.17"\nslug: cardio2e\n'
        assert bump_addon.update_config_version(text, "1.0.18") == (
            'name: Cardio2e\nversion: "1.0.18"\nslug: cardio2e\n'
        )

    def test_update_config_version_missing_raises(self):
        with pytest.raises(ValueError):
            bump_addon.update_config_version("name: x\n", "1.0.18")

    def test_update_dockerfile_arg_replaces_tag(self):
        text = 'FROM img\n\nARG CARDIO2E_VERSION="v2.4.1"\n\nRUN true\n'
        assert bump_addon.update_dockerfile_arg(text, "v2.4.2") == (
            'FROM img\n\nARG CARDIO2E_VERSION="v2.4.2"\n\nRUN true\n'
        )

    def test_update_dockerfile_arg_same_tag_raises(self):
        text = 'ARG CARDIO2E_VERSION="v2.4.2"\n'
        with pytest.raises(bump_addon.AlreadyPinned):
            bump_addon.update_dockerfile_arg(text, "v2.4.2")

    def test_update_dockerfile_arg_missing_raises(self):
        with pytest.raises(ValueError):
            bump_addon.update_dockerfile_arg("FROM img\nARG BUILD_ARCH\n", "v2.4.2")

    def test_update_dockerfile_arg_requires_quoted_default(self):
        with pytest.raises(ValueError):
            bump_addon.update_dockerfile_arg("ARG CARDIO2E_VERSION\n", "v2.4.2")

    def test_insert_changelog_entry_goes_right_after_title(self):
        entry = "## 1.0.18\n\n- Pull cardio2e v2.4.2:\n  - Only item\n"
        out = bump_addon.insert_changelog_entry(ADDON_CHANGELOG, entry)
        assert out.startswith("# Changelog\n\n## 1.0.18\n\n- Pull cardio2e v2.4.2:\n  - Only item\n\n## 1.0.17\n")
        assert out.endswith("- Add `nscenarios` option\n")

    def test_insert_changelog_entry_without_title_raises(self):
        with pytest.raises(ValueError):
            bump_addon.insert_changelog_entry("## 1.0.17\n", "## 1.0.18\n")


class TestRun:
    @pytest.fixture
    def repos(self, tmp_path):
        cardio = tmp_path / "cardio2e"
        cardio.mkdir()
        (cardio / "cardio2e.py").write_text('VERSION = "2.4.2"\n')
        (cardio / "CHANGELOG.md").write_text(CHANGELOG)
        addon = tmp_path / "addon" / "cardio2e"
        addon.mkdir(parents=True)
        (addon / "config.yaml").write_text('name: Cardio2e\nversion: "1.0.17"\n')
        (addon / "Dockerfile").write_text('FROM img\nARG CARDIO2E_VERSION="v2.4.1"\n')
        (addon / "CHANGELOG.md").write_text(ADDON_CHANGELOG)
        return cardio, addon

    def test_run_updates_all_three_files_and_returns_new_version(self, repos):
        cardio, addon = repos
        new_version = bump_addon.run("v2.4.2", cardio, addon)
        assert new_version == "1.0.18"
        assert (addon / "config.yaml").read_text() == 'name: Cardio2e\nversion: "1.0.18"\n'
        assert (addon / "Dockerfile").read_text() == 'FROM img\nARG CARDIO2E_VERSION="v2.4.2"\n'
        changelog = (addon / "CHANGELOG.md").read_text()
        assert "## 1.0.18\n\n- Pull cardio2e v2.4.2:\n  - Fixes:\n" in changelog
        assert changelog.index("## 1.0.18") < changelog.index("## 1.0.17")

    def test_run_rejects_tag_that_does_not_match_version_constant(self, repos):
        cardio, addon = repos
        with pytest.raises(ValueError, match="VERSION"):
            bump_addon.run("v2.4.3", cardio, addon)
        assert (addon / "config.yaml").read_text() == 'name: Cardio2e\nversion: "1.0.17"\n'

    def test_run_rejects_tag_without_v_prefix(self, repos):
        cardio, addon = repos
        with pytest.raises(ValueError, match="v"):
            bump_addon.run("2.4.2", cardio, addon)

    def test_run_writes_nothing_when_already_pinned(self, repos):
        cardio, addon = repos
        (addon / "Dockerfile").write_text('FROM img\nARG CARDIO2E_VERSION="v2.4.2"\n')
        with pytest.raises(bump_addon.AlreadyPinned):
            bump_addon.run("v2.4.2", cardio, addon)
        assert (addon / "config.yaml").read_text() == 'name: Cardio2e\nversion: "1.0.17"\n'
        assert (addon / "CHANGELOG.md").read_text() == ADDON_CHANGELOG
