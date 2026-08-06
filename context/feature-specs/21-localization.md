# Feature 21 — Localization (Internationalization)

# Before You Begin

Before implementing this feature:

1. Read `CLAUDE.md` completely.
2. Analyze the existing project structure.
3. Review the implementations of:
   - Authentication
   - Notifications
   - Email Delivery Channel
   - WhatsApp Delivery Channel
   - Dashboard & Analytics
   - Settings
   - AI Assistant
   - AI Report Generation
4. Reuse existing architectural patterns, services, and abstractions.

Do not begin implementation until these steps are completed.

---

# Objective

Implement Localization (Internationalization).

The platform should support multiple languages while keeping business logic, authorization, and application behavior completely independent from localization.

Localization should affect only presentation and user-facing communication.

The implementation should make it easy to add new languages without modifying application logic.

---

# Goals

Implement:

- User interface localization
- Translation management
- Runtime language switching
- RTL support
- Localized validation messages
- Localized notifications
- Localized email templates
- Localized WhatsApp templates
- Localized AI responses
- Localized AI reports
- Date and time localization
- Number localization
- Logging
- Monitoring

Do NOT implement:

- Automatic document translation
- OCR language translation
- Legal document translation
- AI translation services

---

# Supported Languages

Implement support for:

- English (default)
- French
- Arabic

The implementation should allow future languages without redesign.

---

# Language Selection

The user's preferred language should be determined using the following priority:

1. User preference stored in Settings
2. Browser language (first login only)
3. Application default language

The selected language should persist across sessions.

---

# User Interface Localization

All user-facing interface text should be localized.

Examples include:

- Navigation
- Buttons
- Forms
- Tables
- Dialogs
- Empty states
- Loading messages
- Error messages
- Validation messages
- Confirmation dialogs

No user-facing text should be hardcoded.

---

# Translation Resources

Store translations separately from application logic.

The implementation should:

- organize translations by language
- support namespaces or modules
- support future feature translations

Missing translations should never break the application.

---

# Fallback Strategy

If a translation is unavailable:

1. Use the default language.
2. If still unavailable, display a meaningful fallback.

The application should never expose translation keys to users.

---

# Right-to-Left (RTL) Support

Arabic requires full RTL support.

The implementation should support:

- RTL layouts
- RTL navigation
- RTL forms
- RTL dialogs
- RTL typography
- Proper text alignment

RTL support should coexist with left-to-right languages.

---

# Date & Time Localization

Display dates and times according to the user's preferences.

Localization should respect:

- preferred language
- preferred time zone
- preferred date format
- preferred time format

Backend timestamps should remain language-independent.

---

# Number Localization

Support localized formatting for:

- numbers
- percentages
- file sizes
- storage values

Formatting should depend on the selected locale.

---

# Notifications

Reuse the existing Notification Service.

Notification titles and messages should be localized before delivery.

Localization should support:

- In-App Notifications
- Email Notifications
- WhatsApp Notifications

---

# Email Templates

Provide localized email templates.

Every supported email should exist in:

- English
- French
- Arabic

The Email Delivery Channel should automatically select the correct template based on the recipient's preferred language.

---

# WhatsApp Templates

Provide localized WhatsApp templates.

The WhatsApp Delivery Channel should automatically choose the correct language for each recipient.

The implementation should prepare for future template expansion.

---

# AI Responses

The AI Assistant should respond in the user's preferred language by default.

If the user explicitly requests another language during a conversation, that request should override the default for that interaction only.

The implementation should reuse the existing AI architecture.

---

# AI Report Generation

Generated reports should use the user's preferred language by default.

Users should also be able to explicitly request a report in another supported language.

The implementation should reuse the existing Report Generation workflow.

---

# Business Logic

Localization must never affect:

- authorization
- RBAC
- routing
- database schema
- business rules
- workflow execution

Localization changes presentation only.

---

# Settings Integration

Reuse the existing Settings implementation.

Language selection should integrate with:

- preferred language
- time zone
- date format
- time format

Localization should not duplicate these settings.

---

# Legal Document Integrity

Localization must never translate or modify uploaded legal documents.

Original documents remain stored and displayed exactly as uploaded.

Localization applies only to the application's interface and generated content (notifications, emails, AI responses, AI reports, etc.).

If document translation is required in the future, it should be implemented as a separate AI feature.

---

# Error Handling

Handle:

- missing translations
- invalid locale
- unsupported language
- incomplete translation resources

Failures should gracefully fall back to the default language.

---

# Logging

Log:

- language changed
- translation loading failures
- unsupported locale requests

Never log confidential user information.

---

# Monitoring

Expose metrics including:

- active languages
- translation loading failures
- missing translations
- language distribution

---

# Performance

The implementation should:

- load only required translations
- minimize unnecessary downloads
- cache translation resources
- support efficient language switching

Localization should not noticeably impact application performance.

---

# Security

Ensure:

- localization cannot bypass authorization
- translation resources contain no sensitive information
- language switching cannot affect application permissions

---

# User Experience

Provide:

- language selector
- immediate language switching
- consistent translations
- proper RTL behavior
- localized dates and times

Language changes should not require users to reconfigure other preferences.

---

# Future Integration

This feature prepares the platform for:

- Additional languages
- Region-specific customization
- Localized legal terminology
- Multi-region deployments

These remain out of scope.

---

# Testing

Verify:

- language switching
- translation loading
- fallback behavior
- RTL layouts
- localized notifications
- localized emails
- localized WhatsApp messages
- localized AI responses
- localized AI reports
- date formatting
- number formatting
- authorization remains unaffected

---

# Validation Checklist

- UI localization implemented
- Runtime language switching implemented
- RTL support implemented
- Notifications localized
- Email templates localized
- WhatsApp templates localized
- AI responses localized
- AI reports localized
- Date & time localization implemented
- Number localization implemented
- Fallback strategy implemented
- Logging implemented
- Monitoring implemented

---

# Out of Scope

- Automatic document translation
- OCR translation
- AI-powered document translation
- Machine translation services
- Regional legal adaptations

---

# Implementation Constraints

- Read `CLAUDE.md` before implementation.
- Analyze the existing project structure before writing code.
- Follow existing architectural patterns.
- Reuse the existing Settings module for language preferences.
- Reuse the existing Notification, Email, WhatsApp, AI Assistant, and AI Report systems.
- Do not hardcode user-facing strings.
- Localization must affect presentation only and never alter business logic.
- Design the translation system to allow additional languages without restructuring the application.
- Do not modify unrelated features.
- Stop after completing this feature.