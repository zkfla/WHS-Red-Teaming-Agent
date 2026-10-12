"""Static UI structure: ids, imports, no external assets, no HTML injection."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient

from redteam.app.web.server import STATIC_DIR

HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SCRIPTS = sorted(STATIC_DIR.glob("*.js"))
PALETTE = {"#283618", "#606c38", "#f4f5eb", "#dda15e", "#bc6c25"}


def html_ids() -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', HTML))


def test_every_dom_id_the_scripts_look_up_exists_in_the_page():
    ids = html_ids()
    missing: set[str] = set()
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        for name in re.findall(r'\$\("([^"]+)"\)', text):
            if name not in ids:
                missing.add(f"{script.name}:{name}")
    assert not missing


def test_tab_panes_match_the_tab_buttons():
    tabs = set(re.findall(r'data-tab="([^"]+)"', HTML))
    assert tabs and all(f"tab-{name}" in html_ids() for name in tabs)


def test_every_relative_import_resolves_to_a_static_file():
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        for target in re.findall(r'from "\./([^"]+)"', text):
            assert (STATIC_DIR / target).is_file(), f"{script.name} imports {target}"


def test_page_links_only_local_assets():
    links = re.findall(r'(?:href|src)="([^"]+)"', HTML)
    assert links
    for link in links:
        assert link.startswith("/static/"), link
        assert (STATIC_DIR / Path(link).name).is_file(), link
    for source in [*SCRIPTS, *STATIC_DIR.glob("*.css")]:
        assert not re.search(r"https?://", source.read_text(encoding="utf-8")), source.name


def test_scripts_never_inject_html():
    banned = (
        "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
    )
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{script.name} uses {token}"


def test_page_has_no_inline_script_or_style_that_the_policy_would_block():
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", HTML)
    assert "<style" not in HTML
    assert " style=" not in HTML
    assert not re.search(r"\son[a-z]+=", HTML)


def test_stylesheets_use_only_the_palette_colors():
    for sheet in STATIC_DIR.glob("*.css"):
        text = sheet.read_text(encoding="utf-8")
        for color in re.findall(r"#[0-9a-fA-F]{6}\b", text):
            assert color.lower() in PALETTE, f"{sheet.name}: {color}"


def test_static_files_are_served_with_script_and_style_types():
    app = Starlette(routes=[Mount("/static", StaticFiles(directory=STATIC_DIR))])
    client = TestClient(app)
    assert "javascript" in client.get("/static/app.js").headers["content-type"]
    assert "css" in client.get("/static/theme.css").headers["content-type"]
    assert client.get("/static/missing.js").status_code == 404


@pytest.mark.parametrize("name", ["app.js", "settings.js", "api.js"])
def test_the_api_key_is_not_kept_in_browser_storage(name):
    text = (STATIC_DIR / name).read_text(encoding="utf-8")
    assert "localStorage" not in text and "sessionStorage" not in text


def test_hidden_attribute_always_hides_even_when_a_class_sets_display():
    theme = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")
    assert "[hidden]" in theme and "display: none !important" in theme


def test_dialog_classes_do_not_collide_with_the_page_stylesheets():
    def top_classes(sheet: Path) -> set[str]:
        text = re.sub(r"/\*.*?\*/", "", sheet.read_text(encoding="utf-8"), flags=re.S)
        found: set[str] = set()
        for selector in re.findall(r"(?m)^([^{}@\n][^{}]*)\{", text):
            for part in selector.split(","):
                part = part.strip()
                if not part.startswith(".dialog"):
                    # The leading class names the component; modifiers like ".on" are scoped by it.
                    lead = re.match(r"\.([A-Za-z][\w-]*)", part)
                    if lead:
                        found.add(lead.group(1))
        return found

    mine = top_classes(STATIC_DIR / "dialog.css")
    others: set[str] = set()
    for sheet in STATIC_DIR.glob("*.css"):
        if sheet.name != "dialog.css":
            others |= top_classes(sheet)
    assert not (mine & others), sorted(mine & others)
