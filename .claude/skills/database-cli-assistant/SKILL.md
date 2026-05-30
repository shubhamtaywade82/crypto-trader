---
name: database-cli-assistant
description: Safe SQL queries, schema discovery, and Redis CLI helpers. Use when interacting with PostgreSQL or Redis via shell tools.
---

# Database CLI Assistant

## Instructions
1. **Read-Only Safeguard**: Always perform schema discovery or `SELECT` queries before running modifications.
2. **Redis Queries**: Use patterns or scan queries (`SCAN`) instead of `KEYS *` to prevent blocking the Redis server.
3. **Transaction Safety**: Use SQL transactions for multi-statement queries.
