"""Guard the user-facing Matera rename (tranche 1).

Scope: user-visible product naming only — the browser title, the UI chrome, and
the README. Code identifiers (`backend/pullback/`), the `PULLBACK_` env prefix,
the database filename, and deployment identity are deliberately NOT covered
here; those change in later tranches, and asserting on them now would fail
against a deployment that is still running the old names.

Naming convention:
  * lowercase ``matera`` in UI chrome (matches the existing lowercase-chrome
    convention; run/task STATE values stay uppercase)
  * ``Matera Backup`` on first mention in the README, ``Matera`` thereafter
"""

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = REPOSITORY_ROOT / "frontend" / "src"
INDEX_HTML = REPOSITORY_ROOT / "frontend" / "index.html"
FAVICON = REPOSITORY_ROOT / "frontend" / "public" / "favicon.svg"
README = REPOSITORY_ROOT / "README.md"

OLD_NAME = re.compile(r"pullback", re.IGNORECASE)


def _user_facing_frontend_files():
    """Frontend sources that render text to a user."""
    for path in sorted(FRONTEND_SRC.rglob("*")):
        if path.suffix in {".tsx", ".ts"} and not path.name.endswith(".test.ts"):
            yield path


def test_browser_title_uses_the_new_name():
    assert "<title>matera</title>" in INDEX_HTML.read_text()


def test_index_html_has_no_old_brand_reference():
    assert not OLD_NAME.search(INDEX_HTML.read_text())


def test_favicon_accessible_label_uses_the_new_name():
    """The favicon carries an aria-label that screen readers announce."""
    assert not OLD_NAME.search(FAVICON.read_text())


def test_no_user_facing_frontend_string_mentions_the_old_name():
    """Chrome and labels must read 'matera'.

    One documented exception: TaskForm describes the *live* Uptime Kuma monitor
    prefix, which is still literally ``pullback:`` until the backend rename
    ships in tranche 2. Showing the new name there would tell the user
    something false about monitors that exist in their Kuma instance right now.
    """
    exceptions = {"pages/TaskForm.tsx"}
    offenders = []
    for path in _user_facing_frontend_files():
        rel = str(path.relative_to(FRONTEND_SRC))
        if rel in exceptions:
            continue
        if OLD_NAME.search(path.read_text()):
            offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert not offenders, f"old brand name still rendered by: {offenders}"


def test_kuma_help_text_describes_the_real_monitor_prefix():
    """Pin the exception above against the backend's ACTUAL behaviour.

    Review 313 could not validate the claim that Kuma monitors are still named
    ``pullback: <task>``, because ``services/kuma.py`` is not part of this
    diff. So verify it here rather than asserting it in prose: read the string
    the backend really builds and require the help text to agree.

    This also makes the tranche-2 handoff self-enforcing — the moment the
    backend starts naming monitors ``matera:``, this test fails until the help
    text is updated with it, so the two cannot drift apart.
    """
    backend_call = (
        REPOSITORY_ROOT / "backend" / "pullback" / "api" / "tasks.py"
    ).read_text()
    match = re.search(r'create_push_monitor\(\s*f"([^:"]+):', backend_call)
    assert match, "could not locate the Kuma monitor name construction"
    actual_prefix = match.group(1)

    task_form = (FRONTEND_SRC / "pages" / "TaskForm.tsx").read_text()
    assert "matera creates/updates a Kuma push monitor" in task_form
    assert f"<code>{actual_prefix}: {{form.name" in task_form, (
        f"help text must show the prefix the backend actually uses "
        f"({actual_prefix!r})"
    )


def test_header_renders_the_new_wordmark():
    app = (FRONTEND_SRC / "App.tsx").read_text()
    assert "matera" in app, "header wordmark should read 'matera'"


def test_readme_leads_with_the_disambiguating_product_name():
    first_line = README.read_text().splitlines()[0]
    assert first_line.strip() == "# Matera Backup"


def test_readme_has_no_old_product_branding():
    """The README legitimately still contains the old name in three forms:
    ``PULLBACK_`` env vars, the ``pullback.main:app`` dev command, and the
    ``${DOCKER_VOLUMES}/pullback/data`` host path. All three are current,
    working identifiers that later tranches rename. What must be gone is
    *product* branding — prose calling the tool itself "pullback".

    Rather than counting allowed lines (which silently tolerates new
    branding as long as the count fits), strip the known identifiers and
    require that nothing survives.
    """
    text = README.read_text()
    residue = re.sub(r"PULLBACK_[A-Z_]+", "", text)
    residue = residue.replace("pullback.main:app", "")
    # The host data path, in both the compose block and the naming note.
    residue = residue.replace("${DOCKER_VOLUMES}/pullback/data:/data", "")
    residue = residue.replace("${DOCKER_VOLUMES}/pullback/data", "")
    # The naming note names the env prefix in prose.
    residue = residue.replace("the `PULLBACK_`\n> environment prefix", "")

    leftover = [line for line in residue.splitlines() if OLD_NAME.search(line)]
    assert not leftover, f"old product branding in README: {leftover}"


def test_readme_explains_the_transitional_naming_split():
    """Anyone reading the README must not be confused by Matera-the-product
    sitting alongside PULLBACK_-the-env-var."""
    text = README.read_text()
    assert "The product is Matera" in text


def test_readme_documents_the_real_persistent_data_path():
    """Regression guard for review 310.

    The host data directory holds the SQLite database, run logs, and SSH keys.
    It is NOT renamed in this tranche, so the documented bind mount must still
    read ``pullback/data``. Documenting ``matera/data`` while the deployment
    still mounts ``pullback/data`` would send a reader's container at an empty
    directory and silently orphan their tasks, run history, and keys.

    Tranche 3 changes this path, together with an explicit migration procedure.
    """
    text = README.read_text()
    assert "${DOCKER_VOLUMES}/pullback/data:/data" in text, (
        "documented data mount must match the path the deployment actually uses"
    )
    assert "${DOCKER_VOLUMES}/matera/data" not in text, (
        "data path must not be renamed before tranche 3's migration procedure"
    )


def test_readme_warns_against_renaming_the_data_directory():
    """The mixed naming is only safe if the reason is stated."""
    text = README.read_text()
    assert "Do not rename the host data directory" in text


def test_readme_does_not_leak_private_deployment_identifiers():
    """Open-source readiness: no homelab-specific hosts or registries."""
    text = README.read_text().lower()
    for marker in ("lagoon.cloud", "registry-write", "seal.lagoon"):
        assert marker not in text, f"private identifier in README: {marker}"


def test_ui_chrome_stays_lowercase_but_acronyms_are_preserved():
    """Mike's convention: lowercase chrome, uppercase state, acronyms intact."""
    sources = (FRONTEND_SRC / "pages" / "Sources.tsx").read_text()
    assert "matera's SSH public key" in sources
    assert "Matera's SSH" not in sources, "chrome should be lowercase"
    assert "ssh public key" not in sources, "SSH must stay uppercase"
