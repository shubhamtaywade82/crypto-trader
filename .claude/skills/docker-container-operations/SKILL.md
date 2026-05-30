---
name: docker-container-operations
description: Conversational container management (status, start, stop, logs) for Ollama and Postgres. Use when debugging container health, checking logs, or restarting containers.
---

# Docker Container Operations

## Instructions
1. **Status Checks**: Run `docker compose ps` to inspect running services.
2. **Logging**: Run `docker compose logs -f <service>` to view real-time logs for a specific service.
3. **Restarts**: Stop orphaned services cleanly before starting new ones.
