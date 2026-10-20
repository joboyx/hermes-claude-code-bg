"""`/usage` plan limits, asked of Claude Code itself.

Adapted from claude-subscription-directsdk (`directsdk_setup.fetch_usage`, `usage_windows`; MIT,
see NOTICE). Claude Code answers its own `get_usage` control request with the login it already
holds, so this plugin never reads or sends a token. The request carries no user message, so the
CLI runs no inference.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from claude_code_bg import driver

REQUEST_ID = 'hermes-usage'
# Hermes gives a plugin `/usage` hook 10 s; the CLI call must end before that.
DEFAULT_TIMEOUT_S = 8.0
NO_ANSWER = 'Claude Code gave no usage answer (needs a version with get_usage)'

# A `limits[]` kind maps to a fixed label; a model-scoped limit reads "<model> week"; any other
# kind keeps its raw name so a new limit still shows.
LIMIT_LABELS = {'session': 'Current session', 'weekly_all': 'Current week'}
# The older top-level windows, for a response without `limits`.
LEGACY_WINDOWS = (('five_hour', 'Current session'), ('seven_day', 'Current week'),
                  ('seven_day_opus', 'Opus week'), ('seven_day_sonnet', 'Sonnet week'))


def fetch_usage(command: str | None = None,
                timeout: float = DEFAULT_TIMEOUT_S) -> tuple[dict | None, str | None]:
    """
    Ask Claude Code for the plan limits with its `get_usage` control request. Return
    `(rate_limits, None)` on success, else `(None, reason)` with a reason a person can act on.
    `command` defaults to `driver.command()`; the environment is `driver.clean_env()`.
    """
    command = command or driver.command()
    if shutil.which(command) is None:
        return None, f'Claude Code is not installed ({command} not found)'
    env = driver.clean_env()
    argv = [command, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--verbose', '--tools', '', '--setting-sources', '', '--strict-mcp-config',
            '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands',
            '--no-session-persistence']
    request = json.dumps({'type': 'control_request', 'request_id': REQUEST_ID,
                          'request': {'subtype': 'get_usage', 'skip_behaviors': True}}) + '\n'
    try:
        with tempfile.TemporaryDirectory(prefix='claude-code-bg-usage-') as cwd:
            run = subprocess.run(argv, input=request, env=env, cwd=cwd, capture_output=True,
                                 text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, 'Claude Code did not answer in time'
    except OSError:
        return None, NO_ANSWER
    response = _control_response(run.stdout)
    if response is None:
        return None, NO_ANSWER
    if response.get('subtype') != 'success':
        return None, 'Claude Code refused the usage request (needs a version with get_usage)'
    body = response.get('response') if isinstance(response.get('response'), dict) else {}
    limits = body.get('rate_limits')
    if isinstance(limits, dict):
        return limits, None
    if not body.get('rate_limits_available'):
        return None, 'no Claude subscription login found (run `claude` and log in)'
    if env.get('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'):
        # With nonessential traffic off the CLI answers rate_limits: null.
        return None, 'Claude Code skips the usage fetch with nonessential traffic off'
    return None, 'Claude Code returned no plan limits'


def _control_response(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if not line.startswith('{'):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        response = row.get('response') if isinstance(row, dict) else None
        if (row.get('type') == 'control_response' and isinstance(response, dict)
                and response.get('request_id') == REQUEST_ID):
            return response
    return None


def _percent(value, fraction: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value * 100.0 if fraction and value <= 1 else float(value)


def _reset_at(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def usage_windows(payload: dict) -> tuple[list, list]:
    """
    Turn a plan-limits body (`get_usage` `rate_limits`) into `(windows, detail_lines)`:
    a list of Hermes `AccountUsageWindow` and a list of extra text lines. The `limits` list
    wins; without it the older top-level windows and `extra_usage` are read. Unexpected shapes
    degrade to fewer or emptier windows, never an exception.
    """
    from agent.account_usage import AccountUsageWindow

    limits = payload.get('limits')
    limits = [entry for entry in limits if isinstance(entry, dict)] if isinstance(limits, list) else []
    if limits:
        windows = []
        for entry in limits:
            kind = entry.get('kind')
            scope = entry.get('scope') if isinstance(entry.get('scope'), dict) else {}
            model = scope.get('model') if isinstance(scope.get('model'), dict) else {}
            name = model.get('display_name')
            label = (LIMIT_LABELS.get(kind) if isinstance(kind, str) else None) or (
                f'{name} week' if isinstance(name, str) and name else str(kind or 'unknown'))
            windows.append(AccountUsageWindow(label=label, used_percent=_percent(entry.get('percent')),
                                              reset_at=_reset_at(entry.get('resets_at'))))
        return windows, []
    windows = []
    for key, label in LEGACY_WINDOWS:
        window = payload.get(key)
        used = _percent(window.get('utilization'), fraction=True) if isinstance(window, dict) else None
        if used is not None:
            windows.append(AccountUsageWindow(label=label, used_percent=used,
                                              reset_at=_reset_at(window.get('resets_at'))))
    extra = payload.get('extra_usage') if isinstance(payload.get('extra_usage'), dict) else {}
    spent, cap = _percent(extra.get('used_credits')), _percent(extra.get('monthly_limit'))
    details = []
    if extra.get('is_enabled') and spent is not None and cap is not None:
        details.append(f"Extra usage: {spent:.2f} / {cap:.2f} {extra.get('currency') or 'USD'}")
    return windows, details
