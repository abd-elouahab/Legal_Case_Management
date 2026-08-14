"""Aggregate router for API version 1.

Feature routers are registered here as they are implemented.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.v1.assistant.router import router as assistant_router
from api.v1.auth.router import router as auth_router
from api.v1.authorization.router import router as authorization_router
from api.v1.cases.router import router as cases_router
from api.v1.dashboard.router import router as dashboard_router
from api.v1.documents.router import router as documents_router
from api.v1.indexing.router import document_indexing_router
from api.v1.indexing.router import router as indexing_router
from api.v1.localization.router import router as localization_router
from api.v1.monitoring.router import router as monitoring_router
from api.v1.notifications.router import router as notifications_router
from api.v1.ocr.router import document_ocr_router
from api.v1.ocr.router import router as ocr_router
from api.v1.rag.router import router as rag_router
from api.v1.reports.router import router as reports_router
from api.v1.search.router import router as search_router
from api.v1.settings.router import router as settings_router
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

# The dashboard is the **last** router registered, and it is the only one that is
# not a feature's API. Every router above owns a resource: it creates, reads, and
# changes rows nothing else may touch. This one owns nothing — it reads across all
# of them and writes nowhere, which is why it took no new table, no new event, no
# new worker, and exactly one new permission (`dashboard:monitor`, for its own
# metrics view, like every other `*:monitor`).
#
# Mounted at its own prefix rather than under `/users/me`, and the alternative is
# worth naming. A dashboard *is* personal — its layout comes from the caller's
# role and its rows from their scopes — but so is a report and so is a
# notification, and neither of those is a user resource either. What decides it is
# that a dashboard is a *view over other modules' resources* rather than a
# resource of its own, and hanging it off the user would suggest it could be read
# for somebody else.
#
# Registered after every module it reads from, which is only cosmetic in the route
# table and deliberate in the file: this router's dependencies point at all of
# them, and none of theirs points back.
api_router.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])

# Settings is registered last, after the dashboard, and it is the second router
# here that is not a feature's own API — but it is not a *view* either, which is
# the difference from the one above it. The dashboard reads across every module
# and writes nowhere; this one writes, and what it writes is other features'
# configuration plus the four sections nothing owned.
#
# **What is absent from this prefix is the design.** There is no
# `/settings/notifications`: notification and communication preferences are the
# Notification Service's, served from `/notifications/preferences`, and
# `GET /settings` names them in its section list rather than embedding a copy. A
# second endpoint serving one stored thing is how two answers to one question
# start to disagree, and `20-settings.md`'s *"each feature should own its
# configuration"* is exactly the rule against it.
#
# Mounted at its own prefix rather than under `/users/me`, and the alternative is
# worth naming for the third time on this platform. Settings *are* personal — every
# route here is about the caller and none of them takes a user identifier — but so
# is a report, so is a conversation, and so is a notification, and none of those is
# a user resource either. What decides it is that this is a *surface over several
# features' configuration* rather than a property of the account row, and hanging
# it off the user would suggest it could be read for somebody else.
api_router.include_router(settings_router, prefix="/settings", tags=["settings"])

# Localization is registered last, and it is the smallest router on the platform
# for a reason worth stating: this feature adds **no capability anybody needs a
# grant for**. Reading the interface in Arabic is not a thing to be permitted,
# choosing a language is `PATCH /settings` (a language preference is a setting,
# and this feature deliberately did not give it a second home), and the interface's
# translations are static assets the web application serves — putting them behind
# an authenticated API would make every page load wait on a database-backed
# process for text that changes when a release does, and would put the login
# screen's own copy behind a login.
#
# So what is here is a **catalogue**, a **report**, and a **metrics view**: which
# languages exist and which one this caller reads in, what a browser could not
# render, and the four figures `21-localization.md`'s Monitoring section names.
# Everything else localization does on this platform happens inside the features
# that already say things — a notification renders in the reader's language, an
# email and a WhatsApp message are composed in the recipient's, and an AI answer
# and a report are written in the requester's. None of them needed a route here,
# which is the clearest evidence that this feature changed presentation only.
api_router.include_router(
    localization_router, prefix="/localization", tags=["localization"]
)

# Monitoring is registered last, and it is the **third** router here that is not a
# feature's own API — and the first that is not a feature at all. The dashboard is
# a view over every module's rows; Settings is a surface over every module's
# configuration; this is a view over every module's *instrumentation*, which is
# why it took no table, no migration, no event, no worker, and no business module
# change of any kind.
#
# Mounted at its own prefix rather than beside `/health` and `/ready`, and the
# distinction is the one that matters most about this router. Those two are
# unauthenticated, at the application root, outside the versioned API, because an
# orchestrator has no credentials and a load balancer will not learn to
# authenticate; they answer *"should traffic come here?"* and nothing else. What
# is here is the **operator's** view of the same platform, carrying the detail a
# public probe must not: which dependency failed and why, which setting is
# missing, what is failing and how often, who is being refused and how many
# distinct sources are trying. Every route requires `monitoring:view`, which is
# `22-monitoring.md`'s *"regular users must never access monitoring endpoints or
# operational metrics"* — the one exception being `/monitoring/export`, whose
# separate permission exists so a scraper's credential can read counters without
# reading any of the rest.
#
# **Real-time metrics are deliberately absent from this prefix.** They stay on
# `GET /realtime/metrics`, because their snapshot needs the live connection count
# and only `websocket/manager.py` holds it — and `code-standards.md` says nothing
# outside that package, the lifespan, and the endpoint may import it. A monitoring
# module that broke the platform's one transport boundary to save an operator a
# click would have been a poor trade.
api_router.include_router(monitoring_router, prefix="/monitoring", tags=["monitoring"])
