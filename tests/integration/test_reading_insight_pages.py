"""阅读回顾页面集成测试（R1-02）：详情页阅读时间展示、补记表单与错误保留。"""

from datetime import UTC, datetime

from test_ui_flows import HEADERS

from conftest import activate
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.reading.import_service import import_read_paper
from papersteward.storage import create_session_factory, session_scope

NOW2 = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)


def _import_paper(client, title: str) -> str:
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        return import_read_paper(
            session,
            ImportReadPaperInput.model_validate({"title": title, "score": 4}),
            now=NOW2,
        )


def _post_read_time(client, paper_id: str, data: dict[str, str]):
    return client.post(
        f"/ui/reading-insights/papers/{paper_id}/read-time",
        data={
            "expected_revision": "0",
            "idempotency_key": "page-key-1",
            **data,
        },
        headers=HEADERS,
    )


def test_import_detail_shows_archive_and_unknown_reading_time(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "页面导入论文")
    detail = client.get(f"/ui/papers/{paper_id}")
    assert detail.status_code == 200
    # 归档时间与阅读时间分开呈现；导入未声明时如实显示未记录（文案修正）。
    assert "归档于" in detail.text and "阅读时间" in detail.text
    assert "未记录" in detail.text
    assert "完成于" not in detail.text
    assert "记录阅读时间" in detail.text


def test_read_time_form_success_updates_detail(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "表单导入论文")
    response = _post_read_time(
        client,
        paper_id,
        {"precision": "day", "day": "2026-10-20", "month": ""},
    )
    assert response.status_code == 200
    assert "已记录阅读时间" in response.text
    assert "2026-10-20" in response.text
    assert "阅读时间修订记录" in response.text

    detail = client.get(f"/ui/papers/{paper_id}")
    assert "2026-10-20" in detail.text
    assert "（系统记录）" not in detail.text


def test_read_time_form_conflict_preserves_input(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "冲突导入论文")
    first = _post_read_time(
        client, paper_id, {"precision": "day", "day": "2026-10-20", "month": ""}
    )
    assert first.status_code == 200
    stale = _post_read_time(
        client, paper_id, {"precision": "day", "day": "2026-10-21", "month": ""}
    )
    assert stale.status_code == 409
    assert "已保留你的输入" in stale.text
    # 保留用户已填输入（X-01）。
    assert 'name="day" value="2026-10-21"' in stale.text


def test_read_time_form_rejects_cross_site(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "跨站导入论文")
    response = client.post(
        f"/ui/reading-insights/papers/{paper_id}/read-time",
        data={"precision": "unknown", "expected_revision": "0"},
    )
    assert response.status_code == 409


def test_read_time_form_rejects_future_date(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "未来日期论文")
    response = _post_read_time(
        client,
        paper_id,
        {"precision": "day", "day": "2027-01-01", "month": ""},
    )
    assert response.status_code == 400
    assert "阅读日期不能晚于今天" in response.text


def test_read_time_form_unknown_clears_label(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "清除导入论文")
    _post_read_time(client, paper_id, {"precision": "day", "day": "2026-10-20", "month": ""})
    cleared = _post_read_time(
        client,
        paper_id,
        {
            "precision": "unknown",
            "day": "",
            "month": "",
            "expected_revision": "1",
            "idempotency_key": "page-key-2",
        },
    )
    assert cleared.status_code == 200
    assert "未记录" in cleared.text


def test_archive_list_labels_import_copy(client, migrated_engine):
    activate(client)
    paper_id = _import_paper(client, "列表导入论文")
    archive = client.get("/ui/archive")
    assert archive.status_code == 200
    assert "导入于" in archive.text
    assert f'href="/ui/papers/{paper_id}"' in archive.text
