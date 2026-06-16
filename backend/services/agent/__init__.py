"""
Shared Agent Swarm orchestration layer.

This package is the single place where multi-agent workflows for the PPT /
RedBook / Code product domains are executed, observed and persisted. The
domains do not each reinvent orchestration — they register a workflow and reuse
the runtime, recorder, event bus and artifact storage here.
"""
