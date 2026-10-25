"""导入两步表单页面集成测试（IMP-04）：解析、手填降级、确认导入、徽标。"""

from datetime import UTC, datetime, timedelta

from test_ui_flows import HEADERS

from conftest import NOW, activate
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.pipeline.dedupe import canonicalize
from papersteward.reading.import_service import ResolvedIdentifier, import_read_paper
from papersteward.sources.contracts import PaperCandidate, SourceError
from papersteward.storage import create_session_factory, session_scope


def _post(client, path: str, data: dict[str, str]):
    return client.post(path, data=data, headers=HEADERS)


def _resolved(doi: str = "10.0000/resolved") -> ResolvedIdentifier:
    candidate = PaperCandidate.model_validate(
        {
            "title": "解析得到的论文",
            "provenance": {
                "source": "openalex",
                "source_id": "W999",
                "url": "https://openalex.org/W999",
                "retrieved_at": NOW,
                "raw_metadata": {},
            },
            "doi": doi,
            "authors": ("解析作者",),
            "venue": "解析 Venue",
            "published_at": datetime(2025, 3, 1, tzinfo=UTC),
            "url": "https://example.org/resolved",
            "abstract": "解析摘要",
        }
    )
    return ResolvedIdentifier(candidate=canonicalize(candidate))


def _service_import(client, doi: str, title: str = "服务层导入论文") -> None:
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        import_read_paper(
            session,
            ImportReadPaperInput.model_validate({"title": title, "score": 3, "doi": doi}),
            now=NOW + timedelta(hours=1),
        )


def test_entry_page_and_archive_entry_button(client, migrated_engine):
    activate(client)
    entry = client.get("/ui/import")
    assert entry.status_code == 200
    assert 'action="/ui/import/resolve"' in entry.text
    assert "第一步" in entry.text
    archive = client.get("/ui/archive")
    assert 'href="/ui/import"' in archive.text and "导入已读论文" in archive.text


def test_manual_two_step_import_success_and_badges(client, migrated_engine):
    activate(client)
    # 第一步留空标识符 → 直接进入手填表单，无解析提示。
    manual = _post(client, "/ui/import/resolve", {"identifier": ""})
    assert manual.status_code == 200
    assert "第二步" in manual.text and 'action="/ui/import/confirm"' in manual.text
    assert "暂时无法自动解析" not in manual.text

    confirm = client.post(
        "/ui/import/confirm",
        data={
            "title": "页面导入论文",
            "authors": "A 作者，B 作者",
            "published_year": "2024",
            "score": "4",
            "note": "页面导入备注",
        },
        headers=HEADERS,
        follow_redirects=False,
    )
    assert confirm.status_code == 303
    location = confirm.headers["location"]
    assert location.startswith("/ui/papers/") and location.endswith("?imported=1")

    detail = client.get(location)
    assert detail.status_code == 200
    assert "已导入这篇已读论文并完成归档" in detail.text
    assert "已完成归档" in detail.text and '<span class="tag">导入</span>' in detail.text
    # content_json 无 summary 键 → 模型概括落兜底文案（IMP-06 兼容）。
    assert "历史概括不可用" in detail.text
    assert "A 作者, B 作者" in detail.text

    archive = client.get("/ui/archive")
    assert '<span class="tag">导入</span>' in archive.text
    assert "页面导入论文" in archive.text


def test_resolve_success_prefills_confirm_form(client, migrated_engine, monkeypatch):
    activate(client)
    monkeypatch.setattr(
        "papersteward.api.import_pages.resolve_identifier", lambda raw, *, now: _resolved()
    )
    resolved_page = _post(client, "/ui/import/resolve", {"identifier": "10.0000/resolved"})
    assert resolved_page.status_code == 200
    assert 'value="解析得到的论文"' in resolved_page.text
    assert 'value="10.0000/resolved"' in resolved_page.text
    assert 'value="解析 Venue"' in resolved_page.text
    assert 'value="2025"' in resolved_page.text
    assert "解析得到的论文" in resolved_page.text
    # 解析溯源经隐藏字段带入确认步（JSON 值在属性里经 HTML 转义）。
    assert 'name="resolve"' in resolved_page.text and "W999" in resolved_page.text
    assert 'name="identifier_input" value="10.0000/resolved"' in resolved_page.text


def test_resolve_failure_degrades_to_manual_form(client, migrated_engine, monkeypatch):
    activate(client)

    def boom(raw, *, now):
        raise SourceError("openalex", "timeout")

    monkeypatch.setattr("papersteward.api.import_pages.resolve_identifier", boom)
    page = _post(client, "/ui/import/resolve", {"identifier": "10.0000/anything"})
    assert page.status_code == 200
    assert "暂时无法自动解析，请手动填写" in page.text
    assert "第二步" in page.text


def test_resolve_miss_hint_when_identifier_unknown(client, migrated_engine, monkeypatch):
    activate(client)
    monkeypatch.setattr(
        "papersteward.api.import_pages.resolve_identifier", lambda raw, *, now: None
    )
    page = _post(client, "/ui/import/resolve", {"identifier": "10.0000/missing"})
    assert page.status_code == 200
    assert "没有从来源找到这篇论文" in page.text


def test_resolve_duplicate_shows_detail_link(client, migrated_engine, monkeypatch):
    activate(client)
    _service_import(client, "10.5555/dup.1")
    monkeypatch.setattr(
        "papersteward.api.import_pages.resolve_identifier",
        lambda raw, *, now: _resolved("10.5555/dup.1"),
    )
    page = _post(client, "/ui/import/resolve", {"identifier": "10.5555/dup.1"})
    assert page.status_code == 409
    assert "该论文已归档" in page.text
    assert "打开论文详情页" in page.text
    assert 'href="/ui/papers/' in page.text


def test_confirm_validation_error_prefills_fields(client, migrated_engine):
    activate(client)
    page = _post(
        client,
        "/ui/import/confirm",
        {"title": "", "authors": "保留作者", "score": ""},
    )
    assert page.status_code == 400
    assert 'value="保留作者"' in page.text

    page = _post(client, "/ui/import/confirm", {"title": "有标题", "authors": "", "score": ""})
    assert page.status_code == 400
    assert "请选择 1–5 的评分" in page.text
    assert 'value="有标题"' in page.text

    page = _post(client, "/ui/import/confirm", {"title": "", "score": "4"})
    assert page.status_code == 400
    assert "请填写论文标题" in page.text


def test_confirm_unknown_tag_rejected(client, migrated_engine):
    activate(client)
    page = _post(
        client,
        "/ui/import/confirm",
        {"title": "带未知标签", "score": "3", "primary_tag": "NOPE"},
    )
    assert page.status_code == 400
    assert "所选标签不在当前分类法中" in page.text


def test_confirm_duplicate_final_guard(client, migrated_engine):
    activate(client)
    _service_import(client, "10.5555/dup.2", title="已归档论文")
    page = _post(
        client,
        "/ui/import/confirm",
        {"title": "换个标题再导一次", "score": "3", "doi": "10.5555/dup.2"},
    )
    assert page.status_code == 409
    assert "该论文已归档" in page.text
    assert "打开论文详情页" in page.text


def test_confirm_year_validation(client, migrated_engine):
    activate(client)
    page = _post(
        client,
        "/ui/import/confirm",
        {"title": "年份检查", "score": "3", "published_year": "abcd"},
    )
    assert page.status_code == 400
    assert "发表年份应是数字" in page.text
    page = _post(
        client,
        "/ui/import/confirm",
        {"title": "年份检查", "score": "3", "published_year": "999"},
    )
    assert page.status_code == 400
    assert "1500–2100" in page.text
