"""Unit tests for :class:`~services.search_access.SearchAccessPolicy`.

The policy owns no rules of its own — it delegates to the document policy, which
delegates to the case policy. These tests assert exactly that: **a search result
can never be more visible than the index it came from, which can never be more
visible than the extracted text, which can never be more visible than the file.**

That is the spec's *"reuse the existing authorization system"* stated as an
invariant rather than as a convention, and it is asserted as an **identity** —
this policy's verdict is compared with the document, OCR, and indexing policies'
for every role, so the four cannot drift apart without a test failing.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.exceptions import SearchAccessDeniedError
from models.user import UserRole
from services.document_access import DocumentAccessPolicy
from services.indexing_access import IndexingAccessPolicy
from services.ocr_access import OcrAccessPolicy
from services.search_access import SearchAccessPolicy

MakeUser = Any
MakeCase = Any
MakeDocument = Any


@pytest.fixture
def policy() -> SearchAccessPolicy:
    return SearchAccessPolicy()


@pytest.fixture
def administrator(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="search-admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def assigned_lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="search-assigned@example.com", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="search-other@example.com", role=UserRole.LAWYER)


@pytest.fixture
def court(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="search-court@example.com", role=UserRole.COURT_REPRESENTATIVE)


@pytest.fixture
def legal_case(make_case: MakeCase, assigned_lawyer, court):  # type: ignore[no-untyped-def]
    return make_case(
        assigned_lawyer_id=assigned_lawyer.id,
        assigned_court_representative_id=court.id,
    )


@pytest.fixture
def document(make_document: MakeDocument, legal_case):  # type: ignore[no-untyped-def]
    return make_document(case_id=legal_case.id)


class TestDelegation:
    def test_the_verdict_is_the_document_policy_s_for_every_role(
        self,
        policy: SearchAccessPolicy,
        document,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        documents = DocumentAccessPolicy()

        for user in (administrator, assigned_lawyer, other_lawyer, court):
            assert policy.can_search_document(user, document) == documents.can_view(
                user, document
            )

    def test_the_whole_pipeline_agrees_for_every_role(
        self,
        policy: SearchAccessPolicy,
        document,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        """OCR, indexing, and search must reach the same verdict on one document.

        Each is a further derivation of the same file: extracted text, then
        vectors, then a retrieved passage. A divergence anywhere in that chain
        means one stage exposes something an earlier one does not.
        """
        ocr = OcrAccessPolicy()
        indexing = IndexingAccessPolicy()

        for user in (administrator, assigned_lawyer, other_lawyer, court):
            verdict = policy.can_search_document(user, document)
            assert verdict == ocr.can_view_document(user, document)
            assert verdict == indexing.can_view_document(user, document)

    def test_the_scope_is_the_document_policy_s(
        self,
        policy: SearchAccessPolicy,
        administrator,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        documents = DocumentAccessPolicy()

        for user in (administrator, assigned_lawyer, court):
            assert policy.visibility_scope(user) == documents.visibility_scope(user)


class TestScope:
    def test_an_administrator_searches_everything(
        self,
        policy: SearchAccessPolicy,
        administrator,  # type: ignore[no-untyped-def]
    ) -> None:
        assert policy.visibility_scope(administrator) is None
        assert policy.searches_everything(administrator) is True

    def test_a_lawyer_is_scoped_to_their_own_assignments(
        self,
        policy: SearchAccessPolicy,
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        assert policy.visibility_scope(assigned_lawyer) == assigned_lawyer.id
        assert policy.searches_everything(assigned_lawyer) is False

    def test_a_court_representative_is_scoped_too(
        self,
        policy: SearchAccessPolicy,
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        assert policy.visibility_scope(court) == court.id
        assert policy.searches_everything(court) is False


class TestCaseFilter:
    def test_a_party_may_filter_by_their_own_case(
        self,
        policy: SearchAccessPolicy,
        legal_case,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
    ) -> None:
        for user in (assigned_lawyer, court, administrator):
            policy.require_case_access(user, legal_case)

    def test_a_stranger_filtering_by_a_case_is_refused_not_emptied(
        self,
        policy: SearchAccessPolicy,
        legal_case,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """403, never an empty result set.

        An inaccessible case and a case with no matching text must not be
        indistinguishable, or the filter becomes a way to probe for the existence
        of matters the caller is not on.
        """
        with pytest.raises(SearchAccessDeniedError):
            policy.require_case_access(other_lawyer, legal_case)


class TestDocumentFilter:
    def test_a_party_may_filter_by_a_document_on_their_case(
        self,
        policy: SearchAccessPolicy,
        document,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        for user in (assigned_lawyer, court):
            policy.require_document_access(user, document)

    def test_a_stranger_filtering_by_a_document_is_refused(
        self,
        policy: SearchAccessPolicy,
        document,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(SearchAccessDeniedError):
            policy.require_document_access(other_lawyer, document)


class TestDenialShape:
    def test_the_denial_is_a_generic_403(
        self,
        policy: SearchAccessPolicy,
        legal_case,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        """The body names neither a permission nor a role."""
        with pytest.raises(SearchAccessDeniedError) as excinfo:
            policy.require_case_access(other_lawyer, legal_case)

        message = str(excinfo.value).lower()
        assert "search:query" not in message
        assert "lawyer" not in message
        assert excinfo.value.status_code == 403
