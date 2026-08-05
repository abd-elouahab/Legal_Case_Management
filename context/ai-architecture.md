# AI Architecture

## Purpose

This document defines the AI architecture used throughout the Legal Case Management Platform.

All AI features must follow the decisions described here.

Feature specifications must reference this document instead of redefining AI technologies or architectural choices.

---

# Design Principles

The AI system must be:

- Modular
- Extensible
- Provider-independent
- Retrieval-first
- Secure
- Observable
- Testable

The platform must never tightly couple business logic to a specific LLM provider.

---

# High-Level Architecture

```text
                    User
                      │
                      ▼
              AI Legal Assistant
                      │
                      ▼
                RAG Pipeline
                      │
          ┌───────────┴────────────┐
          ▼                        ▼
   Semantic Search          Conversation Context
          │
          ▼
       Qdrant
          ▲
          │
Document Indexing
          ▲
          │
         OCR
```

---

# AI Stack

| Component | Technology |
|------------|------------|
| Orchestrator | LangGraph |
| LLM Provider | Google Gemini |
| Default Model | gemini-2.5-flash |
| Embedding Model | BAAI/bge-m3 |
| Vector Database | Qdrant |
| OCR Engine | Tesseract OCR |
| Prompt Templates | Jinja2 |
| Vector Search | Qdrant Similarity Search |

These technologies should remain consistent across all AI features unless this document is updated.

---

# Why Gemini?

Google Gemini is selected because it provides:

- generous free tier
- strong reasoning
- excellent multilingual support
- long context window
- fast inference
- production-ready API

The implementation must remain provider-independent despite Gemini being the default.

---

# LLM Provider Architecture

The system must never call the Gemini SDK directly outside the provider implementation.

Instead:

```text
Application
      │
      ▼
LLM Provider Interface
      │
      ▼
Gemini Provider
      │
      ▼
Gemini API
```

Future providers should be implementable without changing application logic.

Examples:

- OpenRouter
- Groq
- OpenAI
- Ollama
- Anthropic

---

# Provider Interface

Every provider should expose a common interface.

Responsibilities include:

- text generation
- streaming generation
- model selection
- token counting (when available)
- retry handling
- error normalization

The rest of the application must depend only on this abstraction.

---

# Agent Orchestration

LangGraph is the official orchestration framework.

The graph should coordinate AI workflows.

The initial implementation includes:

- Retrieval Agent

Future features will add:

- Report Generation Agent
- Summarization Agent
- Information Extraction Agent
- Compliance Agent
- Notification Agent
- Translation Agent

Agents should be added only when their corresponding feature is implemented.

---

# Embedding Strategy

The project uses:

BAAI/bge-m3

Reasons:

- multilingual
- high retrieval quality
- Arabic support
- French support
- English support

The same embedding model must be used for:

- document indexing
- user queries

Changing embedding models requires re-indexing all vectors.

---

# OCR Strategy

OCR uses:

Tesseract OCR

OCR is responsible only for extracting text.

OCR must never:

- summarize
- classify
- generate embeddings
- invoke an LLM

---

# Document Indexing

Responsibilities:

- chunk documents
- generate embeddings
- persist vectors
- maintain metadata

Indexing must remain independent from retrieval.

---

# Semantic Search

Responsibilities:

- query embedding
- vector similarity search
- metadata filtering
- authorization filtering
- ranking

Semantic Search must never invoke the LLM.

---

# RAG Pipeline

Responsibilities:

- retrieve context
- assemble prompts
- invoke the LLM
- generate citations
- return grounded responses

The RAG Pipeline must never manage conversations.

---

# AI Assistant

Responsibilities:

- manage conversations
- maintain chat history
- stream responses
- display citations
- collect user feedback

The AI Assistant must delegate all reasoning to the RAG Pipeline.

---

# Prompt Strategy

Prompt templates should be stored separately from application logic.

Templates should be reusable and versioned.

Prompts must instruct the LLM to:

- answer only using retrieved context
- avoid hallucinations
- admit when evidence is insufficient
- include citations whenever possible

---

# Hallucination Prevention

The AI must never fabricate legal information.

If retrieval fails or evidence is insufficient, the assistant should clearly state that it could not find supporting information.

Grounded responses are always preferred over speculative answers.

---

# Security

The AI must never bypass existing authorization rules.

Every retrieval request must respect:

- document permissions
- case permissions
- user roles

Unauthorized content must never reach the LLM.

---

# Observability

All AI components should expose metrics for:

- latency
- token usage
- retrieval duration
- embedding duration
- OCR duration
- error rate

Structured logging should be used throughout the AI pipeline.

---

# Future Evolution

The architecture should support future capabilities without major redesign, including:

- multiple LLM providers
- hybrid search
- reranking
- conversation memory
- voice assistant
- autonomous planning
- multi-agent workflows
- human approval workflows

---

# AI Feature Dependency Graph

```text
OCR
 │
 ▼
Document Indexing
 │
 ▼
Semantic Search
 │
 ▼
RAG Pipeline
 │
 ▼
AI Legal Assistant
 │
 ├──────────────┐
 ▼              ▼
Report Agent    Summarization Agent
 │              │
 ├──────────────┤
 ▼              ▼
Compliance Agent
 │
 ▼
Translation Agent
 │
 ▼
Future Voice Agent
```

---

# Guiding Principle

Every AI feature implemented in this project must follow this architecture.

Feature specifications should reference this document rather than redefining the AI stack.

Any architectural changes must be made here first before being reflected in future feature specifications.