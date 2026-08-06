"""Unit tests for :mod:`services.vector_store`.

Driven by a stub client rather than a running Qdrant, because what this module
owns is not Qdrant's behaviour but the *translation* around it: the payload
shape, the delete filter, the batching, the collection guard, and the rule that a
driver message — which can quote a payload, and therefore the document's text —
never leaves the boundary. Those are the parts a replacement vector database has
to honour, and none of them needs a server to assert.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from core.indexing import IndexFailureCode
from services.vector_store import (
    CollectionInfo,
    QdrantVectorStore,
    VectorPoint,
    VectorStoreError,
    build_payload,
    get_vector_store,
)


class StubQdrant:
    """A stand-in for the shared Qdrant client."""

    def __init__(self, *, exists: bool = False, dimensions: int = 8) -> None:
        self.exists = exists
        self.dimensions = dimensions
        self.points: dict[str, Any] = {}
        self.deletes: list[Any] = []
        self.upserts: list[list[Any]] = []
        self.created: list[dict[str, Any]] = []
        self.raises: Exception | None = None
        self.reachable = True

    # ----------------------------------------------------------- collection #

    def get_collections(self) -> object:
        if not self.reachable:
            raise RuntimeError("connection refused")
        return object()

    def collection_exists(self, name: str) -> bool:
        if self.raises is not None:
            raise self.raises
        return self.exists

    def create_collection(self, *, collection_name: str, vectors_config: Any) -> None:
        if self.raises is not None:
            raise self.raises
        self.created.append({"name": collection_name, "config": vectors_config})
        self.exists = True

    def get_collection(self, name: str) -> Any:
        class Params:
            size = self.dimensions

        class Vectors:
            vectors = Params()

        class Config:
            params = Vectors()

        class Described:
            config = Config()
            points_count = len(self.points)

        return Described()

    # ---------------------------------------------------------------- write #

    def upsert(self, *, collection_name: str, points: list[Any], wait: bool) -> None:
        if self.raises is not None:
            raise self.raises
        assert wait is True, "a run must not report `indexed` before Qdrant accepted it"
        self.upserts.append(points)
        for point in points:
            self.points[str(point.id)] = point

    def delete(self, *, collection_name: str, points_selector: Any, wait: bool) -> None:
        if self.raises is not None:
            raise self.raises
        self.deletes.append(points_selector)

    def count(self, *, collection_name: str, count_filter: Any, exact: bool) -> Any:
        class Outcome:
            count = len(self.points)

        return Outcome()


@pytest.fixture
def client() -> StubQdrant:
    return StubQdrant()


@pytest.fixture
def store(client: StubQdrant) -> QdrantVectorStore:
    return QdrantVectorStore(collection="test-chunks", client=client)


def make_point(document_id: uuid.UUID, *, version: int = 1, chunk: int = 0) -> VectorPoint:
    return VectorPoint(
        id=uuid.uuid4(),
        vector=[0.1] * 8,
        payload=build_payload(
            document_id=document_id,
            document_version=version,
            case_id=uuid.uuid4(),
            page_number=1,
            chunk_number=chunk,
            text="Le bailleur loue au preneur les locaux.",
            language="fr",
            embedding_model="acme/tiny",
        ),
    )


class TestPayload:
    def test_it_carries_exactly_the_fields_the_spec_lists(self) -> None:
        # "document id, version, case id, page, chunk number, language,
        # timestamps" — plus the text and the model, both justified in the
        # module docstring.
        payload = build_payload(
            document_id=uuid.uuid4(),
            document_version=2,
            case_id=uuid.uuid4(),
            page_number=7,
            chunk_number=13,
            text="passage",
            language="ar",
            embedding_model="acme/tiny",
        )
        assert set(payload) == {
            "document_id",
            "document_version",
            "case_id",
            "page_number",
            "chunk_number",
            "text",
            "language",
            "embedding_model",
            "indexed_at",
        }

    def test_identifiers_are_strings_and_numbers_are_numbers(self) -> None:
        # A payload whose `case_id` is a UUID on one point and a str on another
        # is a filter that silently matches nothing.
        payload = build_payload(
            document_id=uuid.uuid4(),
            document_version=2,
            case_id=uuid.uuid4(),
            page_number=7,
            chunk_number=13,
            text="passage",
            language="ar",
            embedding_model="acme/tiny",
        )
        assert isinstance(payload["document_id"], str)
        assert isinstance(payload["case_id"], str)
        assert isinstance(payload["document_version"], int)
        assert isinstance(payload["page_number"], int)
        assert isinstance(payload["chunk_number"], int)

    def test_the_timestamp_is_iso_8601_in_utc(self) -> None:
        stamp = datetime(2026, 8, 5, 12, 30, tzinfo=UTC)
        payload = build_payload(
            document_id=uuid.uuid4(),
            document_version=1,
            case_id=uuid.uuid4(),
            page_number=1,
            chunk_number=0,
            text="passage",
            language="fr",
            embedding_model="acme/tiny",
            indexed_at=stamp,
        )
        assert payload["indexed_at"] == "2026-08-05T12:30:00+00:00"


class TestCollection:
    def test_a_missing_collection_is_created_with_cosine_distance(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        # Cosine, because the embedder returns unit-length vectors and it is what
        # bge-m3 is trained for.
        store.ensure_collection(dimensions=8)
        assert client.created[0]["name"] == "test-chunks"
        assert client.created[0]["config"].size == 8
        assert client.created[0]["config"].distance.lower() == "cosine"

    def test_an_existing_collection_is_left_exactly_as_it_is(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        # Silently recreating a collection would delete every vector on the
        # platform, which is never the right response to a configuration mistake.
        client.exists = True
        store.ensure_collection(dimensions=8)
        assert client.created == []

    def test_ensuring_twice_costs_one_check(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        store.ensure_collection(dimensions=8)
        client.raises = RuntimeError("must not be called again")
        store.ensure_collection(dimensions=8)  # cached, so it does not touch the client

    def test_a_width_mismatch_is_reported_rather_than_repaired(
        self, client: StubQdrant
    ) -> None:
        # The fix is a re-index of the whole platform under the new model, which
        # is an operator's decision and not one an indexing run may make.
        client.exists = True
        client.dimensions = 1024
        store = QdrantVectorStore(collection="test-chunks", client=client)

        with pytest.raises(VectorStoreError) as failure:
            store.ensure_collection(dimensions=8)
        assert "1024" in str(failure.value)

    def test_an_unreachable_database_fails_with_a_machine_readable_code(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        client.raises = RuntimeError("connection refused")
        with pytest.raises(VectorStoreError) as failure:
            store.ensure_collection(dimensions=8)
        assert failure.value.code is IndexFailureCode.VECTOR_STORE_UNAVAILABLE

    def test_a_concurrent_creation_is_success_not_failure(
        self, client: StubQdrant
    ) -> None:
        # Two runs can reach `create_collection` at once; the loser must not
        # report a failure for a collection that now exists.
        class RacingQdrant(StubQdrant):
            def __init__(self) -> None:
                super().__init__()
                self.checks = 0

            def collection_exists(self, name: str) -> bool:
                self.checks += 1
                # Absent on the first check, present by the time the create fails.
                return self.checks > 1

            def create_collection(self, **kwargs: Any) -> None:
                raise RuntimeError("collection already exists")

        racing = RacingQdrant()
        store = QdrantVectorStore(collection="test-chunks", client=racing)
        store.ensure_collection(dimensions=8)  # does not raise


class TestUpsert:
    def test_nothing_to_write_costs_no_request(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        assert store.upsert([]) == 0
        assert client.upserts == []

    def test_points_are_written_and_counted(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        document_id = uuid.uuid4()
        points = [make_point(document_id, chunk=index) for index in range(3)]
        assert store.upsert(points) == 3
        assert len(client.points) == 3

    def test_writing_the_same_id_twice_replaces_rather_than_duplicates(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        # The whole of "avoid duplicate vectors": Qdrant upserts by point id, and
        # the id is derived from the chunk's position.
        document_id = uuid.uuid4()
        point = make_point(document_id)
        store.upsert([point])
        store.upsert([point])
        assert len(client.points) == 1

    def test_large_writes_are_batched(
        self, store: QdrantVectorStore, client: StubQdrant, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The spec's "batch insertion": one request per vector pays a round trip
        # each; one request for a 900-page bundle can time out.
        from core.config import settings

        monkeypatch.setattr(settings, "QDRANT_UPSERT_BATCH_SIZE", 2)

        document_id = uuid.uuid4()
        points = [make_point(document_id, chunk=index) for index in range(5)]
        assert store.upsert(points) == 5
        assert [len(batch) for batch in client.upserts] == [2, 2, 1]

    def test_a_write_failure_does_not_leak_the_driver_message(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        # The exception's text can include the payload it was writing, and the
        # payload holds the document's text.
        client.raises = RuntimeError("rejected payload {'text': 'Contrat de bail'}")
        with pytest.raises(VectorStoreError) as failure:
            store.upsert([make_point(uuid.uuid4())])
        assert "Contrat de bail" not in str(failure.value)


class TestFilterShapes:
    """The two calls want the filter in *different* shapes.

    ``delete`` takes a ``FilterSelector``; ``count`` takes the bare ``Filter``.
    A stub client accepts either, so this is asserted against the driver's own
    models rather than against the stub — the shape mismatch shipped once and was
    caught only by a live Qdrant, where ``count`` raised a pydantic
    ``ValidationError`` while every stub-driven test passed.
    """

    def test_delete_passes_a_filter_selector(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        from qdrant_client.models import FilterSelector

        client.exists = True
        store.delete_document_version(uuid.uuid4(), 1)
        assert isinstance(client.deletes[0], FilterSelector)

    def test_count_passes_a_bare_filter(self, client: StubQdrant) -> None:
        from qdrant_client.models import Filter

        captured: list[Any] = []

        class Recording(StubQdrant):
            def count(self, *, collection_name: str, count_filter: Any, exact: bool) -> Any:
                captured.append(count_filter)
                return super().count(
                    collection_name=collection_name, count_filter=count_filter, exact=exact
                )

        recording = Recording(exists=True)
        store = QdrantVectorStore(collection="test-chunks", client=recording)
        store.count_document_version(uuid.uuid4(), 1)

        assert isinstance(captured[0], Filter)


class TestDelete:
    def test_the_filter_names_both_the_document_and_the_version(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        # A replacement's vectors and its predecessor's live side by side in one
        # collection; deleting by document alone would take out the version that
        # is still current.
        client.exists = True
        document_id = uuid.uuid4()
        store.delete_document_version(document_id, 2)

        conditions = client.deletes[0].filter.must
        keys = {condition.key for condition in conditions}
        assert keys == {"document_id", "document_version"}
        values = {condition.match.value for condition in conditions}
        assert values == {str(document_id), 2}

    def test_a_missing_collection_is_not_an_error(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        # A first index has no predecessor, and the delete-then-write order makes
        # this the normal path for every new document.
        client.exists = False
        store.delete_document_version(uuid.uuid4(), 1)
        assert client.deletes == []

    def test_a_delete_failure_is_translated(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        client.exists = True
        client.raises = RuntimeError("boom")
        with pytest.raises(VectorStoreError):
            store.delete_document_version(uuid.uuid4(), 1)


class TestReading:
    def test_a_missing_collection_counts_zero(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        client.exists = False
        assert store.count_document_version(uuid.uuid4(), 1) == 0

    def test_collection_info_reports_the_stored_count(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        client.exists = True
        store.upsert([make_point(uuid.uuid4())])

        info = store.collection_info()
        assert info == CollectionInfo(
            name="test-chunks", vector_count=1, dimensions=8, exists=True
        )

    def test_collection_info_never_raises_when_the_database_is_down(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        # Monitoring must still render when Qdrant is down — that is precisely
        # what an operator opens the page to find out.
        client.raises = RuntimeError("connection refused")
        info = store.collection_info()
        assert info.exists is False
        assert info.vector_count is None

    def test_availability_is_probed_rather_than_assumed(
        self, store: QdrantVectorStore, client: StubQdrant
    ) -> None:
        assert store.is_available() is True
        client.reachable = False
        assert store.is_available() is False


class TestScopeBoundary:
    def test_the_protocol_exposes_no_query_method(self) -> None:
        # ``10-document-indexing.md`` puts Semantic Search out of scope, and the
        # boundary is structural rather than a matter of discipline: a retrieval
        # feature cannot be smuggled in through this interface.
        from services.vector_store import VectorStore

        members = {name for name in dir(VectorStore) if not name.startswith("_")}
        assert members == {
            "collection",
            "collection_info",
            "count_document_version",
            "delete_document_version",
            "ensure_collection",
            "is_available",
            "upsert",
        }

    def test_the_implementation_exposes_no_search(self) -> None:
        for forbidden in ("search", "query", "query_points", "retrieve", "scroll"):
            assert not hasattr(QdrantVectorStore, forbidden)


class TestResolution:
    def test_the_factory_builds_a_qdrant_store(self) -> None:
        assert isinstance(get_vector_store(), QdrantVectorStore)

    def test_the_collection_defaults_to_configuration(self) -> None:
        from core.config import settings

        assert get_vector_store().collection == settings.QDRANT_COLLECTION
