# ADR 0001: Native Playwright Execution

## Status
Accepted

## Context
The PRD requires deterministic browser automation and explicitly rejects dependence on a proprietary CLI agent for the core execution path.

## Decision
Use native Playwright-based execution as the primary browser automation layer. LLM assistance may support ambiguous field interpretation, but not replace deterministic handlers for known ATS flows.

## Consequences
- Browser logic stays inside the product codebase.
- Session management and tracing remain first-class engineering concerns.
- Existing agent-driven automation ideas from prior bots are references, not the runtime core.
