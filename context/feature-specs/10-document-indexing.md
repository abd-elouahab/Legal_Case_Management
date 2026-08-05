# Feature 10 — Document Indexing

## Before You Begin

1. Read `CLAUDE.md`.
2. Analyze the current project structure.
3. Review the implementations of OCR Processing and Document Management.
4. Reuse existing architectural patterns.

---

# Objective

Implement the Document Indexing pipeline.

Convert OCR-extracted text into searchable knowledge by splitting text into chunks, generating embeddings, and storing vectors in a vector database.

This feature prepares the platform for Semantic Search and the AI Assistant.

It must **not** implement search, RAG, or any LLM interaction.

---

# Technology Stack

Use mature libraries:

- Qdrant
- sentence-transformers
- LangChain Text Splitters (or equivalent)

Do not implement custom embedding or chunking algorithms.

---

# Goals

Implement:

- Chunk generation
- Embedding generation
- Vector persistence
- Metadata indexing
- Index status tracking
- Re-indexing
- Retry mechanism
- Logging
- Monitoring

Do not implement:

- Semantic Search
- RAG
- AI Assistant
- AI Report Generation

---

# Processing Flow

```text
OCR Completed
      │
      ▼
Load OCR Text
      │
      ▼
Split Into Chunks
      │
      ▼
Generate Embeddings
      │
      ▼
Store in Qdrant
      │
      ▼
Update Index Status
```

The process must execute asynchronously.

---

# Chunking

Requirements:

- preserve semantic meaning
- configurable chunk size
- configurable overlap
- preserve page ordering
- preserve document version

Each chunk must reference:

- document
- document version
- case
- page
- chunk number

---

# Embeddings

Generate one embedding for every chunk.

Requirements:

- deterministic
- multilingual
- support Arabic and French

---

# Vector Storage

Persist vectors in Qdrant.

Metadata should include:

- document id
- version
- case id
- page
- chunk number
- language
- timestamps

---

# Index Status

States:

- Pending
- Indexing
- Indexed
- Failed

---

# Re-indexing

If a document version changes:

- avoid duplicate vectors
- replace outdated vectors
- support retry

The operation must be idempotent.

---

# Failure Handling

Handle:

- embedding failures
- unavailable vector database
- invalid OCR output
- timeouts

Failures must update the status, be logged, and preserve OCR data.

---

# Authorization

Reuse existing authorization.

Future search results must inherit document permissions.

---

# Timeline

Create timeline events:

- Indexing Started
- Indexing Completed
- Indexing Failed
- Indexing Retried

---

# Logging

Log:

- indexing requested
- indexing started
- chunk generation
- embedding generation
- vector persistence
- indexing completed
- indexing failed

Never log document contents.

---

# Monitoring

Expose:

- indexed documents
- indexed chunks
- average indexing duration
- failures

---

# Performance

Support:

- large documents
- batch insertion
- efficient re-indexing

---

# Extensibility

Allow future support for:

- multiple embedding models
- hybrid search
- metadata filtering
- alternative vector databases

---

# Future Integration

Prepares for:

- Semantic Search
- RAG
- AI Assistant

---

# Testing

Verify:

- OCR output is chunked
- embeddings are generated
- vectors stored in Qdrant
- metadata persisted
- re-indexing works
- duplicate vectors prevented
- timeline events created
- authorization preserved

---

# Validation Checklist

- Chunking works
- Embeddings generated
- Qdrant populated
- Metadata persisted
- Re-indexing works
- Authorization preserved
- Logging implemented
- Search NOT implemented
- AI NOT implemented

---

# Out of Scope

- Semantic Search
- RAG
- AI Assistant
- Chat
- Report generation

---

# Implementation Constraints

- Read `CLAUDE.md`.
- Analyze the project structure before coding.
- Follow existing architecture.
- Reuse existing abstractions.
- Do not modify unrelated features.
- Keep indexing independent from search.
- Stop after completing this feature.
