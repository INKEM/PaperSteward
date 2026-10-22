"""P4 富化 provider 契约与确定性降级；信号/持久化实体位于 ``core.entities``。

P4-S2 起持久化由 ``storage`` 与 ``pipeline.enrich_service`` 负责；本模块不访问网络、
不写数据库，缺失字段必须保持中性值。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from papersteward.core.entities import Enrichment, EnrichmentSignals, Paper


@runtime_checkable
class EnrichmentProvider(Protocol):
    """富化 provider：只接收已筛选文献，返回带置信度/来源的中性信号。"""

    @property
    def source(self) -> str: ...

    def enrich(self, paper: Paper) -> EnrichmentSignals:
        """返回信号；无外部证据时返回中性值，失败应抛出可审计错误。"""
        ...


class NeutralEnricher:
    """确定性降级：只复用已在 Paper 中的 OA 状态，不推断任何新信号。"""

    source = "neutral"

    def enrich(self, paper: Paper) -> EnrichmentSignals:
        if not isinstance(paper, Paper):
            raise TypeError("paper 必须是 Paper")
        return EnrichmentSignals(oa_status=paper.oa_status, source=self.source)


__all__ = ["Enrichment", "EnrichmentProvider", "EnrichmentSignals", "NeutralEnricher"]
