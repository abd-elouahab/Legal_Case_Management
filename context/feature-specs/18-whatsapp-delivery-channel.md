# Feature 18 — WhatsApp Delivery Channel

# Before You Begin

Before implementing this feature:

1. Read `CLAUDE.md` completely.
2. Analyze the existing project structure.
3. Review the implementations of:
   - Authentication
   - Authorization (RBAC)
   - Notifications
   - Email Delivery Channel
   - Real-Time Events & Synchronization
4. Reuse existing architectural patterns, services, and abstractions.

Do not begin implementation until these steps are completed.

---

# Objective

Implement the WhatsApp Delivery Channel.

The WhatsApp Delivery Channel is responsible for delivering selected notifications through WhatsApp.

It must never decide which business events should generate WhatsApp messages.

Business modules remain responsible for publishing domain events.

The Notification Service remains responsible for creating notifications.

The WhatsApp Delivery Channel only delivers notifications that have already been marked for WhatsApp delivery.

---

# Goals

Implement:

- WhatsApp Delivery Service
- Provider abstraction
- Meta WhatsApp Cloud API provider
- Template rendering
- Background delivery
- Delivery status tracking
- Retry mechanism
- Logging
- Monitoring

Do NOT implement:

- Notification creation
- Notification policies
- Business logic
- SMS
- Push notifications
- Marketing messages

---

# Delivery Flow

```text
Domain Event
      │
      ▼
Notification Service
      │
      ▼
Notification Created
      │
      ▼
Notification marked for WhatsApp
      │
      ▼
WhatsApp Delivery Service
      │
      ▼
Render Template
      │
      ▼
WhatsApp Provider
      │
      ▼
Recipient
```

The WhatsApp Delivery Channel consumes notifications rather than business events.

---

# WhatsApp Delivery Service

Implement a dedicated WhatsApp Delivery Service.

Responsibilities:

- receive notifications marked for WhatsApp
- render templates
- send WhatsApp messages
- track delivery
- retry temporary failures

The service should remain independent from business modules.

---

# Provider Abstraction

Implement a WhatsApp Provider abstraction.

The application must never depend directly on the Meta SDK or HTTP endpoints.

Implement the first provider using:

- Meta WhatsApp Cloud API

The implementation should allow future providers such as:

- Twilio
- Vonage
- Future providers

without changing application logic.

---

# Provider Configuration

Implement the Meta provider using environment variables.

Required configuration:

```env
WHATSAPP_PROVIDER=meta
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_BUSINESS_ACCOUNT_ID=
WHATSAPP_API_VERSION=v23.0
```

The implementation must:

- never hardcode credentials
- validate configuration during startup
- fail gracefully when configuration is missing
- provide meaningful error messages

---

# Background Processing

WhatsApp delivery must execute asynchronously.

Sending a WhatsApp message should never block:

- API requests
- notification creation
- user interactions

Reuse the project's background processing infrastructure.

---

# Delivery Status

Track:

- Pending
- Sending
- Delivered
- Failed

Persist delivery metadata for troubleshooting.

---

# Retry Policy

Automatically retry temporary failures.

Examples include:

- timeout
- temporary provider outage
- network interruption
- rate limiting

Permanent failures should be logged without affecting application functionality.

---

# WhatsApp Templates

Use WhatsApp message templates.

Templates should remain independent from application logic.

Support variables such as:

- user name
- case title
- hearing date
- report name

The implementation should prepare for localization.

---

# Supported Notification Types

WhatsApp should only be used for important notifications.

Support at minimum:

Authentication

- Account Activated
- Password Reset (optional)

Case Management

- New Case Assigned

Court

- Hearing Reminder
- Hearing Rescheduled
- Urgent Hearing Update

Reports

- AI Report Ready (optional)

System

- Critical System Announcement

The implementation should allow future message types without redesign.

---

# Events That Must NOT Generate WhatsApp Messages

The following notifications should remain in-app only:

- document uploaded
- document replaced
- document deleted
- OCR started
- OCR completed
- OCR failed
- document indexed
- AI assistant responses
- timeline updates
- document viewed
- report opened
- report regenerated

The WhatsApp Delivery Channel should not override notification policies.

---

# User Preferences

Respect notification preferences.

Users should be able to enable or disable WhatsApp delivery independently from:

- In-App notifications
- Email notifications

Future delivery channels should reuse the same preference system.

---

# Authorization

Authorization should already be enforced before notification creation.

The WhatsApp Delivery Channel should trust the Notification Service and never broaden notification visibility.

---

# Error Handling

Handle:

- invalid configuration
- invalid phone number
- provider unavailable
- timeout
- delivery rejected
- temporary failures

Failures should never interrupt application functionality.

---

# Logging

Log:

- message queued
- message sending
- message delivered
- delivery failed
- retry performed

Never log:

- message contents
- access tokens
- confidential legal information

---

# Monitoring

Expose metrics including:

- queued messages
- delivered messages
- failed deliveries
- retry count
- average delivery latency
- provider response time

---

# Performance

The implementation should:

- support batch delivery
- avoid duplicate messages
- minimize provider requests
- prepare for provider rate limits

---

# Security

Ensure:

- secure credential storage
- recipient validation
- provider authentication
- confidential information protection

Messages must never expose information the recipient is not authorized to receive.

---

# Future Integration

This feature prepares the platform for:

- SMS Delivery
- Push Notifications
- Multiple WhatsApp Providers

These remain out of scope.

---

# Testing

Verify:

- messages are queued
- messages are delivered
- provider configuration works
- retries work
- failures are isolated
- preferences are respected
- provider abstraction works
- authorization is preserved

---

# Validation Checklist

- WhatsApp Delivery Service implemented
- Provider abstraction implemented
- Meta Cloud API provider implemented
- Background delivery works
- Templates implemented
- Retry mechanism implemented
- Logging implemented
- Monitoring implemented
- Preferences respected

---

# Out of Scope

- Notification creation
- Notification policies
- SMS
- Push notifications
- Marketing campaigns
- Broadcast messaging

---

# Implementation Constraints

- Read `CLAUDE.md` before implementation.
- Analyze the existing project structure before writing code.
- Follow existing architectural patterns.
- Reuse the existing Notification Service.
- Reuse the existing background processing infrastructure.
- Build WhatsApp Delivery as a notification consumer, not as a business event consumer.
- Implement the first provider using the Meta WhatsApp Cloud API.
- Keep the provider implementation replaceable.
- Do not modify unrelated features.
- Stop after completing this feature.