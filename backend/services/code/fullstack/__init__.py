"""
Full-stack generation services — shared API contract synthesis, middleware
provisioning, generated-backend container lifecycle, and atomic deployment.

These tie the three concurrent Code workflows (frontend / backend / middleware)
together around ONE OpenAPI contract and bring the generated app up behind a
reverse proxy. See docs/code-fullstack-generation.md.
"""
