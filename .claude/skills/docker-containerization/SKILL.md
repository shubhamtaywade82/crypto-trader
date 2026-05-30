---
name: docker-containerization
description: Multi-stage Dockerfiles and Docker Compose configurations for the trading engine and Ollama. Use when setting up or modifying container definitions.
---

# Docker Containerization

## Instructions
1. **Multi-stage Builds**: Use multi-stage Dockerfiles to minimize final image sizes.
2. **Security Hardening**: Run containers as non-root users when possible. Use environment variables for secrets instead of baking them into images.
3. **Docker Compose**: Define dependencies properly (e.g. `depends_on` with `condition: service_healthy` for Postgres/Redis).
