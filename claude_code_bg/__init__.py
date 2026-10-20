"""Hermes model-provider plugin `claude-code-bg`: one Claude Code background session per Hermes chat.

Importing this package registers the provider profile with Hermes. It starts no thread and
touches no file: the idle reaper starts when Hermes creates a client.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

# Hermes imports an installed plugin directory under its own module name
# (`_hermes_user_provider_<hash>_claude_code_bg`). The modules of this package import each
# other as `claude_code_bg.*`, so the package also answers to that name.
if __name__ != 'claude_code_bg':
    sys.modules.setdefault('claude_code_bg', sys.modules[__name__])

from claude_code_bg import catalog, usage  # noqa: E402
from claude_code_bg.client import CANCELLED, AuxiliaryRefused, Client  # noqa: E402

try:
    from providers import register_provider
    from providers.base import ProviderProfile
except ModuleNotFoundError as exc:
    # Outside Hermes (unit tests of the other modules, tools) there is nothing to register.
    if exc.name != 'providers':
        raise
    ProviderProfile = None

NAME = 'claude-code-bg'
COST_NOTE = 'list-price equivalent; not a subscription invoice'


def classify_api_error(error, **_):
    """
    Classify this provider's errors for Hermes' retry loop; None leaves the error to Hermes.
    The stale-call watchdog stops a hung call through `Client.cancel()`, and that stop comes
    back as the cancel error: it is a timeout (retryable), and the client de-duplicates the
    retried turn, so no second follow-up is sent. `AuxiliaryRefused` is not retryable here;
    Hermes falls back to another provider for that auxiliary task.
    """
    if isinstance(error, AuxiliaryRefused):
        return {'reason': 'format_error', 'retryable': False, 'should_fallback': True}
    if isinstance(error, RuntimeError) and str(error) == CANCELLED:
        return {'reason': 'timeout'}
    return None


if ProviderProfile is not None:

    class ClaudeCodeBgProfile(ProviderProfile):
        """
        Provider profile whose client drives `claude --bg` sessions. Hermes keeps its own
        loop; Claude Code runs its own tools and Hermes gets only the final text.
        """

        def get_model_context_length(self, model):
            """
            Return the context window of `model` from the catalog: 1,000,000 for the catalog
            models (aliases included), 200,000 for any other id.
            """
            return catalog.context_window(model)

        def create_client(self, **client_kwargs):
            """
            Return the `claude --bg` client; it accepts and ignores the keys it does not use.
            """
            return Client(**client_kwargs)

        def fetch_models(self, **kwargs):
            """
            Return the catalog model ids. There is no models endpoint to call.
            """
            return list(catalog.MODELS)

        def build_api_kwargs_extras(self, *, reasoning_config=None, **context):
            """
            Return `({}, top_level_kwargs)` for `chat.completions.create`. Chat calls carry
            `session_id` in `context` (the chat-completions transport); auxiliary calls (titles,
            compression, vision) do not, so they are marked `hermes_auxiliary` and refused by
            the client. The Hermes reasoning effort goes through as `hermes_reasoning_effort`.
            """
            top = {}
            if 'session_id' not in context:
                top['hermes_auxiliary'] = True
            elif context.get('session_id'):
                top['hermes_session_id'] = context['session_id']
            if isinstance(reasoning_config, dict) and reasoning_config.get('effort'):
                top['hermes_reasoning_effort'] = reasoning_config['effort']
            return {}, top

        def get_usage_cost(self, model, usage):
            """
            Return a list-price estimate (Hermes `CostResult`) for one response. It uses the
            turn sums in the response's `turn_usage` when present, because `prompt_tokens`
            covers only the last API call of the turn; a summed usage without that block uses
            the canonical buckets. A model without a catalog price gives status `unknown`.
            """
            from decimal import Decimal
            from agent.usage_pricing import CostResult, format_cost_label

            raw = getattr(usage, 'raw_usage', None)
            tokens = raw.get('turn_usage') if isinstance(raw, dict) else None
            if not isinstance(tokens, dict):
                tokens = {'input': usage.input_tokens, 'output': usage.output_tokens,
                          'cache_read': usage.cache_read_tokens,
                          'cache_creation': usage.cache_write_tokens}
            cost = catalog.list_cost(model, tokens)
            if cost is None:
                return CostResult(amount_usd=None, status='unknown', source='none', label='n/a',
                                  notes=('no list price for this model',))
            amount = Decimal(str(round(cost, 10)))
            return CostResult(amount_usd=amount, status='estimated', source='official_docs_snapshot',
                              label=format_cost_label(amount), notes=(COST_NOTE,))

        def fetch_account_usage(self, *, base_url=None, api_key=None):
            """
            Return the `/usage` snapshot: plan limits from Claude Code's own `get_usage`
            control request. Every failure becomes an "Unavailable" line, never an exception.
            """
            from agent.account_usage import AccountUsageSnapshot

            try:
                limits, reason = usage.fetch_usage()
                windows, details = usage.usage_windows(limits) if limits is not None else ([], [])
            except Exception as exc:  # /usage shows the reason instead of failing
                windows, details = [], []
                reason = f'cannot read the plan limits ({type(exc).__name__}: {exc})'
            return AccountUsageSnapshot(provider=self.name, source='claude_code_get_usage',
                                        fetched_at=datetime.now(timezone.utc),
                                        title='Claude plan limits', windows=tuple(windows),
                                        details=tuple(details), unavailable_reason=reason)

    profile = ClaudeCodeBgProfile(
        name=NAME,
        display_name='Claude Code background sessions',
        description='Claude Code background sessions (claude --bg) on the host Claude Code login; '
                    'Claude Code runs its own tools',
        api_mode='chat_completions',
        auth_type='external_process',
        supports_health_check=False,
        supports_model_listing=False,
        # Image parts reach the client (image routing picks 'native'); the client saves them as
        # files that Claude Code reads. False would send images through the vision auxiliary
        # task, which this provider refuses.
        supports_vision=True,
        # The seam that Hermes image routing reads (agent/models_dev.py, exact model ids);
        # `supports_vision` alone is not probed there.
        model_capabilities={m: {'supports_vision': True, 'context_window': catalog.context_window(m)}
                            for m in catalog.MODELS},
        env_vars=(),
        base_url='process://claude-code-bg',
        process_command='claude',
        process_args=(),
        # Hermes checks that the command exists before it creates a client; the driver runs
        # the same override.
        process_command_env_vars=('CLAUDE_CODE_BG_COMMAND',),
        default_aux_model='claude-haiku-5-5',
        fallback_models=catalog.MODELS,
        model_aliases=dict(catalog.ALIASES),
        # A dataclass field: a method on the subclass would be shadowed by its None default.
        classify_api_error=classify_api_error,
    )
    register_provider(profile)
