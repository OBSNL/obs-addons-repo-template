from pathlib import Path

import packaging.version
import yaml
from copier.main import run_copy

# Change this one in case it becomes a mandatory check
SOME_PYLINT_OPTIONAL_CHECK = "too-complex"

# These are arbitrarily picked, but in case anything changes (very unlikely),
# modify these variables to avoid failing tests
REPO_SLUG = "server-tools"
REPO_ID = 149


def read_and_parse_precommit_rev(tmp_path: Path, repo: str):
    # Attempt to find the rev of a pre-commit repo.
    # If it found, return the actual version, if it's not return 0.0.0.
    config = yaml.safe_load((tmp_path / ".pre-commit-config.yaml").read_text())
    entry = next(
        filter(lambda r: r.get("repo", "") == repo, config.get("repos", [])), {}
    )
    return packaging.version.parse(entry.get("rev", "0.0.0"))


def test_bootstrap(tmp_path: Path, odoo_version: float, cloned_template: Path):
    """Test that a project is properly bootstrapped."""
    # Prepare the data for Copier
    # Note: Only fields that don't have `when: false` in copier.yml will be saved in answers file
    data = {
        "odoo_version": odoo_version,
        "repo_slug": REPO_SLUG,
        "repo_name": "Test repo",
        "ci": "GitHub",
    }
    # Also pass additional parameters that affect template generation but won't be saved in answers
    all_data = data.copy()
    all_data.update({
        "use_ruff": False,  # Use flake8 instead of ruff for consistency in tests
        "github_enable_stale_action": True,  # Enable stale workflow for testing
    })
    run_copy(str(cloned_template), tmp_path, data=all_data, defaults=True, unsafe=True)
    # When loading YAML files, we are also testing their syntax is correct, which
    # can be a little bit tricky due to the way both Jinja and YAML handle whitespace
    answers = yaml.safe_load((tmp_path / ".copier-answers.yml").read_text())
    for key, value in data.items():  # Only check fields that should be in answers
        assert answers[key] == value
    # Assert linter config files look like they were looking before
    pylintrc_mandatory = (tmp_path / ".pylintrc-mandatory").read_text()
    assert "disable=all\n" in pylintrc_mandatory
    assert "enable=" in pylintrc_mandatory
    pylint_odoo_version = read_and_parse_precommit_rev(
        tmp_path, "https://github.com/OCA/pylint-odoo"
    )
    valid_odoo_versions = "valid_odoo_versions"
    if pylint_odoo_version >= packaging.version.parse("v8.0.5"):
        # versions of pylint-odoo >= 8.0.5 use dashes for keys, rather than
        # underscores
        valid_odoo_versions = "valid-odoo-versions"
    assert f"{valid_odoo_versions}={odoo_version}" in pylintrc_mandatory
    assert SOME_PYLINT_OPTIONAL_CHECK not in pylintrc_mandatory
    pylintrc_optional = (tmp_path / ".pylintrc").read_text()
    assert "disable=all\n" in pylintrc_optional
    assert "# This .pylintrc contains" in pylintrc_optional
    assert f"{valid_odoo_versions}={odoo_version}" in pylintrc_optional
    assert SOME_PYLINT_OPTIONAL_CHECK in pylintrc_optional
    # Check linter config based on odoo_version and use_ruff flag (which we passed but not saved in answers)
    # Since we set use_ruff: False, we should always get .flake8 when applicable
    # .flake8 is created for odoo_version < 13 (first condition) or odoo_version >= 13 and use_ruff is False (second condition)
    if odoo_version < 13 or (odoo_version >= 13 and all_data["use_ruff"] == False):
        flake8 = (tmp_path / ".flake8").read_text()
        assert "[flake8]" in flake8
    # The isort condition also depends on use_ruff - isort is created when odoo_version > 12 and not use_ruff
    if odoo_version > 12 and all_data["use_ruff"] == False:
        isort = (tmp_path / ".isort.cfg").read_text()
        assert "[settings]" in isort
    assert not (tmp_path / ".gitmodules").is_file()
    # Assert other files
    license_ = (tmp_path / "LICENSE").read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_
    # Workflows for the subprojects are copied
    assert (tmp_path / ".github" / "workflows" / "pre-commit.yml").is_file()
    assert (tmp_path / ".github" / "workflows" / "stale.yml").is_file()
    # Workflows for the template itself are not copied
    assert not (tmp_path / ".github" / "workflows" / "lint.yml").is_file()
    # Assert badges in readme; this is testing the repo_id macro
    readme = (tmp_path / "README.md").read_text()
    # With GitHub CI, we should have GitHub Actions badges
    assert (
        f"[![Pre-commit Status](https://github.com/OBSNL/{REPO_SLUG}/actions/workflows/pre-commit.yml/badge.svg?branch={odoo_version})](https://github.com/OBSNL/{REPO_SLUG}/actions/workflows/pre-commit.yml?query=branch%3A{odoo_version})"  # noqa: B950
        in readme
    )
    assert (
        f"[![Build Status](https://github.com/OBSNL/{REPO_SLUG}/actions/workflows/test.yml/badge.svg?branch={odoo_version})](https://github.com/OBSNL/{REPO_SLUG}/actions/workflows/test.yml?query=branch%3A{odoo_version})"  # noqa: B950
        in readme
    )
    assert (
        f"[![codecov](https://codecov.io/gh/OBSNL/{REPO_SLUG}/branch/{odoo_version}/graph/badge.svg)](https://codecov.io/gh/OBSNL/{REPO_SLUG})"  # noqa: B950
        in readme
    )
    odoo_version_tr = str(odoo_version).replace(".", "-")
    # Translation status badge is not always included in non-OCA context
    # so we skip checking for it
    assert "# Test repo" in readme
    assert "Customizations for server-tools" in readme
    # Assert no stuff specific for this repo is found
    garbage = (
        "setup.cfg",
        "tests",
        "vendor",
        "version-specific",
        "copier.yml",
        ".gitmodules",
        "poetry.lock",
        "pyproject.toml",
    )
    for file_ in garbage:
        assert not (tmp_path / file_).exists()
