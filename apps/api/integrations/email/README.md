# Email — where it actually lives

This directory is part of the original scaffold and is **deliberately empty**.

The Email Delivery Channel (`context/feature-specs/17-email-delivery-channel.md`)
is implemented as flat modules inside `apps/api`, following the convention every
other feature on this platform already uses — `services/ocr_engine.py`,
`services/embedding.py`, `services/vector_store.py`, `services/llm.py`, and
`services/prompts.py` are all integrations with an external thing, and none of
them lives under `integrations/`. Putting this one here would have made it the
only exception, and an exception is a second place a reader has to know about.

| Concern | Module |
| ------- | ------ |
| Vocabulary, rules, retry policy, recipient validation | `core/email.py` |
| Delivery record and its lifecycle | `models/email.py` |
| Data access | `repositories/email.py` |
| The service: queue, render, send, retry, report | `services/email_delivery.py` |
| **The provider boundary** — the only module importing `smtplib` | `services/email_provider.py` |
| Template rendering (two Jinja environments) | `services/email_templates.py` |
| Observability | `services/email_metrics.py` |
| Worker pool and retry sweeper | `services/email_worker.py` |
| Templates (`subject`/`html`/`text` per version) | `apps/api/emails/` |
| Response shapes | `schemas/email.py` |
| Endpoint | `GET /api/v1/notifications/email/metrics` |

The same applies to `integrations/whatsapp/` and `integrations/court/` when those
are built. The directories are kept rather than deleted only because removing a
scaffold is a change to make deliberately rather than in passing.
