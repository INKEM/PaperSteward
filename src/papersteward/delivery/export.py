"""导出冻结内容及反馈修订；不从当前Paper补写历史元数据。"""

import csv
import hashlib
import io
import json
from collections.abc import Sequence
from typing import Literal

from papersteward.delivery.contracts import ArchiveRecord
from papersteward.delivery.render import markdown, safe_url

ExportFormat = Literal["bibtex", "csv", "markdown", "json"]


def _bib(value: object) -> str:
    mapping = {
        "\\": r"{\textbackslash}",
        "{": r"\{",
        "}": r"\}",
        "&": r"\&",
        "%": r"\%",
        "_": r"\_",
        "#": r"\#",
        "$": r"\$",
    }
    return "".join(mapping.get(c, c) for c in " ".join(str(value).split()))


def _cell(value: object) -> str:
    result = "" if value is None else str(value)
    return "'" + result if result.lstrip().startswith(("=", "+", "-", "@")) else result


def export_archives(records: Sequence[ArchiveRecord], format: ExportFormat) -> str:
    if format == "json":
        return (
            json.dumps([r.model_dump(mode="json") for r in records], ensure_ascii=False, indent=2)
            + "\n"
        )
    if format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "paper_id",
                "title",
                "authors",
                "venue",
                "year",
                "doi",
                "url",
                "pdf_url",
                "code_url",
                "primary_tag",
                "secondary_tags",
                "score",
                "note",
                "initial_score",
                "initial_note",
                "revision_history",
            ]
        )
        for r in records:
            m = r.content.get("metadata", {})
            writer.writerow(
                [
                    _cell(v)
                    for v in [
                        r.paper_id,
                        m.get("title", "历史元数据不可用"),
                        "; ".join(m.get("authors", [])),
                        m.get("venue"),
                        str(m.get("published_at") or "")[:4],
                        m.get("doi"),
                        m.get("url"),
                        m.get("pdf_url"),
                        m.get("code_url"),
                        r.primary_tag,
                        "; ".join(r.secondary_tags),
                        r.score,
                        r.note,
                        r.initial_score,
                        r.initial_note,
                        json.dumps(r.revision_history, ensure_ascii=False),
                    ]
                ]
            )
        return stream.getvalue()
    parts = []
    for r in records:
        m = r.content.get("metadata", {})
        tags = [r.primary_tag, *r.secondary_tags]
        if format == "bibtex":
            fields = {
                "title": m.get("title", "历史元数据不可用"),
                "author": " and ".join(m.get("authors", [])),
                "journal": m.get("venue"),
                "year": str(m.get("published_at") or "")[:4],
                "doi": m.get("doi"),
                "url": m.get("url"),
                "pdf": m.get("pdf_url"),
                "code": m.get("code_url"),
                "keywords": ", ".join(tags),
                "rating": r.score,
                "note": f"当前备注: {r.note or ''}; 初始评分: {r.initial_score}; "
                f"初始备注: {r.initial_note or ''}",
                "annote": json.dumps(r.revision_history, ensure_ascii=False),
            }
            key = "ps" + hashlib.sha256(r.paper_id.encode()).hexdigest()[:12]
            parts.append(
                "@article{"
                + key
                + ",\n"
                + ",\n".join(
                    f"  {k} = {{{_bib(v)}}}" for k, v in fields.items() if v is not None and v != ""
                )
                + "\n}\n"
            )
        else:
            lines = [
                f"## {markdown(m.get('title', '历史元数据不可用'))}",
                "",
                f"标签：{markdown(' / '.join(tags))}",
                f"评分：{r.score}；初始评分：{r.initial_score}",
                f"备注：{markdown(r.note or '')}",
                f"初始备注：{markdown(r.initial_note or '')}",
                f"作者：{markdown(', '.join(m.get('authors', [])))} · "
                f"{markdown(m.get('venue') or '')}",
            ]
            for label, field in (("原文", "url"), ("PDF", "pdf_url"), ("代码", "code_url")):
                url = safe_url(m.get(field))
                if url:
                    lines.append(f"[{label}]({url})")
            lines.extend(
                [
                    "修订历史：",
                    *[
                        markdown(json.dumps(revision, ensure_ascii=False))
                        for revision in r.revision_history
                    ],
                    "",
                ]
            )
            parts.append("\n".join(lines))
    return "\n".join(parts)
