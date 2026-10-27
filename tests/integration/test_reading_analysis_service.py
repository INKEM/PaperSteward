"""阅读分析授权门、材料清单与摘录的集成验证（R2-02）。

覆盖：授权矩阵（未配置/正常/配置变化/端点变化/类别扩大/撤回/once 绑定）、
未授权零 provider 构造、gateway 强制关闭缓存、清单构建锚点、预览令牌
生命周期、摘录幂等与工作区隔离。
"""

from datetime import UTC, datetime, timedelta

import pytest
from test_reading_insight_projection import (
    T_DONE,
    seed_archive,
    seed_paper,
    system_content,
)
from test_ui_flows import HEADERS

from conftest import activate, activate_workspace
from papersteward.config import Settings
from papersteward.delivery.contracts import ImportReadPaperInput
from papersteward.errors import InvalidRequestError, NotAuthorizedError, NotFoundError
from papersteward.llm.runtime import LLMSettingsPayload, save_llm_config
from papersteward.reading.import_service import import_read_paper
from papersteward.reading.insights.authorization import (
    build_analysis_gateway,
    ensure_send_authorized,
    grant_analysis_authorization,
)
from papersteward.reading.insights.contracts import ExcerptInput
from papersteward.reading.insights.materials import (
    add_excerpt,
    create_manifest,
    create_preview_token,
    delete_excerpt,
    list_excerpts,
    manifest_content_of,
    validate_preview_token,
)
from papersteward.storage import session_scope
from papersteward.storage.db import create_session_factory
from papersteward.storage.repositories import ConfigRepository

NOW2 = datetime(2026, 10, 25, 18, 0, tzinfo=UTC)
SETTINGS = Settings()


def connect_llm(
    factory, *, base_url: str = "http://127.0.0.1:9/v1", model: str = "test-model"
) -> str:
    """保存模型连接（本机地址无需密钥），返回配置行版本。"""
    with session_scope(factory) as session:
        save_llm_config(
            session,
            LLMSettingsPayload.model_validate(
                {"base_url": base_url, "model": model, "consent": True}
            ),
            SETTINGS,
        )
        row = ConfigRepository(session).latest("llm_config")
        assert row is not None
        return row.version


def seed_archived(factory, paper_id: str, *, note: str | None = "初读备注") -> None:
    with session_scope(factory) as session:
        seed_paper(session, paper_id, first_seen_at=T_DONE)
        seed_archive(
            session,
            paper_id,
            archived_at=T_DONE,
            content=system_content(title=f"分析-{paper_id}", completed_at=T_DONE),
            rating_col=4,
            note_col=note,
            ratings=[(T_DONE, 4, note)] if note else [],
        )


def grant(factory, *, scope_mode: str = "workspace", classes=None, manifest_hash=None):
    with session_scope(factory) as session:
        return grant_analysis_authorization(
            session,
            scope_mode=scope_mode,
            material_classes=classes or ["user_notes"],
            settings=SETTINGS,
            now=NOW2,
            manifest_hash=manifest_hash,
        )


def gate(factory, *, kinds=("user_notes",), manifest_hash=None):
    with session_scope(factory) as session:
        return ensure_send_authorized(
            session,
            settings=SETTINGS,
            manifest_hash=manifest_hash,
            material_kinds=kinds,
        )


# —— 授权矩阵 ————————————————————————————————————————————————


def test_gate_without_any_grant_rejects_even_with_llm_configured(insights_factory):
    """仅有既有模型授权时，阅读分析发送边界的调用次数必须为零。"""
    factory = insights_factory
    connect_llm(factory)
    with pytest.raises(NotAuthorizedError):
        gate(factory)


def test_gate_rejects_without_provider_construction(insights_factory, monkeypatch):
    """未授权路径不构造任何 provider：发送在边界即被拒绝。"""
    factory = insights_factory
    connect_llm(factory)
    from papersteward.reading.insights import authorization

    counter = {"count": 0}

    def counting_provider(*args, **kwargs):
        counter["count"] += 1
        raise AssertionError("未授权路径不应构造 provider")

    monkeypatch.setattr(authorization, "build_llm_provider", counting_provider)
    with pytest.raises(NotAuthorizedError):
        gate(factory)
    assert counter["count"] == 0


def test_grant_requires_llm_config(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session, pytest.raises(InvalidRequestError) as excinfo:
        grant_analysis_authorization(
            session,
            scope_mode="workspace",
            material_classes=["user_notes"],
            settings=SETTINGS,
            now=NOW2,
        )
    assert "模型连接" in excinfo.value.message


def test_grant_and_gate_pass_with_bound_version(insights_factory):
    factory = insights_factory
    version = connect_llm(factory)
    view = grant(factory)
    assert view.model_config_version == version
    assert view.consent_version == "reading-analysis.consent/1"
    resolved = gate(factory)
    assert resolved.grant_id == view.grant_id
    assert resolved.model_config_version == version


def test_any_llm_config_save_invalidates_existing_grant(insights_factory):
    factory = insights_factory
    connect_llm(factory)
    grant(factory)
    connect_llm(factory, model="另一型号")  # 任何设置保存都换配置版本
    with pytest.raises(NotAuthorizedError, match="重新核对"):
        gate(factory)


def test_endpoint_change_invalidates_grant(insights_factory):
    factory = insights_factory
    connect_llm(factory, base_url="http://127.0.0.1:9/v1")
    grant(factory)
    connect_llm(factory, base_url="http://127.0.0.1:10/v1")
    with pytest.raises(NotAuthorizedError):
        gate(factory)


def test_class_expansion_requires_new_grant(insights_factory):
    factory = insights_factory
    connect_llm(factory)
    grant(factory, classes=["user_notes"])
    with pytest.raises(NotAuthorizedError) as excinfo:
        gate(factory, kinds=("user_notes", "user_excerpt"))
    assert excinfo.value.details == {"missing_classes": ["user_excerpt"]}


def test_revoke_blocks_gate_and_records_event(insights_factory):
    factory = insights_factory
    connect_llm(factory)
    view = grant(factory)
    with session_scope(factory) as session:
        from papersteward.reading.insights.authorization import revoke_analysis_authorization as rv

        rv(session, grant_id=view.grant_id, now=NOW2)
        again = rv(session, grant_id=view.grant_id, now=NOW2)  # 幂等
    assert again.revoked_at is not None
    with pytest.raises(NotAuthorizedError):
        gate(factory)


def test_once_grant_binds_manifest_hash(insights_factory):
    factory = insights_factory
    connect_llm(factory)
    grant(factory, scope_mode="once", manifest_hash="a" * 64)
    resolved = gate(factory, manifest_hash="a" * 64)
    assert resolved.purpose == "reading_reflection"
    with pytest.raises(NotAuthorizedError):
        gate(factory, manifest_hash="b" * 64)


def test_workspace_isolation(insights_factory, migrated_engine, tmp_path):
    """授权按工作区（独立库）隔离：另一工作区无授权即拒绝。"""
    factory = insights_factory
    connect_llm(factory)
    grant(factory)
    from papersteward.storage.db import create_db_engine, create_session_factory

    other = create_db_engine(f"sqlite:///{tmp_path}/ws-other.db")
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path("alembic.ini").resolve()))
    cfg.set_main_option("script_location", str(Path("migrations").resolve()))
    cfg.attributes["connection"] = other.connect()
    command.upgrade(cfg, "head")
    activate_workspace(other)
    other_factory = create_session_factory(other)
    connect_llm(other_factory)
    with pytest.raises(NotAuthorizedError):
        gate(other_factory)


# —— gateway 构造 ——————————————————————————————————————————————


def test_build_analysis_gateway_never_caches(insights_factory, monkeypatch):
    factory = insights_factory
    connect_llm(factory)
    grant(factory)
    resolved = gate(factory)
    from papersteward.llm.provider import FakeLLM
    from papersteward.reading.insights import authorization

    monkeypatch.setattr(authorization, "build_llm_provider", lambda *a, **k: FakeLLM(["ok"]))
    gateway = build_analysis_gateway(resolved)
    assert gateway._cache is None  # noqa: SLF001 - 私人分析关闭通用缓存是计划要求


# —— 材料清单与预览令牌 ————————————————————————————————————————


def test_manifest_build_anchors_and_reuse(insights_factory):
    factory = insights_factory
    seed_archived(factory, "mat-a")
    with session_scope(factory) as session:
        from papersteward.reading.insights.materials import preview_materials

        content = preview_materials(
            session,
            paper_ids=["mat-a"],
            kinds=["archive_metadata", "model_summary", "user_rating", "user_notes"],
            now=NOW2,
        )
    kinds = {(ref.kind, ref.target_id) for ref in content.materials}
    assert ("archive_metadata", "mat-a") in kinds
    assert ("model_summary", "mat-a") in kinds
    assert ("user_rating", "mat-a") in kinds
    assert ("user_notes", "mat-a") in kinds
    notes = next(ref for ref in content.materials if ref.kind == "user_notes")
    assert notes.fields == ["note"]
    assert notes.ref_hash != notes.version_key
    assert content.coverage.archived_total == 1
    assert content.coverage.selected_papers == 1

    with session_scope(factory) as session:
        row, created = create_manifest(
            session,
            paper_ids=["mat-a"],
            kinds=["archive_metadata", "model_summary", "user_rating", "user_notes"],
            now=NOW2,
        )
        assert created is True
        same, again = create_manifest(
            session,
            paper_ids=["mat-a"],
            kinds=["archive_metadata", "model_summary", "user_rating", "user_notes"],
            now=NOW2,
        )
        assert again is False and same.id == row.id
        loaded = manifest_content_of(row)
        assert len(loaded.materials) == 4


def test_manifest_missing_paper_rejected(insights_factory):
    factory = insights_factory
    seed_archived(factory, "mat-b")
    with session_scope(factory) as session, pytest.raises(InvalidRequestError) as excinfo:
        from papersteward.reading.insights.materials import preview_materials

        preview_materials(
            session, paper_ids=["mat-b", "ghost"], kinds=["archive_metadata"], now=NOW2
        )
    assert excinfo.value.details == {"paper_ids": ["ghost"]}


def test_manifest_exclusion_skips_paper(insights_factory):
    factory = insights_factory
    seed_archived(factory, "mat-c")
    with session_scope(factory) as session:
        from papersteward.reading.insights.contracts import MaterialExclusion
        from papersteward.reading.insights.materials import preview_materials

        content = preview_materials(
            session,
            paper_ids=["mat-c"],
            kinds=["user_notes"],
            exclusions=[
                MaterialExclusion.model_validate({"paper_id": "mat-c", "reason": "任务无关"})
            ],
            now=NOW2,
        )
    assert content.materials == []
    assert content.exclusions[0].reason == "任务无关"


def test_preview_token_lifecycle(insights_factory):
    factory = insights_factory
    connect_llm(factory)
    seed_archived(factory, "mat-d")
    with session_scope(factory) as session:
        row, _ = create_manifest(session, paper_ids=["mat-d"], kinds=["archive_metadata"], now=NOW2)
        view = create_preview_token(session, manifest_id=row.id, now=NOW2)
    token, manifest_hash = view.token, row.manifest_hash
    with session_scope(factory) as session:
        assert view.consumed is False
        validate_preview_token(session, token=token, manifest_hash=manifest_hash, now=NOW2)
        # 清单不符
        with pytest.raises(InvalidRequestError):
            validate_preview_token(session, token=token, manifest_hash="c" * 64, now=NOW2)
        # 过期
        with pytest.raises(NotAuthorizedError):
            validate_preview_token(
                session, token=token, manifest_hash=manifest_hash, now=NOW2 + timedelta(minutes=16)
            )
    # 模型配置变化（换会话写配置，避免读事务持锁）
    connect_llm(factory, model="换了型号")
    with session_scope(factory) as session, pytest.raises(InvalidRequestError):
        validate_preview_token(session, token=token, manifest_hash=manifest_hash, now=NOW2)


def test_preview_token_requires_manifest(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session, pytest.raises(NotFoundError):
        create_preview_token(session, manifest_id="ghost", now=NOW2)


# —— 摘录 ———————————————————————————————————————————————————


def test_excerpt_add_dedupe_list_delete(insights_factory):
    factory = insights_factory
    seed_archived(factory, "exc-a")
    with session_scope(factory) as session:
        first = add_excerpt(
            session,
            "exc-a",
            data=ExcerptInput.model_validate({"content": "关键句原文", "source_note": "第 2 页"}),
            now=NOW2,
        )
        duplicate = add_excerpt(
            session,
            "exc-a",
            data=ExcerptInput.model_validate({"content": "关键句原文"}),
            now=NOW2,
        )
        assert duplicate.excerpt_id == first.excerpt_id
        items = list_excerpts(session, "exc-a")
        assert [item.excerpt_id for item in items] == [first.excerpt_id]
        delete_excerpt(session, "exc-a", first.excerpt_id)
        assert list_excerpts(session, "exc-a") == []
        with pytest.raises(NotFoundError):
            delete_excerpt(session, "exc-a", first.excerpt_id)


def test_excerpt_requires_archived_paper(insights_factory):
    factory = insights_factory
    with session_scope(factory) as session, pytest.raises(NotFoundError):
        add_excerpt(session, "ghost", data=ExcerptInput.model_validate({"content": "x"}), now=NOW2)


def test_excerpt_cascades_with_paper(insights_factory):
    factory = insights_factory
    seed_archived(factory, "exc-b")
    with session_scope(factory) as session:
        view = add_excerpt(
            session, "exc-b", data=ExcerptInput.model_validate({"content": "x"}), now=NOW2
        )
    with session_scope(factory) as session:
        from sqlalchemy import delete as sa_delete

        from papersteward.storage.models import PaperRow

        session.execute(sa_delete(PaperRow).where(PaperRow.id == "exc-b"))
    with session_scope(factory) as session:
        assert list_excerpts(session, "exc-b") == []
        with pytest.raises(NotFoundError):
            delete_excerpt(session, "exc-b", view.excerpt_id)


# —— JSON 接口 ——————————————————————————————————————————————————————


def test_json_analysis_api_roundtrip(client, migrated_engine):
    activate_workspace(migrated_engine)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed_paper(session, "api-a", first_seen_at=T_DONE)
        seed_archive(
            session,
            "api-a",
            archived_at=T_DONE,
            content=system_content(title="API分析", completed_at=T_DONE),
            rating_col=4,
            note_col="初读备注",
            ratings=[(T_DONE, 4, "初读备注")],
        )
    with session_scope(factory) as session:
        save_llm_config(
            session,
            LLMSettingsPayload.model_validate(
                {"base_url": "http://127.0.0.1:9/v1", "model": "test-model", "consent": True}
            ),
            SETTINGS,
        )

    status_view = client.get("/reading-insights/analysis/authorization")
    assert status_view.status_code == 200
    body = status_view.json()
    assert body["llm_configured"] is True
    assert body["llm_endpoint_identity"] == "http://127.0.0.1:9/v1"
    assert body["grants"] == []

    granted = client.post(
        "/reading-insights/analysis/grants",
        json={
            "scope_mode": "workspace",
            "material_classes": ["archive_metadata", "user_notes"],
            "acknowledged": True,
        },
    )
    assert granted.status_code == 201
    grant_view = granted.json()
    assert grant_view["model_config_version"] == body["llm_config_version"]

    manifest = client.post(
        "/reading-insights/analysis/manifests",
        json={"paper_ids": ["api-a"], "kinds": ["archive_metadata", "user_notes"]},
    )
    assert manifest.status_code == 201
    payload = manifest.json()
    assert payload["manifest"]["coverage"]["selected_papers"] == 1
    assert payload["preview"]["consumed"] is False

    preview = client.get(f"/reading-insights/analysis/previews/{payload['preview']['token']}")
    assert preview.status_code == 200
    assert preview.json()["valid"] is True

    missing = client.post(
        "/reading-insights/analysis/manifests",
        json={"paper_ids": ["ghost"], "kinds": ["archive_metadata"]},
    )
    assert missing.status_code == 400

    bad_grant = client.post(
        "/reading-insights/analysis/grants",
        json={"scope_mode": "workspace", "material_classes": ["user_notes"]},
    )
    assert bad_grant.status_code == 422

    revoke = client.post(f"/reading-insights/analysis/grants/{grant_view['grant_id']}/revoke")
    assert revoke.status_code == 200
    assert revoke.json()["revoked_at"] is not None
    status_after = client.get("/reading-insights/analysis/authorization")
    grants = status_after.json()["grants"]
    assert grants[0]["revoked_at"] is not None


def test_json_excerpt_api_roundtrip(client, migrated_engine):
    activate_workspace(migrated_engine)
    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        seed_paper(session, "api-exc", first_seen_at=T_DONE)
        seed_archive(
            session,
            "api-exc",
            archived_at=T_DONE,
            content=system_content(title="API摘录", completed_at=T_DONE),
        )

    created = client.post(
        "/reading-insights/papers/api-exc/excerpts",
        json={"content": "摘录正文", "source_note": "第 3 页"},
    )
    assert created.status_code == 201
    excerpt = created.json()

    listing = client.get("/reading-insights/papers/api-exc/excerpts")
    assert listing.status_code == 200
    assert [item["excerpt_id"] for item in listing.json()] == [excerpt["excerpt_id"]]

    removed = client.delete(f"/reading-insights/papers/api-exc/excerpts/{excerpt['excerpt_id']}")
    assert removed.status_code == 204
    assert client.get("/reading-insights/papers/api-exc/excerpts").json() == []

    missing_paper = client.post("/reading-insights/papers/ghost/excerpts", json={"content": "x"})
    assert missing_paper.status_code == 404


# —— 页面 ——————————————————————————————————————————————————————


def _connect_llm_http(factory) -> None:
    with session_scope(factory) as session:
        save_llm_config(
            session,
            LLMSettingsPayload.model_validate(
                {"base_url": "http://127.0.0.1:9/v1", "model": "test-model", "consent": True}
            ),
            SETTINGS,
        )


def test_analysis_page_renders_blocks(client, migrated_engine):
    activate(client)
    _connect_llm_http(create_session_factory(client.app.state.engine))
    page = client.get("/ui/reading-insights/analysis")
    assert page.status_code == 200
    assert "阅读分析" in page.text
    # 模型连接只读摘要与连接设置分别表达
    assert "127.0.0.1:9/v1" in page.text
    assert "连接只在设置页修改" in page.text
    # 授权表单：范围单选、类别勾选、知情确认必勾
    assert 'name="acknowledged" value="1" required' in page.text
    assert 'name="cls_user_notes"' in page.text
    assert "还没有授权记录" in page.text
    # 材料预览表单折叠
    assert "材料清单预览（不发送任何请求）" in page.text


def test_analysis_grant_page_flow(client, migrated_engine):
    activate(client)
    _connect_llm_http(create_session_factory(client.app.state.engine))

    # 未勾选确认 → 400 且保留选择
    denied = client.post(
        "/ui/reading-insights/analysis/grants",
        data={"scope_mode": "workspace", "cls_user_notes": "1"},
        headers=HEADERS,
    )
    assert denied.status_code == 400
    assert "知情确认" in denied.text

    granted = client.post(
        "/ui/reading-insights/analysis/grants",
        data={
            "scope_mode": "workspace",
            "cls_user_notes": "1",
            "cls_archive_metadata": "1",
            "acknowledged": "1",
        },
        headers=HEADERS,
    )
    assert granted.status_code == 200
    assert "已保存授权" in granted.text
    assert "生效中" in granted.text

    revoked = client.post("/ui/reading-insights/analysis/grants/_missing_/revoke", headers=HEADERS)
    assert revoked.status_code == 404


def test_analysis_preview_page_flow(client, migrated_engine):
    activate(client)
    factory = create_session_factory(client.app.state.engine)
    # 预览令牌绑定模型配置：先连接模型（R2-03 起预览即建清单与令牌）。
    from test_reading_analysis_runner import connect_llm

    connect_llm(factory)
    with session_scope(factory) as session:
        seed_paper(session, "page-a", first_seen_at=T_DONE)
        seed_archive(
            session,
            "page-a",
            archived_at=T_DONE,
            content=system_content(title="页面预览", completed_at=T_DONE),
            rating_col=4,
            note_col="有备注",
            ratings=[(T_DONE, 4, "有备注")],
        )
    empty = client.post(
        "/ui/reading-insights/analysis/preview",
        data={"paper_ids": "", "kind_archive_metadata": "1"},
        headers=HEADERS,
    )
    assert empty.status_code == 400

    preview = client.post(
        "/ui/reading-insights/analysis/preview",
        data={
            "paper_ids": "page-a",
            "kind_archive_metadata": "1",
            "kind_user_notes": "1",
        },
        headers=HEADERS,
    )
    assert preview.status_code == 200
    assert "选中 1 篇" in preview.text
    assert "冻结元数据／导入摘要" in preview.text
    assert "未发送任何请求" in preview.text

    ghost = client.post(
        "/ui/reading-insights/analysis/preview",
        data={"paper_ids": "ghost", "kind_archive_metadata": "1"},
        headers=HEADERS,
    )
    assert ghost.status_code == 400
    assert "尚未归档" in ghost.text


def test_detail_excerpt_block_flow(client, migrated_engine):
    activate(client)
    factory = create_session_factory(client.app.state.engine)
    with session_scope(factory) as session:
        paper_id = import_read_paper(
            session,
            ImportReadPaperInput.model_validate({"title": "摘录页面论文", "score": 4}),
            now=NOW2,
        )
    detail = client.get(f"/ui/papers/{paper_id}")
    assert detail.status_code == 200
    assert "补充摘录（可选）" in detail.text
    assert "私人材料" in detail.text

    saved = client.post(
        f"/ui/reading-insights/papers/{paper_id}/excerpt",
        data={"content": "关键原文片段", "source_note": "第 2 页"},
        headers=HEADERS,
    )
    assert saved.status_code == 200
    assert "已保存摘录" in saved.text
    assert "关键原文片段" in saved.text
    assert "出处：第 2 页" in saved.text

    # 幂等：同内容重复提交不产生第二条
    again = client.post(
        f"/ui/reading-insights/papers/{paper_id}/excerpt",
        data={"content": "关键原文片段"},
        headers=HEADERS,
    )
    assert again.status_code == 200
    detail = client.get(f"/ui/papers/{paper_id}")
    assert detail.text.count("关键原文片段") == 1

    empty = client.post(
        f"/ui/reading-insights/papers/{paper_id}/excerpt",
        data={"content": ""},
        headers=HEADERS,
    )
    assert empty.status_code == 422
