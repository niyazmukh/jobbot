# ADR 0002: SQLAlchemy + Alembic for Local-First Persistence

## Status
Accepted

## Context
The PRD calls for a local-first system with SQLite in WAL mode, evolving toward a richer persistence model than ad hoc SQL helpers.

## Decision
Use SQLAlchemy 2.x ORM models with Alembic migrations, targeting SQLite first. Schema design must preserve portability toward Postgres later.

## Consequences
- Database evolution is tracked in source control.
- Core entities and relationships become explicit early.
- Migrations become part of Phase 0 instead of deferred cleanup.
