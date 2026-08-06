# Feature 17 — Email Delivery Channel

# Before You Begin

Before implementing this feature:

1. Read `CLAUDE.md` completely.
2. Analyze the existing project structure.
3. Review the implementations of:
   - Authentication
   - Authorization (RBAC)
   - Notifications
   - Real-Time Events & Synchronization
4. Reuse existing architectural patterns, services, and abstractions.

Do not begin implementation until these steps are completed.

---

# Objective

Implement the Email Delivery Channel.

The Email Delivery Channel is responsible for delivering selected notifications via email.

It must not decide which application events deserve email delivery.

Business modules and the Notification Service remain responsible for deciding when a notification should be created.

The Email Delivery Channel only delivers notifications that have already been marked for email delivery.

---

# Goals

Implement:

- Email delivery service
- Email provider abstraction
- Email template rendering
- Background email delivery
- Delivery status tracking
- Retry mechanism
- Delivery logging
- Monitoring

Do NOT implement:

- Notification creation
- Notification policies
- Business logic
- SMS
- WhatsApp
- Push notifications

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
Notification marked for Email
      │
      ▼
Email Delivery Service
      │
      ▼
Render Template
      │
      ▼
Email Provider
      │
      ▼
Recipient
```

The Email Delivery Channel should never receive domain events directly.

It consumes notifications, not business events.

---

# Email Delivery Service

Implement a dedicated Email Delivery Service.

Responsibilities:

- receive notifications marked for email
- render templates
- send emails
- track delivery
- retry failures

The service should remain independent from business modules.

---

# Provider Abstraction

Implement an Email Provider abstraction.

The application must never depend directly on a specific provider.

Implement the first provider using SMTP.

The design should allow future providers such as:

- Resend
- SendGrid
- Amazon SES
- Mailgun

without changing application logic.

---

# Background Processing

Email delivery must execute asynchronously.

Sending email should never block:

- API requests
- notification creation
- user interactions

Reuse the project's background processing infrastructure.

---

# Delivery Status

Track:

- Pending
- Sending
- Sent
- Failed

Delivery history should remain available for troubleshooting.

---

# Retry Policy

Automatically retry temporary failures.

Examples include:

- timeout
- temporary SMTP failure
- network interruption

Permanent failures should be logged without affecting application functionality.

---

# Email Templates

Templates should remain independent from application logic.

Support:

- HTML
- Plain Text

Templates should support variable substitution.

Examples:

- user name
- case title
- hearing date
- report name

---

# Supported Email Types

The Email Delivery Channel should support notifications such as:

Authentication

- Password Reset
- Password Changed
- Account Activated

Case Management

- Case Assigned

Court

- Hearing Reminder
- Hearing Rescheduled

Reports

- AI Report Ready (when configured)

System

- Critical System Announcement

The implementation should support future email types without redesign.

---

# Events That Must NOT Generate Emails

The following events should remain in-app notifications only:

- document uploaded
- document deleted
- OCR started
- OCR completed
- OCR failed
- document indexed
- AI assistant responses
- timeline updates
- document viewed
- report opened

The Email Delivery Channel should not override these policies.

---

# User Preferences

Respect notification preferences.

If a user disables email delivery for a supported notification type, no email should be sent.

Future delivery channels should reuse the same preference system.

---

# Authorization

Authorization should already be enforced before notification creation.

The Email Delivery Channel should trust the Notification Service and never attempt to broaden notification visibility.

---

# Error Handling

Handle:

- provider unavailable
- timeout
- invalid recipient
- temporary delivery failure

Failures should never interrupt application functionality.

---

# Logging

Log:

- email queued
- email sending
- email delivered
- delivery failed
- retry performed

Never log:

- email contents
- passwords
- access tokens
- confidential legal information

---

# Monitoring

Expose metrics including:

- queued emails
- sent emails
- failed emails
- retry count
- average delivery latency

---

# Performance

The implementation should:

- support batch delivery
- avoid duplicate emails
- minimize provider requests
- prepare for future provider rate limits

---

# Security

Ensure:

- secure provider credentials
- protected template rendering
- recipient validation
- confidential information protection

Email templates should never expose information the recipient is not authorized to receive.

---

# Future Integration

This feature prepares the platform for:

- WhatsApp Delivery
- Push Notifications
- SMS Delivery
- Multiple Email Providers

These remain out of scope.

---

# Testing

Verify:

- emails are queued
- emails are delivered
- HTML rendering works
- plain text rendering works
- retries work
- failures are isolated
- preferences are respected
- provider abstraction works

---

# Validation Checklist

- Email Delivery Service implemented
- Provider abstraction implemented
- SMTP provider implemented
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
- WhatsApp
- SMS
- Push notifications
- Marketing emails

---

# Implementation Constraints

- Read `CLAUDE.md` before implementation.
- Analyze the existing project structure before writing code.
- Follow existing architectural patterns.
- Reuse the existing Notification Service.
- Reuse the existing background processing infrastructure.
- Build Email Delivery as a notification consumer, not as a business event consumer.
- Keep the provider implementation replaceable.
- Do not modify unrelated features.
- Stop after completing this feature.