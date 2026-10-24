from __future__ import annotations

import json
import time
from pathlib import Path

from sqlalchemy.engine import Engine

from papersteward.config import get_settings
from papersteward.core.enums import RunStatus
from papersteward.llm.runtime import LLMSettingsPayload, save_llm_config
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.repositories import RunRepository

HEADERS = {"origin": "http://testserver", "content-type": "application/x-www-form-urlencoded"}
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXTRACTION = json.loads((FIXTURES / "llm/profile_extraction.valid.json").read_text())


def post(client, path, data):
    return client.post(path, data=data, headers=HEADERS)


def configure(engine: Engine) -> None:
    with session_scope(create_session_factory(engine)) as session:
        save_llm_config(
            session,
            LLMSettingsPayload(
                base_url="https://api.example.com/v1",
                api_key="sk-test-1234",
                model="test-model",
                consent=True,
            ),
            get_settings(),
        )


def extraction_provider(monkeypatch, responses: list[dict] | None = None) -> list[int]:
    """让后台抽取使用可脚本化的 FakeLLM；返回调用计数。"""
    from papersteward.llm.provider import FakeLLM

    payloads = responses or [EXTRACTION, EXTRACTION]
    calls: list[int] = []

    def build(_config):
        index = min(len(calls), len(payloads) - 1)
        calls.append(index)
        return FakeLLM(responses=(json.dumps(payloads[index], ensure_ascii=False),))

    monkeypatch.setattr("papersteward.onboarding.intake.build_llm_provider", build)
    return calls


def wait_run(engine: Engine, identity: str) -> object:
    factory = create_session_factory(engine)
    for _ in range(600):
        with session_scope(factory) as session:
            report = RunRepository(session).get(identity)
        if report is not None and report.status is not RunStatus.RUNNING:
            return report
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def add_material(
    client, content: str = "我研究长文档检索与检索增强生成，关注事实性与可复现评测。"
) -> None:
    response = post(client, "/ui/setup/materials", {"name": "研究简介", "content": content})
    assert response.status_code == 200, response.text


def test_setup_starts_at_model_connection_and_renders_form(client):
    page = client.get("/ui/setup")
    assert page.status_code == 200
    assert "连接模型" in page.text and "第一步" in page.text
    assert 'action="/ui/settings/llm"' in page.text
    assert 'name="consent"' in page.text
    assert "data-test-llm" in page.text
    assert "校准" not in page.text and "试运行" not in page.text


def test_material_import_form_targets_are_form_scoped(client):
    """UXR-01：文件导入的目标/标记按「同表单字段名」解析，状态栏限定当前表单。

    setup.js 在 input.closest("form") 内按字段名查找导入目标与 from_file 标记，
    因此 data-import-target / data-file-marker 的值必须是同表单内真实存在的字段名，
    且状态栏以 data-import-status 标记、每份表单恰好一个。
    """
    from html.parser import HTMLParser

    page = client.get("/ui/setup")

    class Forms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parsed: list[dict] = []
            self.current = None

        def handle_starttag(self, tag, attrs):
            items = dict(attrs)
            if tag == "form":
                self.current = {
                    "action": items.get("action", ""),
                    "names": set(),
                    "import_target": None,
                    "file_marker": None,
                    "status_count": 0,
                }
                return
            if self.current is None:
                return
            if items.get("name"):
                self.current["names"].add(items["name"])
            if "data-import-target" in items:
                self.current["import_target"] = items["data-import-target"]
            if "data-file-marker" in items:
                self.current["file_marker"] = items["data-file-marker"]
            if "data-import-status" in items:
                self.current["status_count"] += 1

        def handle_endtag(self, tag):
            if tag == "form" and self.current is not None:
                self.parsed.append(self.current)
                self.current = None

    scanner = Forms()
    scanner.feed(page.text)
    forms = [f for f in scanner.parsed if f["action"] == "/ui/setup/materials"]
    assert len(forms) == 1, "添加材料表单应在首次设置页出现一次"
    form = forms[0]
    assert form["import_target"] in form["names"], "data-import-target 必须指向同表单内的字段名"
    assert form["file_marker"] in form["names"], "data-file-marker 必须指向同表单内的字段名"
    assert form["status_count"] == 1, "导入状态栏应存在且限定在当前表单内"


def test_full_wizard_generate_feedback_confirm_and_today(client, migrated_engine, monkeypatch):
    calls = extraction_provider(monkeypatch)
    assert (
        post(
            client,
            "/ui/settings/llm",
            {
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-test-1234",
                "model": "test-model",
                "timeout_s": "45",
                "consent": "yes",
                "next": "/ui/setup",
            },
        ).status_code
        == 200
    )
    page = client.get("/ui/setup")
    assert "模型已连接" in page.text and "研究材料" in page.text

    add_material(client)
    generated = post(client, "/ui/setup/generate", {"run_id": "ext-first"})
    assert generated.status_code == 200, generated.text
    assert wait_run(migrated_engine, "ext-first").status is RunStatus.SUCCEEDED
    assert calls == [0]

    review = client.get("/ui/setup")
    assert "渲染视图" in review.text and "Markdown 源码" in review.text
    assert "核心问题" in review.text and "长文档检索" in review.text
    assert "研究方向档案" in review.text and "分类标签" in review.text
    drafts = client.get("/onboarding/drafts").json()
    assert drafts["profile"]["version"] == "0.1"
    assert drafts["ready_to_confirm"] is True

    changed = json.loads(json.dumps(EXTRACTION))
    changed["profile"]["positioning"]["summary"] = "修改后的定位：更重视评测与可复现性。"
    extraction_provider(monkeypatch, [changed])
    feedback = post(client, "/ui/setup/feedback", {"run_id": "ext-second", "comment": "更重视评测"})
    assert feedback.status_code == 200, feedback.text
    assert wait_run(migrated_engine, "ext-second").status is RunStatus.SUCCEEDED
    updated = client.get("/ui/setup")
    assert "与上一版的差异" in updated.text
    assert "修改后的定位" in updated.text
    drafts = client.get("/onboarding/drafts").json()
    assert drafts["profile"]["version"] == "0.2"
    profile, taxonomy = drafts["profile"], drafts["taxonomy"]

    confirmed = post(
        client,
        "/ui/setup/confirm",
        {
            "confirmed": "yes",
            "confirmed_by": "researcher",
            "change_summary": "审阅通过",
            "expected_revision": "1",
            "profile_id": profile["id"],
            "profile_version": profile["version"],
            "profile_hash": profile["content_hash"],
            "taxonomy_id": taxonomy["id"],
            "taxonomy_version": taxonomy["version"],
            "taxonomy_hash": taxonomy["content_hash"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert client.get("/onboarding").json() == {"stage": "active", "revision": 2}
    assert "首次设置已完成" in client.get("/ui/setup").text
    assert client.get("/ui/today").status_code == 200


def test_confirmation_rejects_stale_hash_and_unchecked_box(client, migrated_engine, monkeypatch):
    extraction_provider(monkeypatch)
    configure(migrated_engine)
    add_material(client)
    assert post(client, "/ui/setup/generate", {"run_id": "ext-confirm"}).status_code == 200
    wait_run(migrated_engine, "ext-confirm")
    drafts = client.get("/onboarding/drafts").json()
    profile, taxonomy = drafts["profile"], drafts["taxonomy"]
    base = {
        "confirmed_by": "researcher",
        "change_summary": "审阅通过",
        "expected_revision": "1",
        "profile_id": profile["id"],
        "profile_version": profile["version"],
        "profile_hash": profile["content_hash"],
        "taxonomy_id": taxonomy["id"],
        "taxonomy_version": taxonomy["version"],
        "taxonomy_hash": taxonomy["content_hash"],
    }
    assert post(client, "/ui/setup/confirm", base).status_code == 400
    stale = {**base, "confirmed": "yes", "profile_hash": "0" * 64}
    assert post(client, "/ui/setup/confirm", stale).status_code == 409
    assert client.get("/onboarding").json()["stage"] == "draft"


def test_generation_failure_surfaces_schema_error(client, migrated_engine, monkeypatch):
    from papersteward.llm.provider import FakeLLM

    monkeypatch.setattr(
        "papersteward.onboarding.intake.build_llm_provider",
        lambda _config: FakeLLM(responses=("not json", "still not json")),
    )
    configure(migrated_engine)
    add_material(client)
    assert post(client, "/ui/setup/generate", {"run_id": "ext-bad"}).status_code == 200
    report = wait_run(migrated_engine, "ext-bad")
    assert report.status is RunStatus.FAILED
    assert "模型返回不符合要求" in str(report.errors_json)
    page = client.get("/ui/runs/ext-bad")
    assert "模型返回不符合要求" in page.text
    # extract 失败的恢复路径唯一化：不做原地重试，回设置页调整后重新生成。
    assert "返回首次设置" in page.text
    assert "显式重试" not in page.text


def test_cross_site_large_and_unconfirmed_input(client, migrated_engine):
    assert (
        client.post(
            "/ui/setup/materials",
            data={"content": "x"},
            headers={"origin": "https://evil.example"},
        ).status_code
        == 409
    )
    assert client.get("/onboarding").json()["stage"] == "setup"
    configure(migrated_engine)
    assert (
        post(client, "/ui/setup/materials", {"content": "x" * (512 * 1024 + 1)}).status_code == 400
    )
    add_material(client)
    assert client.get("/onboarding").json()["stage"] == "setup"


def test_legacy_drafts_can_be_imported_as_materials(client, migrated_engine):
    from dataclasses import replace

    from papersteward.core.enums import DocumentStatus as Status
    from papersteward.documents import parse_profile, parse_taxonomy

    profile = replace(
        parse_profile((FIXTURES / "profiles/basic.md").read_text()),
        status=Status.DRAFT,
    )
    from datetime import UTC, datetime

    taxonomy = replace(
        parse_taxonomy((FIXTURES / "taxonomies/onboarding.md").read_text()),
        status=Status.CONFIRMED,
        confirmed_at=datetime(2026, 10, 21, tzinfo=UTC),
    )
    from papersteward.storage.repositories import ProfileRepository, TaxonomyRepository

    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        ProfileRepository(session).add(profile)
        TaxonomyRepository(session).add(taxonomy)
    page = client.get("/ui/setup")
    assert "从旧向导导入遗留草稿" in page.text
    assert post(client, "/ui/setup/import-legacy", {}).status_code == 200
    materials = client.get("/onboarding/intake/materials").json()
    assert len(materials) == 2
    assert all("旧向导草稿" in item["name"] for item in materials)
    assert "从旧向导导入遗留草稿" not in client.get("/ui/setup").text


def test_rendered_preview_escapes_material_derived_html(client, migrated_engine, monkeypatch):
    extraction = json.loads(json.dumps(EXTRACTION))
    extraction["profile"]["positioning"]["summary"] = "<script>alert(1)</script>"
    extraction_provider(monkeypatch, [extraction])
    configure(migrated_engine)
    add_material(client)
    assert post(client, "/ui/setup/generate", {"run_id": "ext-escape"}).status_code == 200
    wait_run(migrated_engine, "ext-escape")
    page = client.get("/ui/setup")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
