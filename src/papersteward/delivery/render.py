"""确定性MD/HTML文本渲染；论文内容始终作为数据。"""

import json
import re
from collections import Counter
from html import escape
from urllib.parse import quote, urlsplit

from papersteward.core.enums import ReadMode
from papersteward.delivery.contracts import Digest, DigestCard


def text(value: object) -> str:
    return " ".join(str(value).split())


def markdown(value: object) -> str:
    value = escape(text(value), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def safe_url(value: str | None) -> str | None:
    if value is None or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
    except ValueError:
        return None
    return quote(value, safe=":/?&=%#@+;,~_-.")


def _mode(card: DigestCard) -> str:
    return "精读" if card.read_mode is ReadMode.DEEP else "粗读"


def _header(digest: Digest) -> str:
    counts = Counter(i.read_mode for i in digest.items)
    return (
        f"队列 {digest.queue_total} 篇 · 精读 {counts[ReadMode.DEEP]} 篇 / "
        f"粗读 {counts[ReadMode.SKIM]} 篇 · 时区 {digest.timezone}"
    )


def _links(card: DigestCard) -> list[tuple[str, str]]:
    return [
        (label, url)
        for label, raw in (("原文", card.url), ("PDF", card.pdf_url), ("代码", card.code_url))
        if (url := safe_url(raw)) is not None
    ]


def render_markdown(digest: Digest) -> str:
    lines = [f"# 今日文献 · {digest.digest_date}", "", markdown(_header(digest)), ""]
    if digest.inventory_mode:
        lines.extend(["清库存模式：优先消化已排队文献。", ""])
    for card in digest.items:
        year = card.published_on.year if card.published_on else "年份未知"
        lines.extend(
            [
                f"## 【{_mode(card)}】{markdown(card.title)}",
                "",
                f"作者：{markdown(', '.join(card.authors[:3]) or '未知')} · "
                f"{markdown(card.venue or 'venue未知')} · {year}",
                f"归属：{markdown(card.filing_bucket or card.primary_tag)}"
                f" · 主标签 {markdown(card.primary_tag)}"
                f" · 副标签 {markdown(' / '.join(card.secondary_tags) or '无')}",
                " / ".join(f"[{label}]({url})" for label, url in _links(card)),
                "",
                f"> {markdown(card.summary_100)}",
                "",
                f"建议：{markdown(card.reason)}",
                f"P={card.priority:.1f} · 排序分={card.rank_score:.1f}",
                "",
                "<details><summary>分项、权重、证据与版本</summary>",
                "",
                "<pre>" + escape(card.explanation.model_dump_json(indent=2)) + "</pre>",
                "</details>",
                "",
            ]
        )
    if not digest.items:
        lines.extend(["今日无可推送条目；不降低质量门槛补量。", ""])
    lines.extend(
        [
            f"特殊桶：{markdown(digest.special_status)}（预留上限 {digest.special_slots}）",
            f"待确认：{digest.pending_review} 篇；延期/冷却/配额未选：{len(digest.deferred)} 篇。",
            "",
        ]
    )
    return "\n".join(lines)


def render_html(digest: Digest) -> str:
    parts = [
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>今日文献 · {digest.digest_date}</title><body><main>",
        f"<h1>今日文献 · {digest.digest_date}</h1><p>{escape(_header(digest))}</p>",
    ]
    if digest.inventory_mode:
        parts.append("<p>清库存模式：优先消化已排队文献。</p>")
    for card in digest.items:
        year = card.published_on.year if card.published_on else "年份未知"
        parts.extend(
            [
                f"<article><h2>【{_mode(card)}】{escape(card.title)}</h2>",
                f"<p>作者：{escape(', '.join(card.authors[:3]) or '未知')} · "
                f"{escape(card.venue or 'venue未知')} · {year}</p>",
                f"<p>归属：{escape(card.filing_bucket or card.primary_tag)}"
                f" · 主标签 {escape(card.primary_tag)}"
                f" · 副标签 {escape(' / '.join(card.secondary_tags) or '无')}</p>",
                "<p>"
                + " / ".join(
                    f'<a rel="noreferrer" href="{escape(url)}">{label}</a>'
                    for label, url in _links(card)
                )
                + "</p>",
                f"<blockquote>{escape(card.summary_100)}</blockquote>",
                f"<p>{escape(card.reason)}</p>"
                f"<p>P={card.priority:.1f} · 排序分={card.rank_score:.1f}</p>",
                "<details><summary>分项、权重、证据与版本</summary><pre>"
                + escape(
                    json.dumps(
                        card.explanation.model_dump(mode="json"), ensure_ascii=False, indent=2
                    )
                )
                + "</pre></details></article>",
            ]
        )
    if not digest.items:
        parts.append("<p>今日无可推送条目；不降低质量门槛补量。</p>")
    parts.append(
        f"<footer><p>特殊桶：{escape(digest.special_status)}（预留上限 {digest.special_slots}）</p>"
        f"<p>待确认：{digest.pending_review} 篇；"
        f"延期/冷却/配额未选：{len(digest.deferred)} 篇。</p>"
        "</footer></main></body></html>"
    )
    return "\n".join(parts) + "\n"
