"""基础无障碍契约（批次G）：lang、标题层级、skip-link、表单控件可标注。

不替代完整 WCAG 审计；把全站结构性要求固化为回归，防新页面回退。
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
from test_priority_queue import NOW, seed

from conftest import activate
from papersteward.delivery.contracts import Action
from papersteward.reading.actions import apply_action
from papersteward.scoring.queue import refresh_queue
from papersteward.storage import create_session_factory, session_scope


class _Structure(HTMLParser):
    """收集 lang、h1 数量、skip-link、main、以及无标注的表单控件。"""

    def __init__(self) -> None:
        super().__init__()
        self.lang: str | None = None
        self.h1 = 0
        self.skip_link = False
        self.main = False
        self._label_open = False
        self._labelled: set[int] = set()
        self._ids: set[str] = set()
        self._aria_labels: set[int] = set()
        self._current_tag: tuple[str, dict[str, str]] | None = None
        self.unlabelled: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        items = {k: (v or "") for k, v in attrs}
        line = self.getpos()[0]
        if tag == "html" and self.lang is None:
            self.lang = items.get("lang")
        if tag == "h1":
            self.h1 += 1
        if tag == "a" and "skip-link" in items.get("class", ""):
            self.skip_link = True
        if tag == "main" and items.get("id") == "main":
            self.main = True
        if tag == "label":
            self._label_open = True
            self._current_tag = (tag, items)
        for control in ("input", "select", "textarea"):
            if tag == control:
                if items.get("type") in {"hidden", "checkbox"}:
                    continue
                if items.get("id"):
                    self._ids.add(items["id"])
                if items.get("aria-label"):
                    self._aria_labels.add(line)
                if not self._label_open and not items.get("aria-label"):
                    self.unlabelled.append(f"{control}@{line}:{items.get('name', '')}")
        # 表单控件在 label 内则视为已标注
        if self._label_open and tag in {"input", "select", "textarea"}:
            pass

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self._label_open = False


def _parse(html: str) -> _Structure:
    parser = _Structure()
    parser.feed(html)
    return parser


PAGES = (
    "/ui/queue",
    "/ui/views",
    "/ui/shelves",
    "/ui/archive",
    "/ui/import",
    "/ui/help",
    "/ui/setup",
    "/ui/settings",
)


@pytest.mark.parametrize("path", PAGES)
def test_page_structure_contract(client, migrated_engine, path):
    """每页：lang=zh-CN、恰一个 h1、skip-link、main#main。"""
    activate(client)
    page = client.get(path)
    assert page.status_code == 200
    parser = _parse(page.text)
    assert parser.lang == "zh-CN", path
    assert parser.h1 == 1, f"{path}: h1 数量为 {parser.h1}"
    assert parser.skip_link, path
    assert parser.main, path


def test_detail_page_structure_contract(client, migrated_engine):
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "a11y", r=80)
        refresh_queue(session, now=NOW)
    parser = _parse(client.get("/ui/papers/a11y").text)
    assert parser.lang == "zh-CN" and parser.h1 == 1 and parser.main


def test_form_controls_are_labelled(client, migrated_engine):
    """全站核心页面的表单控件都在 label 内或有 aria-label（批次G 抽查）。"""
    activate(client)
    with session_scope(create_session_factory(migrated_engine)) as session:
        seed(session, "a11y", r=80)
        refresh_queue(session, now=NOW)
        apply_action(
            session,
            "a11y",
            Action(kind="done", request_id="a11y-done", mode="skim", score=4, note="ok"),
            now=NOW,
        )
    offenders: list[str] = []
    for path in (*PAGES, "/ui/papers/a11y"):
        page = client.get(path)
        if page.status_code != 200:
            continue
        parser = _parse(page.text)
        if parser.unlabelled:
            offenders.append(f"{path}: {parser.unlabelled}")
    assert not offenders, "无标注表单控件: " + "; ".join(offenders)
