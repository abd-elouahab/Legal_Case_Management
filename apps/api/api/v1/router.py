"""Aggregate router for API version 1.

Feature routers are registered here as they are implemented.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.v1.assistant.router import router as assistant_router
from api.v1.auth.router import router as auth_router
from api.v1.authorization.router import router as authorization_router
from api.v1.cases.router import router as cases_router
from api.v1.documents.router import router as documents_router
from api.v1.indexing.router import document_indexing_router
from api.v1.indexing.router import router as indexing_router
from api.v1.notifications.router import router as notifications_router
from api.v1.ocr.router import document_ocr_router
from api.v1.ocr.router import router as ocr_router
from api.v1.rag.router import router as rag_router
from api.v1.reports.router import router as reports_router
from api.v1.search.router import router as search_router
from api.v1.timeline.router import case_timeline_router
from api.v1.timeline.router import router as timeline_router
from api.v1.users.router import router as users_router
from api.v1.websocket.router import router as realtime_router

api_router = APIRouter()

api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(authorization_router, prefix="/authorization", tags=["authorization"])
api_router.include_router(users_router, prefix="/users", tags=["users"])
api_router.include_router(cases_router, prefix="/cases", tags=["cases"])
api_router.include_router(documents_router, prefix="/documents", tags=["documents"])

# `GET|POST /documents/{document_id}/ocr*` live under the document prefix but
# belong to the OCR module, so they are registered from there rather than added
# to the document router. Tagged `ocr`, so OpenAPI groups them with the module
# that owns them. Registered *after* the document router so that `/documents/{id}`
# and its siblings keep their place in the route table.
api_router.include_router(document_ocr_router, prefix="/documents", tags=["ocr"])
api_router.include_router(ocr_router, prefix="/ocr", tags=["ocr"])

# `GET|POST /documents/{document_id}/index*` follow the same pattern: they live
# under the document prefix but belong to the indexing module, so they are
# registered from there and tagged `indexing`.
api_router.include_router(document_indexing_router, prefix="/documents", tags=["indexing"])
api_router.include_router(indexing_router, prefix="/indexing", tags=["indexing"])

# Semantic search reads the vectors indexing wrote, so it is its own module under
# its own prefix rather than a route on `/indexing`: the two are separate
# capabilities with separate permissions, and `services/vector_store.py`
# deliberately exposes no query method for a search route to have been hung off.
api_router.include_router(search_router, prefix="/search", tags=["search"])

# The RAG pipeline reads through the search service above rather than through
# Qdrant, so it is its own module under its own prefix with its own permissions —
# and deliberately *not* a route on `/search`: retrieval returns the platform's
# own text verbatim, while this returns a generated answer, and the two are
# different capabilities that different roles hold. It is the pipeline, not the
# assistant: there is no conversation here, and `12-rag-pipeline.md` puts the
# chat interface out of scope.
api_router.include_router(rag_router, prefix="/rag", tags=["rag"])

# The AI Legal Assistant is the conversational surface *over* the pipeline above,
# and it is its own module under its own prefix for the same reason search and RAG
# are: it is a different capability (`ai:chat`, which a deployment may grant
# without `ai:ask`) with different resources. Every answer it returns is produced
# by `RagService` — it retrieves nothing, builds no answer prompt, and calls no
# model — so the two prefixes are one pipeline seen from two distances: a single
# stateless question, and a conversation made of them.
api_router.include_router(assistant_router, prefix="/assistant", tags=["assistant"])

# AI report generation is the second consumer of the pipeline and the first
# non-conversational one, so it is its own module under its own prefix — with its
# own permissions (`reports:*`, which a deployment may grant without `ai:chat`)
# and its own resources. Every section it produces is a `RagService` answer: it
# retrieves nothing, builds no answer prompt, and calls no model, exactly as the
# assistant does not. The difference between the two prefixes is what is
# persisted — a transcript there, a structured document with a lifecycle here.
api_router.include_router(reports_router, prefix="/reports", tags=["reports"])

# `GET /cases/{case_id}/timeline` lives under the case prefix but belongs to the
# timeline module, so it is registered from there rather than added to the case
# router. Tagged `timeline`, so OpenAPI groups it with the module that owns it.
api_router.include_router(case_timeline_router, prefix="/cases", tags=["timeline"])
api_router.include_router(timeline_router, prefix="/timeline", tags=["timeline"])

# Real-time synchronization is its own module under its own prefix, and — unlike
# every router above it — it is **not a feature's API**. It is the transport every
# feature's updates travel on: the WebSocket at `/realtime/ws`, plus the two
# administrative reads that say whether it is healthy and who is connected.
#
# It is deliberately *not* mounted under `/cases`, `/documents`, or `/reports`,
# even though those are what its events are about. A socket per feature would be
# a socket per open panel in a browser that allows a handful in total, and the
# whole point of a central dispatcher is that one channel carries everything the
# caller is entitled to. Which events reach it is decided by subscription and by
# per-resource authorization, not by which URL it was opened at.
api_router.include_router(realtime_router, prefix="/realtime", tags=["realtime"])

# Notifications is the **first consumer** of the dispatcher above rather than
# another producer on it, and its API says so: there is no route here that a
# business module calls, and only one that creates anything at all
# (`POST /notifications/announcements`, which exists because the platform has no
# broadcast topic to publish an announcement on). Everything else in a feed
# arrived through the event channel from modules that do not know this router
# exists.
#
# Mounted at its own prefix rather than under `/users/me` or `/realtime`, and
# both alternatives are worth naming. It is not a user resource: a notification
# is about a case or a document and merely *belongs* to somebody, exactly as a
# report does. And it is not part of real-time synchronization: it shares that
# feature's transport and nothing else — a notification is persistent, queryable,
# filterable, and readable long after the socket that announced it closed, which
# is the whole difference between the two modules.
api_router.include_router(notifications_router, prefix="/notifications", tags=["notifications"])
