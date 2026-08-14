# WhatsApp — where it actually lives

This directory is part of the original scaffold and is **deliberately empty**.

The WhatsApp Delivery Channel
(`context/feature-specs/18-whatsapp-delivery-channel.md`) is implemented as flat
modules inside `apps/api`, following the convention every other feature on this
platform already uses — `services/ocr_engine.py`, `services/embedding.py`,
`services/vector_store.py`, `services/llm.py`, `services/prompts.py`, and
`services/email_provider.py` are all integrations with an external thing, and none
of them lives under `integrations/`. Putting this one here would have made it the
only exception, and an exception is a second place a reader has to know about.

| Concern | Module |
| ------- | ------ |
| Vocabulary, rules, retry policy, recipient normalization | `core/whatsapp.py` |
| Delivery record and its lifecycle | `models/whatsapp.py` |
| Data access | `repositories/whatsapp.py` |
| The service: queue, render, send, retry, report | `services/whatsapp_delivery.py` |
| **The provider boundary** — the only module that speaks to Meta | `services/whatsapp_provider.py` |
| Template parameter rendering | `services/whatsapp_templates.py` |
| Observability | `services/whatsapp_metrics.py` |
| Worker pool and retry sweeper | `services/whatsapp_worker.py` |
| Template **descriptors** (`params` per version) | `apps/api/whatsapp/` |
| Response shapes | `schemas/whatsapp.py` |
| Endpoint | `GET /api/v1/notifications/whatsapp/metrics` |

Note the one row that has no email equivalent: `apps/api/whatsapp/` holds
**descriptors**, not messages. A WhatsApp template lives on Meta's side and is
approved in a console; what ships here is the ordered list of values that fill its
slots. `apps/api/whatsapp/README.md` is the file to read before submitting one.

The same applies to `integrations/court/` when that is built. These directories
are kept rather than deleted only because removing a scaffold is a change to make
deliberately rather than in passing.
