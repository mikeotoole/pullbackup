"""Guard the user-facing Pullbackup rename (tranche 1).

Scope: user-visible product naming only — the browser title, the UI chrome, and
the README. Code identifiers (``backend/pullback/``), the ``PULLBACK_`` env
prefix, the database filename, and deployment identity are deliberately NOT
covered here; those change in later tranches, and asserting on them now would
fail against a deployment that is still running the old names.

Naming convention:
  * lowercase ``pullbackup`` in UI chrome (matches the existing lowercase-chrome
    convention; run/task STATE values stay uppercase)
  * ``Pullbackup`` capitalised in prose and as the README H1

Note that the old name is a strict prefix of the new one, so a bare substring
search for "pullback" matches "pullbackup" too. Every check below is written to
distinguish the two rather than accepting the prefix as a match.
"""

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = REPOSITORY_ROOT / "frontend" / "src"
INDEX_HTML = REPOSITORY_ROOT / "frontend" / "index.html"
FAVICON = REPOSITORY_ROOT / "frontend" / "public" / "favicon.svg"
README = REPOSITORY_ROOT / "README.md"

NEW_NAME = "pullbackup"

# "pullback" NOT followed by "up" — the negative lookahead is what stops the new
# name matching as its own predecessor.
OLD_NAME = re.compile(r"pullback(?!up)", re.IGNORECASE)

# The retired Matera branding must not survive anywhere.
MATERA = re.compile(r"matera", re.IGNORECASE)


def _user_facing_frontend_files():
    """Frontend sources that render text to a user."""
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if path.suffix in {".tsx", ".ts"} and not path.name.endswith(".test.ts"):
            yield path


def test_old_name_pattern_does_not_match_the_new_name():
    """Guard the guard: the whole suite is meaningless if the regex is wrong."""
    assert OLD_NAME.search("pullback")
    assert OLD_NAME.search("PULLBACK_DATA_DIR")
    assert not OLD_NAME.search("pullbackup")
    assert not OLD_NAME.search("Pullbackup")


def test_browser_title_uses_the_new_name():
    assert f"<title>{NEW_NAME}</title>" in INDEX_HTML.read_text()


def test_favicon_accessible_label_uses_the_new_name():
    """The favicon carries an aria-label that screen readers announce."""
    assert f'aria-label="{NEW_NAME}"' in FAVICON.read_text()


def test_header_renders_the_new_wordmark():
    app = (FRONTEND_SRC / "App.tsx").read_text()
    assert NEW_NAME in app, "header wordmark should read 'pullbackup'"


def test_no_user_facing_frontend_string_uses_the_bare_old_name():
    """Chrome and labels must read 'pullbackup', not 'pullback'.

    One documented exception: TaskForm describes the *live* Uptime Kuma monitor
    prefix, which is still literally ``pullback:`` until the backend rename
    ships. Showing the new name there would tell the user something false about
    monitors that exist in their Kuma instance right now.
    """
    exceptions = {"pages/TaskForm.tsx"}
    offenders = []
    for path in _user_facing_frontend_files():
        if str(path.relative_to(FRONTEND_SRC)) in exceptions:
            continue
        if OLD_NAME.search(path.read_text()):
            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert not offenders, f"bare old name still rendered by: {offenders}"


def test_kuma_help_text_describes_the_real_monitor_prefix():
    """Pin the exception above against the backend's ACTUAL behaviour.

    Read the literal the backend really builds and require the help text to
    agree, so the two cannot drift and the UI cannot misdescribe monitors that
    already exist in a user's Uptime Kuma instance.
    """
    backend_call = (
        REPOSITORY_ROOT / "backend" / "pullbackup" / "api" / "tasks.py"
    ).read_text()
    match = re.search(r'create_push_monitor\(\s*f"([^:"]+):', backend_call)
    assert match, "could not locate the Kuma monitor name construction"
    actual_prefix = match.group(1)

    task_form = (FRONTEND_SRC / "pages" / "TaskForm.tsx").read_text()
    assert f"{NEW_NAME} creates/updates a Kuma push monitor" in task_form
    assert f"<code>{actual_prefix}: {{form.name" in task_form, (
        f"help text must show the prefix the backend actually uses ({actual_prefix!r})"
    )


def test_no_retired_matera_branding_remains():
    """The Matera rename was reverted; none of it may survive."""
    offenders = []
    for path in list(_user_facing_frontend_files()) + [INDEX_HTML, FAVICON, README]:
        if MATERA.search(path.read_text()):
            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert not offenders, f"retired Matera branding still present in: {offenders}"


def test_readme_leads_with_the_product_name():
    first_line = README.read_text().splitlines()[0]
    assert first_line.strip() == "# Pullbackup"


def _readme_prose():
    """README text as flowing prose: blockquote markers stripped and whitespace
    collapsed, so assertions do not depend on where a line happens to wrap."""
    lines = [
        re.sub(r"^\s*>\s?", "", line)
        for line in README.read_text().splitlines()
    ]
    return " ".join(" ".join(lines).split())


def test_readme_has_no_bare_old_product_branding():
    """The README legitimately still contains the old name inside identifiers:
    the host data directory (``${DOCKER_VOLUMES}/pullback/data``) and the
    database filename (``pullback.db``) inside it. Both are real, current
    identifiers that a deliberate migration renames later. What must be gone is
    *product* branding — prose calling the tool itself "pullback".
    """
    prose = _readme_prose()
    # Strip every legitimate identifier occurrence. `PULLBACK_` appears both as
    # concrete variable names and as the bare prefix in prose, so remove the
    # backticked prefix form first.
    residue = prose.replace("`PULLBACK_`", "")
    residue = re.sub(r"PULLBACK_[A-Z_]+", "", residue)
    for identifier in (
        "${DOCKER_VOLUMES}/pullback/data:/data",
        "${DOCKER_VOLUMES}/pullback/data",
        "pullback.db",
    ):
        residue = residue.replace(identifier, "")

    leftover = OLD_NAME.findall(residue)
    assert not leftover, (
        f"bare old product branding in README ({len(leftover)}x): {residue[:400]!r}"
    )


def test_readme_explains_the_transitional_naming_split():
    """The product is Pullbackup while identifiers still read PULLBACK_; the
    README must say so, or the mixed naming reads as a typo."""
    assert "The product is Pullbackup" in _readme_prose()


def test_readme_documents_the_real_persistent_data_path():
    """Regression guard for review 310.

    The host data directory holds the SQLite database, run logs, and SSH keys.
    It is NOT renamed in this tranche, so the documented bind mount must still
    read ``pullback/data``. Documenting a renamed path while the deployment
    still mounts ``pullback/data`` would send a reader's container at an empty
    directory and silently orphan their tasks, run history, and keys.
    """
    text = README.read_text()
    assert "${DOCKER_VOLUMES}/pullback/data:/data" in text, (
        "documented data mount must match the path the deployment actually uses"
    )
    assert "${DOCKER_VOLUMES}/pullbackup/data" not in text, (
        "data path must not be renamed before an explicit migration procedure"
    )


def test_readme_warns_against_renaming_the_data_directory():
    assert "Do not rename the host data directory" in _readme_prose()


def test_readme_does_not_leak_private_deployment_identifiers():
    """Open-source readiness: no homelab-specific hosts or registries."""
    text = README.read_text().lower()
    for marker in ("lagoon.cloud", "registry-write", "seal.lagoon"):
        assert marker not in text, f"private identifier in README: {marker}"


def test_ui_chrome_stays_lowercase_but_acronyms_are_preserved():
    """Mike's convention: lowercase chrome, uppercase state, acronyms intact."""
    sources = (FRONTEND_SRC / "pages" / "Sources.tsx").read_text()
    assert f"{NEW_NAME}'s SSH public key" in sources
    assert "Pullbackup's SSH" not in sources, "chrome should be lowercase"
    assert "ssh public key" not in sources, "SSH must stay uppercase"
