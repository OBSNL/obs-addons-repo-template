#!/usr/bin/env python3
"""Generate an addon's README.md from its ``readme/`` fragments.

This is the OBS counterpart of OCA's ``oca-gen-addon-readme``. It differs in
two ways that matter to us:

* it emits **Markdown**, not reStructuredText. The fragments are already
  Markdown, so nothing is converted -- which means a ```` ```mermaid ````
  block survives verbatim and GitHub renders it as a diagram. Routed through
  the OCA tool the same block becomes ``.. code:: mermaid`` and degrades to a
  plain code listing.
* it carries no Odoo Community Association branding. The OCA template gates
  most of its OCA-specific content on ``org_name == 'OCA'``, but the readme
  banner image is emitted unconditionally, so every OBS module ends up with an
  OCA header it has no business showing.

Fragments are the OCA ones (``DESCRIPTION.md``, ``USAGE.md``, ...) plus
``CONTEXT.md`` for the business case. Only ``DESCRIPTION.md`` is required; an
addon without one is skipped, exactly as the OCA tool does.

The generated file opens with an HTML comment holding a digest of the manifest
and the fragments. ``--if-source-changed`` uses it to skip addons whose sources
have not moved, which keeps the pre-commit hook fast and idempotent.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import logging
import re
import sys
from pathlib import Path

FRAGMENTS_DIR = "readme"
README_FILENAME = "README.md"

#: fragment name -> (section title, heading level of that title)
#: ``None`` as a title means the fragment is spliced in without one.
FRAGMENTS = (
    ("DESCRIPTION", None, 2),
    ("CONTEXT", "Use Cases / Context", 2),
    ("INSTALL", "Installation", 2),
    ("CONFIGURE", "Configuration", 2),
    ("USAGE", "Usage", 2),
    ("DEVELOP", "Development", 2),
    ("ROADMAP", "Known issues / Roadmap", 2),
    ("HISTORY", "Changelog", 2),
    ("CONTRIBUTORS", "Contributors", 3),
    ("CREDITS", "Other credits", 3),
)

#: fragments rendered under the "Credits" section rather than as top-level ones
CREDITS_FRAGMENTS = ("CONTRIBUTORS", "CREDITS")

FRAGMENT_TITLES = {name: title for name, title, _level in FRAGMENTS}

LICENSE_BADGES = {
    "AGPL-3": (
        "https://img.shields.io/badge/license-AGPL--3-blue.png",
        "https://www.gnu.org/licenses/agpl-3.0-standalone.html",
        "License: AGPL-3",
    ),
    "LGPL-3": (
        "https://img.shields.io/badge/license-LGPL--3-blue.png",
        "https://www.gnu.org/licenses/lgpl-3.0-standalone.html",
        "License: LGPL-3",
    ),
    "GPL-3": (
        "https://img.shields.io/badge/license-GPL--3-blue.png",
        "https://www.gnu.org/licenses/gpl-3.0-standalone.html",
        "License: GPL-3",
    ),
    "OPL-1": (
        "https://img.shields.io/badge/license-OPL--1-blue.png",
        "https://www.odoo.com/documentation/master/legal/licenses.html",
        "License: OPL-1",
    ),
}

DEVELOPMENT_STATUS_BADGES = {
    "mature": (
        "https://img.shields.io/badge/maturity-Mature-brightgreen.png",
        "Mature",
    ),
    "production/stable": (
        "https://img.shields.io/badge/maturity-Production%2FStable-green.png",
        "Production/Stable",
    ),
    "beta": ("https://img.shields.io/badge/maturity-Beta-yellow.png", "Beta"),
    "alpha": ("https://img.shields.io/badge/maturity-Alpha-red.png", "Alpha"),
}

GENERATED_MARKER = "obs-gen-addon-readme"
DIGEST_RE = re.compile(r"source digest: (?P<digest>sha256:[0-9a-f]+)")
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})")
ATX_RE = re.compile(r"^(#{1,6})(\s)")
#: setext underline, i.e. a line of === (h1) or --- (h2) under its title
SETEXT_RE = re.compile(r"^(=+|-+)\s*$")
#: markdown image, e.g. ``![alt](path "title")``
IMAGE_RE = re.compile(r"(!\[[^\]]*\]\()(?P<path>[^)\s]+)")
LEADING_RELATIVE_RE = re.compile(r"^(?:\.\./|\./)+")

_logger = logging.getLogger("obs-gen-addon-readme")


def read_manifest(addon_dir: Path) -> dict | None:
    for name in ("__manifest__.py", "__openerp__.py"):
        path = addon_dir / name
        if path.is_file():
            return ast.literal_eval(path.read_text(encoding="utf8"))
    return None


def source_digest(addon_dir: Path) -> str:
    """Digest of the manifest plus every fragment, path-sensitive.

    Renaming or deleting a fragment must change the digest, so the relative
    path goes into the hash alongside the content.
    """
    digest = hashlib.sha256()
    paths = [
        p
        for p in (addon_dir / "__manifest__.py", addon_dir / "__openerp__.py")
        if p.is_file()
    ]
    fragments_dir = addon_dir / FRAGMENTS_DIR
    if fragments_dir.is_dir():
        paths.extend(sorted(p for p in fragments_dir.iterdir() if p.is_file()))
    for path in paths:
        digest.update(str(path.relative_to(addon_dir)).encode("utf8"))
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _is_setext_title(line: str) -> bool:
    """Whether ``line`` can carry a setext underline.

    A blank line, a list item or a quote makes the following ``---`` a
    thematic break rather than a heading underline.
    """
    stripped = line.strip()
    if not stripped or ATX_RE.match(line):
        return False
    return not re.match(r"[-*+>]\s|\d+[.)]\s", stripped)


def shift_headings(text: str, by: int) -> str:
    """Push the fragment's own ATX headings below its section title.

    Fenced code blocks are left alone -- a ``# comment`` on the first column of
    a shell snippet is not a heading, and neither is anything inside a
    ```` ```mermaid ```` block.
    """
    if by <= 0:
        return text
    out = []
    fence = None
    for line in text.splitlines():
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(2)
            if fence is None:
                fence = marker[0] * 3
            elif marker.startswith(fence):
                fence = None
            out.append(line)
            continue
        if fence is not None:
            out.append(line)
            continue
        heading = ATX_RE.match(line)
        if heading:
            level = min(len(heading.group(1)) + by, 6)
            out.append("#" * level + line[len(heading.group(1)) :])
            continue
        # A setext heading is the title line plus its underline. Rewrite the
        # pair as ATX so it nests under the section title -- several fragments
        # still carry the reStructuredText-style underlines they were migrated
        # from, and left as-is they render as siblings of the section itself.
        underline = SETEXT_RE.match(line)
        if underline and out and _is_setext_title(out[-1]):
            level = min((1 if underline.group(1)[0] == "=" else 2) + by, 6)
            out[-1] = "#" * level + " " + out[-1].strip()
            continue
        out.append(line)
    return "\n".join(out)


def absolutize_images(text: str, module_url: str) -> str:
    """Point relative image paths at raw.githubusercontent.

    Fragments are written to read well inside ``readme/``, so they reference
    images as ``../static/...``. Making them absolute keeps them working
    wherever the README is rendered, not just in the addon directory.
    """

    def replace(match: re.Match) -> str:
        path = match.group("path")
        if path.startswith(("http://", "https://", "#", "data:")):
            return match.group(0)
        return match.group(1) + module_url + LEADING_RELATIVE_RE.sub("", path)

    return IMAGE_RE.sub(replace, text)


def read_fragment(addon_dir: Path, name: str) -> str | None:
    path = addon_dir / FRAGMENTS_DIR / f"{name}.md"
    if (addon_dir / FRAGMENTS_DIR / f"{name}.rst").is_file():
        raise SystemExit(
            f"{addon_dir / FRAGMENTS_DIR / f'{name}.rst'}: .rst fragments are not "
            f"supported; this generator emits Markdown. Convert it to {name}.md."
        )
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf8").strip()
    return text or None


def slugify(title: str) -> str:
    """GitHub's heading anchor slug."""
    slug = title.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s", "-", slug)


def render(
    addon_name: str,
    manifest: dict,
    fragments: dict,
    org_name: str,
    repo_name: str,
    branch: str,
    digest: str,
) -> str:
    addon_url = f"https://github.com/{org_name}/{repo_name}/tree/{branch}/{addon_name}"
    repo_url = f"https://github.com/{org_name}/{repo_name}"

    badges = []
    status = manifest.get("development_status", "Beta").lower()
    if status in DEVELOPMENT_STATUS_BADGES:
        image, alt = DEVELOPMENT_STATUS_BADGES[status]
        badges.append(f"![{alt}]({image})")
    license_badge = LICENSE_BADGES.get(manifest.get("license"))
    if license_badge:
        image, target, alt = license_badge
        badges.append(f"[![{alt}]({image})]({target})")
    badge_org = org_name.replace("-", "--")
    badge_repo = repo_name.replace("-", "--")
    badges.append(
        f"[![{org_name}/{repo_name}]"
        f"(https://img.shields.io/badge/github-{badge_org}%2F{badge_repo}"
        f"-lightgray.png?logo=github)]({addon_url})"
    )

    lines = [
        f"<!-- This file is generated by {GENERATED_MARKER}; do not edit it.",
        f"     Edit the fragments under {FRAGMENTS_DIR}/ instead.",
        f"     source digest: {digest} -->",
        "",
        f"# {manifest.get('name', addon_name)}",
        "",
        " ".join(badges),
        "",
        fragments["DESCRIPTION"],
        "",
    ]

    if status == "alpha":
        lines += [
            "> [!WARNING]",
            "> This is an alpha version: the data model and the design can change",
            "> at any time without warning. For development or testing only.",
            "",
        ]

    sections = [
        (name, title, level)
        for name, title, level in FRAGMENTS
        if title and name in fragments and name not in CREDITS_FRAGMENTS
    ]
    has_credits = any(name in fragments for name in CREDITS_FRAGMENTS) or manifest.get(
        "author"
    )
    toc = [f"- [{title}](#{slugify(title)})" for _, title, _ in sections]
    toc.append("- [Bug Tracker](#bug-tracker)")
    if has_credits:
        toc.append("- [Credits](#credits)")
    lines += ["**Table of contents**", "", *toc, ""]

    for name, title, level in sections:
        lines += [
            "#" * level + f" {title}",
            "",
            shift_headings(fragments[name], level),
            "",
        ]

    lines += [
        "## Bug Tracker",
        "",
        f"Bugs are tracked on [GitHub Issues]({repo_url}/issues). In case of trouble,",
        "please check there if your issue has already been reported. If you spotted it",
        "first, help us to smash it by providing a detailed and welcomed feedback.",
        "",
        "Do not contact contributors directly about support or help with technical",
        "issues.",
        "",
    ]

    if has_credits:
        lines += ["## Credits", ""]
        authors = [
            a.strip() for a in manifest.get("author", "").split(",") if a.strip()
        ]
        if authors:
            lines += ["### Authors", "", *[f"- {author}" for author in authors], ""]
        for name in CREDITS_FRAGMENTS:
            if name not in fragments:
                continue
            title = FRAGMENT_TITLES[name]
            lines += [f"### {title}", "", shift_headings(fragments[name], 3), ""]
        maintainers = manifest.get("maintainers") or []
        if maintainers:
            plural = "s" if len(maintainers) > 1 else ""
            lines += [
                "### Maintainers",
                "",
                f"Current maintainer{plural}:",
                "",
                " ".join(
                    f"[![{m}](https://github.com/{m}.png?size=40px)]"
                    f"(https://github.com/{m})"
                    for m in maintainers
                ),
                "",
            ]

    lines += [
        f"This module is part of the [{org_name}/{repo_name}]({addon_url}) project on",
        "GitHub. You are welcome to contribute.",
        "",
    ]

    return "\n".join(lines)


def gen_addon_readme(
    addon_dir: Path,
    org_name: str,
    repo_name: str,
    branch: str,
    if_source_changed: bool,
) -> Path | None:
    manifest = read_manifest(addon_dir)
    if manifest is None:
        return None
    description = read_fragment(addon_dir, "DESCRIPTION")
    if description is None:
        # no fragments to build from -- leave any hand-written readme alone
        return None

    readme_path = addon_dir / README_FILENAME
    digest = source_digest(addon_dir)
    if if_source_changed and readme_path.is_file():
        match = DIGEST_RE.search(readme_path.read_text(encoding="utf8"))
        if match and match.group("digest") == digest:
            return None

    module_url = (
        f"https://raw.githubusercontent.com/{org_name}/{repo_name}"
        f"/{branch}/{addon_dir.name}/"
    )
    fragments = {}
    for name, _title, _level in FRAGMENTS:
        text = description if name == "DESCRIPTION" else read_fragment(addon_dir, name)
        if text:
            fragments[name] = absolutize_images(text, module_url)

    content = render(
        addon_dir.name, manifest, fragments, org_name, repo_name, branch, digest
    )
    if readme_path.is_file() and readme_path.read_text(encoding="utf8") == content:
        return None
    readme_path.write_text(content, encoding="utf8")
    return readme_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--addons-dir", default=".", help="directory holding the addons"
    )
    parser.add_argument("--org-name", default="OBSNL")
    parser.add_argument("--repo-name", required=True)
    parser.add_argument("--branch", required=True, help="Odoo series, e.g. 19.0")
    parser.add_argument(
        "--if-source-changed",
        action="store_true",
        help="skip addons whose manifest and fragments are unchanged",
    )
    parser.add_argument(
        "--remove-rst",
        action="store_true",
        help="delete a leftover README.rst next to a generated README.md",
    )
    parser.add_argument(
        "addons",
        nargs="*",
        help="paths inside the addons to regenerate; defaults to all of them",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    addons_dir = Path(args.addons_dir).resolve()
    if args.addons:
        # pre-commit passes changed files; map each back to its addon
        names = set()
        for path in args.addons:
            resolved = Path(path).resolve()
            try:
                names.add(resolved.relative_to(addons_dir).parts[0])
            except (ValueError, IndexError):
                continue
        candidates = [addons_dir / name for name in sorted(names)]
    else:
        candidates = sorted(p for p in addons_dir.iterdir() if p.is_dir())

    written = []
    for addon_dir in candidates:
        if not addon_dir.is_dir() or addon_dir.name.startswith("."):
            continue
        readme_path = gen_addon_readme(
            addon_dir,
            args.org_name,
            args.repo_name,
            args.branch,
            args.if_source_changed,
        )
        if readme_path is not None:
            written.append(readme_path)
        # Drop the OCA-generated README.rst once a README.md stands next to it,
        # independently of whether this run rewrote the .md: --if-source-changed
        # short-circuits an up-to-date addon before it gets here.
        if args.remove_rst and (addon_dir / README_FILENAME).is_file():
            rst = addon_dir / "README.rst"
            if rst.is_file():
                rst.unlink()
                written.append(rst)

    for path in written:
        _logger.info("%s: regenerated", path.relative_to(addons_dir))
    # pre-commit convention: a hook that rewrote files fails, so the run stops
    # and the developer commits the regenerated README.
    return 1 if written else 0


if __name__ == "__main__":
    raise SystemExit(main())
