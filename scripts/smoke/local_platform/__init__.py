"""Smoke test platform plugin: loopback HTTP in, JSONL outbox out."""
from .adapter import register

__all__ = ['register']
