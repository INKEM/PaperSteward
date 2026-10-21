from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from papersteward.core.entities import Paper, ResearchProfile, Run
from papersteward.core.enums import DocumentStatus, PaperStatus, RunStatus
from papersteward.core.errors import DomainValidationError, InvalidTransition
from papersteward.core.state_machine import (
    PAPER_STATUS_EVENTS,
    PAPER_TRANSITIONS,
    allowed_paper_transitions,
    can_transition,
    ensure_document_transition,
    ensure_paper_transition,
    ensure_run_transition,
    transition_document,
    transition_paper,
    transition_run,
)

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)

HAPPY_PATH = (
    PaperStatus.DEDUPED,
    PaperStatus.SCREENED,
    PaperStatus.ENRICHED,
    PaperStatus.SUMMARIZED,
    PaperStatus.CLASSIFIED,
    PaperStatus.SCORED,
    PaperStatus.QUEUED,
    PaperStatus.PUSHED,
    PaperStatus.READING,
    PaperStatus.DONE,
    PaperStatus.ARCHIVED,
)


def test_happy_path_moves_through_pipeline() -> None:
    paper = Paper(id="p001", title="RAG")

    for status in HAPPY_PATH:
        paper = transition_paper(paper, status)

    assert paper.status is PaperStatus.ARCHIVED
    assert paper.id == "p001"


def test_needs_review_cannot_reach_queue_or_push() -> None:
    assert not can_transition(PaperStatus.NEEDS_REVIEW, PaperStatus.QUEUED)
    assert not can_transition(PaperStatus.NEEDS_REVIEW, PaperStatus.PUSHED)

    with pytest.raises(InvalidTransition, match="needs_review"):
        ensure_paper_transition(PaperStatus.NEEDS_REVIEW, PaperStatus.QUEUED, paper_id="p001")


def test_done_is_the_only_entry_to_archived_and_archived_is_terminal() -> None:
    assert can_transition(PaperStatus.DONE, PaperStatus.ARCHIVED)
    for status in PaperStatus:
        if status not in {PaperStatus.DONE, PaperStatus.ARCHIVED}:
            assert not can_transition(status, PaperStatus.ARCHIVED)

    assert allowed_paper_transitions(PaperStatus.ARCHIVED) == frozenset()
    with pytest.raises(InvalidTransition):
        ensure_paper_transition(PaperStatus.ARCHIVED, PaperStatus.READING)


def test_rejected_can_be_restored_to_screening() -> None:
    assert can_transition(PaperStatus.REJECTED, PaperStatus.SCREENED)
    assert not can_transition(PaperStatus.REJECTED, PaperStatus.QUEUED)


def test_every_status_has_an_event_and_a_transition_entry() -> None:
    assert set(PAPER_STATUS_EVENTS) == set(PaperStatus)
    assert set(PAPER_TRANSITIONS) == set(PaperStatus)
    for targets in PAPER_TRANSITIONS.values():
        assert targets <= set(PaperStatus)


def test_document_transitions_require_confirmation_time() -> None:
    profile = ResearchProfile(
        id="profile-1",
        version="1.0",
        status=DocumentStatus.DRAFT,
        content_md="# profile",
    )

    with pytest.raises(InvalidTransition, match="必须提供确认时间"):
        transition_document(profile, DocumentStatus.CONFIRMED)

    confirmed = transition_document(profile, DocumentStatus.CONFIRMED, confirmed_at=NOW)
    assert confirmed.status is DocumentStatus.CONFIRMED
    assert confirmed.confirmed_at == NOW
    assert profile.status is DocumentStatus.DRAFT

    archived = transition_document(confirmed, DocumentStatus.ARCHIVED)
    assert archived.status is DocumentStatus.ARCHIVED
    assert archived.confirmed_at == NOW

    with pytest.raises(InvalidTransition):
        ensure_document_transition(DocumentStatus.CONFIRMED, DocumentStatus.DRAFT)


def test_run_transitions_require_finished_at_for_terminal_states() -> None:
    run = Run(id="r1", run_type="digest", started_at=NOW)

    with pytest.raises(DomainValidationError, match="终态运行必须有结束时间"):
        transition_run(run, RunStatus.SUCCEEDED)

    finished = transition_run(run, RunStatus.SUCCEEDED, finished_at=NOW + timedelta(minutes=1))
    assert finished.status is RunStatus.SUCCEEDED
    assert finished.finished_at == NOW + timedelta(minutes=1)

    with pytest.raises(InvalidTransition):
        ensure_run_transition(RunStatus.SUCCEEDED, RunStatus.RUNNING)


def test_shelved_must_rescore_before_rejoining_queue() -> None:
    assert can_transition(PaperStatus.SHELVED, PaperStatus.SCORED)
    assert not can_transition(PaperStatus.SHELVED, PaperStatus.QUEUED)
