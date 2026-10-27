"""模型输出的服务端证据验证：来源引用与逐字引文核对（纯函数）。

schema 校验只证明形状合法；「已存在的编号」不证明语义成立——这里把每条
候选的来源引用对回本次真正发送的材料：编号必须在范围内、直接引文必须
逐字（空白归一化后）来自所引材料。被拒候选逐条剔除并给出原因码，不整篇
作废；有效候选保留待用户核对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from papersteward.schemas import ReadingReflectionOutput, ReflectionCandidateOutput

__all__ = [
    "MaterialEntry",
    "verify_candidates",
]

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class MaterialEntry:
    """一次发送中编号为 ``index`` 的材料（内容文本与清单锚点）。"""

    index: int
    kind: str
    target_id: str
    version_key: str
    content_text: str
    # 专题综合按论文归并独立来源；单篇反思不填（None 表示按 target 计）。
    paper_id: str | None = None


def verify_candidates(
    output: ReadingReflectionOutput, entries: list[MaterialEntry], *, paper_id: str
) -> tuple[list[ReflectionCandidateOutput], list[str]]:
    """核对输出候选的来源引用；返回 (有效候选, 拒绝原因码列表)。

    拒绝原因码：``paper_mismatch``（输出 paper_id 与请求不符，整批拒绝）、
    ``source_out_of_range``、``quote_without_source``、``quote_mismatch``、
    ``quote_source_missing``。拒绝不携带任何原文。
    """
    if output.paper_id != paper_id:
        return [], ["paper_mismatch"] * max(1, len(output.candidates))
    by_index = {entry.index: entry for entry in entries}
    valid: list[ReflectionCandidateOutput] = []
    rejected: list[str] = []
    for candidate in output.candidates:
        code = _reject_reason(candidate, by_index)
        if code is None:
            valid.append(candidate)
        else:
            rejected.append(code)
    return valid, rejected


def _reject_reason(
    candidate: ReflectionCandidateOutput, by_index: dict[int, MaterialEntry]
) -> str | None:
    if not candidate.source_indexes or any(
        index not in by_index for index in candidate.source_indexes
    ):
        return "source_out_of_range"
    if candidate.quote is not None:
        if candidate.quote_source_index is None:
            return "quote_without_source"
        entry = by_index.get(candidate.quote_source_index)
        if entry is None:
            return "source_out_of_range"
        if _normalize(candidate.quote) not in _normalize(entry.content_text):
            return "quote_mismatch"
    elif candidate.quote_source_index is not None:
        return "quote_source_missing"
    return None


def _normalize(text: str) -> str:
    """空白归一化：引文跨行/多余空白不视为伪造，其余逐字比对。"""
    return _WHITESPACE.sub(" ", text).strip()
