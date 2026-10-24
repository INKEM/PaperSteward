"""静态资产版本号（内容哈希）的行为契约。"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import HTMLResponse

from papersteward.api.html import _ASSET_VERSION, _asset_digest, page


def test_asset_digest_is_stable_for_same_content(tmp_path: Path) -> None:
    asset = tmp_path / "style.css"
    asset.write_text("body { color: red }", encoding="utf-8")
    first = _asset_digest([asset])
    assert first == _asset_digest([asset])
    assert len(first) == 12


def test_asset_digest_changes_when_content_changes(tmp_path: Path) -> None:
    asset = tmp_path / "style.css"
    asset.write_text("body { color: red }", encoding="utf-8")
    before = _asset_digest([asset])
    asset.write_text("body { color: blue }", encoding="utf-8")
    assert before != _asset_digest([asset])


def test_asset_digest_depends_on_every_file(tmp_path: Path) -> None:
    css = tmp_path / "style.css"
    js = tmp_path / "setup.js"
    css.write_text("a", encoding="utf-8")
    js.write_text("a", encoding="utf-8")
    assert _asset_digest([css, js]) != _asset_digest([css])


def test_page_references_hashed_stylesheet_url() -> None:
    response = page("测试", "<p>x</p>")
    assert isinstance(response, HTMLResponse)
    html = response.body.decode()
    assert f"/ui/static/style.css?v={_ASSET_VERSION}" in html


def test_page_provides_network_alert_container() -> None:
    """UXR-20：外壳自带网络失败提示条，由 setup.js 填充与聚焦。"""
    html = page("测试", "<p>x</p>").body.decode()
    assert 'id="network-alert" role="alert" hidden' in html


def test_setup_js_handles_htmx_network_failures() -> None:
    js = (Path(__file__).resolve().parents[2] / "src/papersteward/static/setup.js").read_text(
        encoding="utf-8"
    )
    assert "htmx:sendError" in js and "htmx:responseError" in js
    assert "showNetworkAlert" in js
