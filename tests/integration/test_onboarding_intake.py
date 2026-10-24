"""新引导 API：材料摄取、Agent 草案生成、反馈迭代与确认。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from papersteward.config import get_settings
from papersteward.core.enums import RunStatus
from papersteward.llm.runtime import LLMSettingsPayload, save_llm_config
from papersteward.onboarding.intake import MAX_TOTAL_CHARS, materials_text
from papersteward.storage import create_session_factory, session_scope
from papersteward.storage.repositories import RunRepository

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EXTRACTION = (FIXTURES / "llm/profile_extraction.valid.json").read_text(encoding="utf-8")
HEADERS = {"origin": "http://testserver"}


def configure(engine: Engine) -> None:
    with session_scope(create_session_factory(engine)) as session:
        save_llm_config(
            session,
            LLMSettingsPayload(
                base_url="https://api.example.com/v1",
                api_key="sk-test",
                model="test-model",
                consent=True,
            ),
            get_settings(),
        )


def scripted(monkeypatch, responses: list[str]) -> list[str]:
    from papersteward.llm.provider import FakeLLM

    prompts: list[str] = []

    def build(_config):
        text = responses[min(len(prompts), len(responses) - 1)]
        provider = FakeLLM(responses=(text,))

        return _RecordingProvider(provider, prompts)

    monkeypatch.setattr("papersteward.onboarding.intake.build_llm_provider", build)
    return prompts


class _RecordingProvider:
    """记录 prompt 后委托给 FakeLLM；close 为可选接口。"""

    def __init__(self, provider, prompts: list[str]):
        self._provider = provider
        self._prompts = prompts

    @property
    def model_id(self) -> str:
        return self._provider.model_id

    def complete(self, prompt, *, timeout_s):
        self._prompts.append(prompt)
        return self._provider.complete(prompt, timeout_s=timeout_s)


def wait(client: TestClient, engine: Engine, identity: str):
    factory = create_session_factory(engine)
    for _ in range(600):
        with session_scope(factory) as session:
            report = RunRepository(session).get(identity)
        if report is not None and report.status is not RunStatus.RUNNING:
            return report
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def add(client: TestClient, content: str, name: str = "材料") -> dict:
    response = client.post(
        "/onboarding/intake/materials",
        json={"name": name, "content": content},
        headers=HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_api_material_crud_and_generation_requires_model(client: TestClient) -> None:
    assert client.get("/onboarding").json() == {"stage": "setup", "revision": 0}
    material = add(client, "研究长文档检索与 RAG 的评测。")
    assert client.get("/onboarding/intake/materials").json()[0]["id"] == material["id"]
    duplicate = add(client, "研究长文档检索与 RAG 的评测。", "重复内容")
    assert duplicate["id"] == material["id"]
    response = client.post("/onboarding/intake/generate", json={}, headers=HEADERS)
    assert response.status_code == 400
    assert "设置" in response.json()["error"]["message"]
    assert (
        client.delete(f"/onboarding/intake/materials/{material['id']}", headers=HEADERS).status_code
        == 200
    )
    assert client.get("/onboarding/intake/materials").json() == []


def test_api_generate_feedback_confirm_and_prompt_context(
    client: TestClient, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed = json.loads(EXTRACTION)
    changed["profile"]["positioning"]["summary"] = "第二版：把评测与可复现性放在中心。"
    prompts = scripted(monkeypatch, [EXTRACTION, json.dumps(changed, ensure_ascii=False)])
    configure(migrated_engine)
    add(client, "我研究长文档检索、RAG 与忠实性评测。")

    generated = client.post(
        "/onboarding/intake/generate", json={"run_id": "ext-api-1"}, headers=HEADERS
    )
    assert generated.status_code == 202, generated.text
    assert generated.json()["run_id"] == "ext-api-1"
    assert wait(client, migrated_engine, "ext-api-1").status is RunStatus.SUCCEEDED
    assert "不可信数据" in prompts[0]
    assert "长文档检索" in prompts[0]

    drafts = client.get("/onboarding/drafts").json()
    assert drafts["profile"]["version"] == "0.1"
    assert drafts["taxonomy"]["version"] == "0.1"
    assert drafts["ready_to_confirm"] is True

    feedback = client.post(
        "/onboarding/intake/feedback",
        json={"comment": "更强调评测，并把定位写清楚"},
        headers=HEADERS,
    )
    assert feedback.status_code == 202, feedback.text
    run_id = feedback.json()["run_id"]
    assert wait(client, migrated_engine, run_id).status is RunStatus.SUCCEEDED
    assert "更强调评测" in prompts[1]
    drafts = client.get("/onboarding/drafts").json()
    assert drafts["profile"]["version"] == "0.2"
    assert "第二版" in drafts["profile"]["content_md"]
    assert "与上一版" not in drafts["profile"]["content_md"]
    assert drafts["profile"]["markdown_diff"]

    profile, taxonomy = drafts["profile"], drafts["taxonomy"]
    confirmed = client.post(
        "/onboarding/confirm",
        json={
            "confirmed": True,
            "confirmed_by": "researcher",
            "change_summary": "已审阅",
            "expected_revision": 1,
            "profile_id": profile["id"],
            "profile_version": profile["version"],
            "profile_hash": profile["content_hash"],
            "taxonomy_id": taxonomy["id"],
            "taxonomy_version": taxonomy["version"],
            "taxonomy_hash": taxonomy["content_hash"],
        },
        headers=HEADERS,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"stage": "active", "revision": 2}
    assert client.get("/onboarding").json()["stage"] == "active"
    assert (
        client.post(
            "/onboarding/intake/materials",
            json={"name": "x", "content": "初始化后不应再修改"},
            headers=HEADERS,
        ).status_code
        == 409
    )


def test_feedback_requires_draft(client: TestClient) -> None:
    response = client.post(
        "/onboarding/intake/feedback", json={"comment": "改一下"}, headers=HEADERS
    )
    assert response.status_code == 409
    assert "草案" in response.json()["error"]["message"]


def test_material_limits_and_merged_text_truncation(
    client: TestClient, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("papersteward.onboarding.intake.MAX_MATERIALS", 2)
    add(client, "第一份材料")
    add(client, "第二份材料")
    response = client.post(
        "/onboarding/intake/materials",
        json={"name": "第三份", "content": "第三份材料"},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "最多保留 2 份" in response.json()["error"]["message"]

    factory = create_session_factory(migrated_engine)
    with session_scope(factory) as session:
        from papersteward.core.entities import IntakeMaterial
        from papersteward.storage.repositories import IntakeMaterialRepository

        IntakeMaterialRepository(session).add(
            IntakeMaterial(
                id="mat-large-1",
                name="长材料一",
                kind="text",
                content="a" * (MAX_TOTAL_CHARS // 2 + 10),
                sha256="1" * 64,
            )
        )
        IntakeMaterialRepository(session).add(
            IntakeMaterial(
                id="mat-large-2",
                name="长材料二",
                kind="text",
                content="b" * (MAX_TOTAL_CHARS // 2 + 10),
                sha256="2" * 64,
            )
        )
    with session_scope(factory) as session:
        merged = materials_text(session)
    assert len(merged) <= MAX_TOTAL_CHARS + 200
    assert "已截断" in merged


def test_generation_requires_material(client: TestClient, migrated_engine: Engine) -> None:
    configure(migrated_engine)
    response = client.post("/onboarding/intake/generate", json={}, headers=HEADERS)
    assert response.status_code == 400
    assert "材料" in response.json()["error"]["message"]
