import json
from datetime import UTC, datetime
from pathlib import Path

from papersteward.pipeline.dedupe import deduplicate
from papersteward.sources.arxiv import parse_feed
from papersteward.sources.openalex import parse_work
from papersteward.sources.semantic_scholar import parse_paper


def test_three_source_fixtures_merge_without_losing_provenance():
    root = Path(__file__).resolve().parents[1] / "fixtures/sources"
    now = datetime(2026, 10, 22, tzinfo=UTC)
    candidates = parse_feed((root / "arxiv.xml").read_bytes(), now)
    candidates.append(
        parse_work(json.loads((root / "openalex.json").read_text())["results"][0], now)
    )
    candidates.append(
        parse_paper(json.loads((root / "semantic_scholar.json").read_text())["data"][0], now)
    )
    (group,) = deduplicate(candidates)
    assert group.candidate.doi == "10.0000/fixture"
    assert group.candidate.arxiv_id == "2610.00001"
    assert {p.source for p in group.provenances} == {"arxiv", "openalex", "semantic_scholar"}
    assert len(group.reasons) == 2
