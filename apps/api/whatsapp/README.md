# WhatsApp message templates

These are **descriptors**, not messages. A WhatsApp template lives on the
provider's side — submitted to Meta, reviewed, approved, and versioned in a
console this repository cannot read — and what the platform sends is its *name*,
its *language*, and an ordered list of **parameters** substituted into its
`{{1}}`, `{{2}}`, … slots.

Each file here says, in order, which values fill one approved template's slots:
`<name>.v<version>.params.j2`, **one parameter per line**. Blank lines are
dropped, so a descriptor can use `{% if %}` without producing an empty slot.

## Why the wording is on this side

The tempting design is to put the sentences in the approved template
("Your hearing on case {{1}} has been rescheduled") and send a case number. It
reads better in the console, and it puts the platform's wording somewhere no test
can assert on, no reviewer can diff, and nobody can keep in step with
`core/notifications.py` — so the first time the in-app wording is improved,
WhatsApp keeps saying the old thing and nobody finds out.

So the approved template is a **thin envelope** — a greeting slot, a heading slot,
a body slot — and the sentences come from `core/notifications.py`, the same
function the in-app feed and the email channel render from. The three channels
cannot drift, and an Arabic recipient's message is Arabic for the same reason
their feed is.

## Registering the templates

**Since `21-localization.md` shipped, "each language the deployment sends" means
every language its *users* have chosen, not one.** A delivery row carries the
recipient's own `user_settings.language`, resolved before the message is queued,
so a firm with one Arabic-reading lawyer needs the Arabic localization approved
even if `WHATSAPP_DEFAULT_LANGUAGE` is English. A template missing in a
recipient's language fails that message with `template_rejected` and affects
nobody else — visible in the metrics, one delivery at a time.

Two templates, in each language the deployment serves (`en_US`, `fr`, `ar`),
category **UTILITY** — not MARKETING: these are transactional notifications about
a reader's own cases, and `18-whatsapp-delivery-channel.md` puts marketing
messages out of scope explicitly.

| Name | Slots | Used by |
| ---- | ----- | ------- |
| `notification` | greeting, title, message, link-or-fallback | case assigned, hearing updated, hearing awaited, report ready, maintenance notice |
| `security` | greeting, title, message, security note | account activated, password reset |

The exact body each one must have is written at the top of its descriptor file,
which is the place to read before submitting one.

**The parameter count and order in a descriptor *are* the contract with the
approved template.** Changing either without re-submitting the template produces
Cloud API error `132000` on every message, recorded as `template_rejected` — a
failure class that exists precisely so an operator reading
`GET /api/v1/notifications/whatsapp/metrics` knows to open Business Manager
rather than this repository.

## Adding a template

1. Add `<name>.v1.params.j2` here, documenting its slots at the top.
2. Submit a matching template to the WhatsApp Business account, under the same
   name, in every language the deployment sends.
3. Point a rule at it in `core/whatsapp.py` (`WHATSAPP_RULES`).

A new *version* of an existing template is a new file plus a `version=` on the
rule, so two versions coexist and every delivery row records which one produced
it.
