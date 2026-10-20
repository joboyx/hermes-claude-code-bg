"""Model catalog: Claude Code model ids, aliases, context windows, effort levels and list prices."""
from __future__ import annotations

import os

MODELS: tuple[str, ...] = ('claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5')
DEFAULT_MODEL = 'claude-opus-5-5'
DEFAULT_EFFORT = 'high'

ALIASES: dict[str, str] = {
    'opus': 'claude-opus-5-5',
    'opus-5-5': 'claude-opus-5-5',
    'opus-5.5': 'claude-opus-5-5',
    'sonnet': 'claude-sonnet-5-5',
    'sonnet-5-5': 'claude-sonnet-5-5',
    'sonnet-5.5': 'claude-sonnet-5-5',
    'haiku': 'claude-haiku-5-5',
    'haiku-5-5': 'claude-haiku-5-5',
    'haiku-5.5': 'claude-haiku-5-5',
}

# Hermes reasoning effort -> Claude Code `--effort` level.
EFFORTS: dict[str, str] = {
    'none': 'low', 'minimal': 'low', 'low': 'low', 'medium': 'medium',
    'high': 'high', 'xhigh': 'xhigh', 'max': 'max', 'ultra': 'max',
}

# USD per million tokens. Every `--bg` cache write is a 1-hour write.
PRICES: dict[str, dict[str, float]] = {
    'claude-opus-5-5': {'input': 4.0, 'output': 20.0, 'cache_write_1h': 8.0, 'cache_read': 0.20},
    'claude-sonnet-5-5': {'input': 2.0, 'output': 10.0, 'cache_write_1h': 4.0, 'cache_read': 0.20},
    'claude-haiku-5-5': {'input': 0.10, 'output': 0.50, 'cache_write_1h': 0.20, 'cache_read': 0.01},
}

FULL_WINDOW = 1_000_000
UNKNOWN_WINDOW = 200_000


def resolve(model: str | None) -> str:
    """
    Return the full model id for `model`: an alias maps to its catalog id,
    an unknown non-empty id passes through unchanged, and None or an empty
    string gives DEFAULT_MODEL.
    """
    name = (model or '').strip()
    if not name:
        return DEFAULT_MODEL
    return ALIASES.get(name.lower(), name)


def context_window(model: str) -> int:
    """
    Return the context window in tokens: 1,000,000 for catalog models
    (aliases included), 200,000 for any other id.
    """
    return FULL_WINDOW if resolve(model) in MODELS else UNKNOWN_WINDOW


def effort_of(value: str | None) -> str:
    """
    Map a Hermes reasoning effort to a Claude Code effort level. An unknown
    or missing value falls back to CLAUDE_CODE_BG_EFFORT, then DEFAULT_EFFORT.
    """
    effort = EFFORTS.get((value or '').strip().lower())
    if effort:
        return effort
    configured = (os.environ.get('CLAUDE_CODE_BG_EFFORT') or '').strip().lower()
    return EFFORTS.get(configured, DEFAULT_EFFORT)


def list_cost(model: str, usage: dict) -> float | None:
    """
    Return the list-price cost in USD of one turn's token usage (keys
    `input`, `output`, `cache_read`, `cache_creation`; missing keys count
    as 0). Cache writes use the 1-hour rate. Unknown model -> None.
    """
    price = PRICES.get(resolve(model))
    if price is None:
        return None
    tokens = {
        'input': usage.get('input') or 0,
        'output': usage.get('output') or 0,
        'cache_write_1h': usage.get('cache_creation') or 0,
        'cache_read': usage.get('cache_read') or 0,
    }
    return sum(tokens[k] * price[k] for k in tokens) / 1_000_000
