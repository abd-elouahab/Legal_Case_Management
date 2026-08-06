"""Unit tests for :class:`~services.indexing_access.IndexingAccessPolicy`.

The policy owns no rules of its own — it delegates to the document policy, which
delegates to the case policy. These tests assert exactly that: **an index can
never be more visible than the extracted text it was built from, which can never
be more visible than the file that text was read from**. That is the spec's
"reuse existing authorization" and "future search results must inherit document
permissions" stated as an invariant rather than as a convention.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.exceptions import IndexAccessDeniedError
from models.user import UserRole
from services.document_access import DocumentAccessPolicy
from services.indexing_access import IndexingAccessPolicy
from services.ocr_access import OcrAccessPolicy

MakeUser = Any
MakeCase = Any
MakeDocument = Any
MakeIndex = Any


@pytest.fixture
def policy() -> IndexingAccessPolicy:
    return IndexingAccessPolicy()


@pytest.fixture
def administrator(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="index-admin@example.com", role=UserRole.ADMINISTRATOR)


@pytest.fixture
def assigned_lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="index-assigned@example.com", role=UserRole.LAWYER)


@pytest.fixture
def other_lawyer(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="index-other@example.com", role=UserRole.LAWYER)


@pytest.fixture
def court(make_user: MakeUser):  # type: ignore[no-untyped-def]
    return make_user(email="index-court@example.com", role=UserRole.COURT_REPRESENTATIVE)


@pytest.fixture
def legal_case(make_case: MakeCase, assigned_lawyer, court):  # type: ignore[no-untyped-def]
    return make_case(
        assigned_lawyer_id=assigned_lawyer.id,
        assigned_court_representative_id=court.id,
    )


@pytest.fixture
def document(make_document: MakeDocument, legal_case):  # type: ignore[no-untyped-def]
    return make_document(case_id=legal_case.id)


@pytest.fixture
def index(make_document_index: MakeIndex, document, legal_case):  # type: ignore[no-untyped-def]
    return make_document_index(document_id=document.id, case_id=legal_case.id)


class TestDelegation:
    def test_the_verdict_is_the_document_policy_s_for_every_role(
        self,
        policy: IndexingAccessPolicy,
        document,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        # Asserted as the identity it is: this policy has no opinion of its own,
        # so a divergence for *any* role would mean it had grown one.
        documents = DocumentAccessPolicy()
        for user in (administrator, assigned_lawyer, other_lawyer, court):
            assert policy.can_view_document(user, document) == documents.can_view(
                user, document
            ), user.role

    def test_it_agrees_with_the_ocr_policy_for_every_role(
        self,
        policy: IndexingAccessPolicy,
        document,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        # The chain has three links now — index → document → case, and text →
        # document → case — and they must arrive at the same answer, or an index
        # would be readable by someone who cannot read the text it was built
        # from.
        ocr = OcrAccessPolicy()
        for user in (administrator, assigned_lawyer, other_lawyer, court):
            assert policy.can_view_document(user, document) == ocr.can_view_document(
                user, document
            ), user.role

    def test_the_visibility_scope_is_the_document_policy_s(
        self,
        policy: IndexingAccessPolicy,
        administrator,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        documents = DocumentAccessPolicy()
        for user in (administrator, assigned_lawyer):
            assert policy.visibility_scope(user) == documents.visibility_scope(user)


class TestDecisions:
    def test_an_administrator_reaches_any_index(
        self,
        policy: IndexingAccessPolicy,
        index,  # type: ignore[no-untyped-def]
        administrator,  # type: ignore[no-untyped-def]
    ) -> None:
        assert policy.can_view(administrator, index)
        policy.require_view(administrator, index)

    def test_the_assigned_lawyer_reaches_the_index(
        self,
        policy: IndexingAccessPolicy,
        index,  # type: ignore[no-untyped-def]
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        assert policy.can_view(assigned_lawyer, index)

    def test_the_assigned_court_representative_reaches_the_index(
        self,
        policy: IndexingAccessPolicy,
        index,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        # Reading *whether* a document is searchable is part of viewing it. Only
        # rebuilding is withheld, and that is a permission rather than a scope.
        assert policy.can_view(court, index)

    def test_an_unassigned_lawyer_is_refused(
        self,
        policy: IndexingAccessPolicy,
        index,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        assert not policy.can_view(other_lawyer, index)
        with pytest.raises(IndexAccessDeniedError):
            policy.require_view(other_lawyer, index)

    def test_an_unassigned_lawyer_is_refused_the_document_too(
        self,
        policy: IndexingAccessPolicy,
        document,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(IndexAccessDeniedError):
            policy.require_document_access(other_lawyer, document)


class TestDenialBody:
    def test_the_denial_names_neither_permission_nor_role(
        self,
        policy: IndexingAccessPolicy,
        index,  # type: ignore[no-untyped-def]
        other_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        # The log carries the specifics, correlatable with the response's
        # request_id; the body says only that access was refused.
        with pytest.raises(IndexAccessDeniedError) as failure:
            policy.require_view(other_lawyer, index)

        rendered = str(failure.value).lower()
        assert "indexing:" not in rendered
        assert "lawyer" not in rendered
        assert str(index.id) not in rendered

    def test_it_answers_403_rather_than_a_concealing_404(
        self, policy: IndexingAccessPolicy
    ) -> None:
        from fastapi import status

        assert IndexAccessDeniedError.status_code == status.HTTP_403_FORBIDDEN


class TestScope:
    def test_a_privileged_caller_is_unrestricted(
        self,
        policy: IndexingAccessPolicy,
        administrator,  # type: ignore[no-untyped-def]
    ) -> None:
        assert policy.visibility_scope(administrator) is None

    def test_everyone_else_is_scoped_to_themselves(
        self,
        policy: IndexingAccessPolicy,
        assigned_lawyer,  # type: ignore[no-untyped-def]
        court,  # type: ignore[no-untyped-def]
    ) -> None:
        # Returned rather than applied, so the restriction can be pushed into the
        # SQL query and page totals count only what the caller may access.
        assert policy.visibility_scope(assigned_lawyer) == assigned_lawyer.id
        assert policy.visibility_scope(court) == court.id

    def test_the_scope_is_a_user_id_not_a_case_list(
        self,
        policy: IndexingAccessPolicy,
        assigned_lawyer,  # type: ignore[no-untyped-def]
    ) -> None:
        scope = policy.visibility_scope(assigned_lawyer)
        assert scope is None or isinstance(scope, uuid.UUID)
