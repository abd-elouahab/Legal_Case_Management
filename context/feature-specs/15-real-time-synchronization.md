# Feature 15 — Real-Time Events & Synchronization

# Before You Begin

Before implementing this feature:

1. Read `CLAUDE.md` completely.
2. Analyze the existing project structure.
3. Review the implementations of:
   - Authentication
   - Authorization (RBAC)
   - Timeline
   - AI Assistant
   - AI Report Generation
4. Reuse existing architectural patterns and abstractions.

Do not begin implementation until these steps are completed.

---

# Objective

Implement the platform's real-time event system.

This feature enables the backend to publish domain events and synchronize updates instantly with connected clients.

The implementation should provide a reusable event infrastructure that future features such as Notifications, Email Integration, WhatsApp Integration, Monitoring, and Dashboard Analytics can build upon.

---

# Goals

Implement:

- Event publishing
- Event dispatching
- WebSocket infrastructure
- Client subscriptions
- Event authorization
- Connection management
- Presence management
- Streaming support
- Logging
- Monitoring

Do NOT implement:

- Notifications
- Email delivery
- WhatsApp delivery
- Dashboard analytics
- Scheduled jobs

---

# High-Level Flow

```text
Application Action
        │
        ▼
Domain Event
        │
        ▼
Event Dispatcher
        │
        ▼
WebSocket Manager
        │
        ▼
Authorized Clients
        │
        ▼
Frontend Update
```

The event system should be reusable by every future feature.

---

# Domain Events

Every important action should publish a domain event.

Examples include:

- case created
- case updated
- case archived

- document uploaded
- document deleted

- OCR started
- OCR completed
- OCR failed

- indexing started
- indexing completed

- report generation started
- report generated
- report failed

- timeline updated

- notification created

The implementation should allow future event types without redesign.

---

# Event Dispatcher

Implement a centralized event dispatcher.

Responsibilities:

- publish events
- dispatch events
- manage subscribers
- isolate event producers from consumers

Application modules should never communicate directly with WebSockets.

Everything should go through the dispatcher.

---

# WebSocket Infrastructure

Implement the platform's WebSocket layer.

Responsibilities:

- authenticate users
- maintain connections
- route events
- disconnect inactive clients
- reconnect gracefully

Reuse existing authentication.

---

# Connection Management

Support:

- connect
- disconnect
- reconnect

Handle:

- browser refresh
- temporary network loss
- duplicate connections

The implementation should be resilient.

---

# Client Subscriptions

Users should receive only events they are authorized to receive.

Examples:

- case updates
- document updates
- OCR progress
- report progress
- timeline updates

The implementation should support additional subscriptions later.

---

# Authorization

Reuse the existing RBAC implementation.

Users must never receive events for:

- unauthorized cases
- unauthorized documents
- unauthorized reports
- conversations belonging to other users

Authorization should be enforced before every event is delivered.

---

# Event Types

The implementation should support strongly typed events.

Examples:

- CaseUpdated
- DocumentUploaded
- OCRCompleted
- IndexCompleted
- ReportGenerated
- TimelineUpdated

Avoid generic unstructured payloads.

---

# Payloads

Each event should include only the information required by clients.

Payloads should avoid:

- unnecessary duplication
- sensitive information
- confidential document contents

Events should remain lightweight.

---

# Streaming

Support streaming where appropriate.

Examples include:

- AI response streaming
- report generation progress

Streaming should reuse the same infrastructure whenever possible.

---

# Presence

Support tracking connected users.

The implementation should prepare for future features such as:

- online indicators
- collaborative editing
- active viewers

Presence visualization itself is out of scope.

---

# Reliability

Handle:

- lost connections
- reconnects
- duplicate events
- out-of-order events

The implementation should avoid unnecessary event duplication.

---

# Logging

Log:

- client connected
- client disconnected
- event published
- event delivered
- delivery failures
- reconnect events

Never log confidential payload contents.

---

# Monitoring

Expose metrics including:

- active connections
- event throughput
- average delivery latency
- failed deliveries
- reconnect count

---

# Performance

The implementation should:

- support many simultaneous users
- avoid unnecessary broadcasts
- deliver only relevant events
- minimize bandwidth usage

Prepare for horizontal scaling.

---

# Security

Ensure:

- authenticated WebSocket connections
- authorization on every event
- secure reconnect flow
- no unauthorized subscriptions

Never trust client-provided subscription information.

---

# User Experience

Provide:

- automatic reconnect
- connection status indicator
- graceful degradation when offline

The frontend should remain usable if the WebSocket connection is temporarily unavailable.

---

# Future Integration

This feature prepares the platform for:

- Notifications
- Email Integration
- WhatsApp Integration
- Dashboard Analytics
- Monitoring
- Collaborative Editing

Those features remain out of scope.

---

# Testing

Verify:

- clients connect successfully
- authentication works
- authorization is enforced
- events are published
- events are delivered
- reconnect works
- duplicate events are avoided
- streaming works
- unauthorized users receive nothing

---

# Validation Checklist

- Event Dispatcher implemented
- WebSocket infrastructure implemented
- Connection management works
- Authorization enforced
- Events delivered correctly
- Streaming supported
- Logging implemented
- Monitoring implemented
- Performance acceptable

---

## Event-Driven Architecture

The event dispatcher is the single source of truth for publishing application events.

Future features (Notifications, Email Integration, WhatsApp Integration, Dashboard Analytics, Monitoring, and Audit Logging) must subscribe to events through this dispatcher rather than coupling directly to business logic.

Business modules should publish events but should never know who consumes them.

---

# Out of Scope

- Notifications
- Email delivery
- WhatsApp delivery
- Dashboard implementation
- Collaborative editing
- Scheduled jobs

---

# Implementation Constraints

- Read `CLAUDE.md` before implementation.
- Read `docs/architecture/ai-architecture.md`.
- Analyze the existing project structure before writing code.
- Follow existing architectural patterns.
- Reuse the existing authentication and authorization systems.
- Build a reusable event infrastructure rather than feature-specific WebSocket code.
- Keep producers independent from consumers through the event dispatcher.
- Do not modify unrelated features.
- Stop after completing this feature.