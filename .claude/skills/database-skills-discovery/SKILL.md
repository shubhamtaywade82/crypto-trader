---
name: database-skills-discovery
description: Postgres database schema design, Redis caching strategies, and ORM query optimization. Use when designing tables or caching keys for trade state persistence.
---

# Database Skills Discovery

## Instructions
1. **PostgreSQL Schema**: Design tables using proper primary/foreign keys, timestamp columns with timezones, and indexes for query optimization (especially on query fields like `symbol`, `timestamp`).
2. **Redis Caching**: Define clear key namespace formats, e.g., `cache:{symbol}:{timeframe}:{timestamp}`. Always set an appropriate TTL (Time To Live).
3. **Connection Pooling**: Always close database connections using context managers (`with` statements) to prevent leaks.
