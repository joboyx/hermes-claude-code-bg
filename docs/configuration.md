# Configuration

## Hermes configuration

The plugin ships no Hermes config. Add this to the Hermes `config.yaml` of the home that loads it:

```yaml
providers:
  claude-code-bg:
    request_timeout_seconds: 3600
# no stale_timeout_seconds for this provider
auxiliary:
  title_generation: {provider: <non-Claude provider>}
  compression: {provider: <non-Claude provider>}
  vision: {provider: <non-Claude provider>}
```

- `request_timeout_seconds`: one Claude Code turn can run for many minutes (tools, subagents). When Hermes gives up first, the turn keeps running; the next message delivers its reply first, marked "(Reply to an earlier message that timed out:)".
- A turn that ends without text (Claude only ran tools) returns "(Claude finished without a text reply.)" instead of waiting for the request timeout.
- `stale_timeout_seconds`: leave it unset. A stale kill cancels the wait and Hermes retries; the retry waits for the same turn and sends nothing twice, but each kill costs a retry cycle.
- `auxiliary.*`: this provider refuses auxiliary calls (titles, compression, vision). Route each task to another provider.

A chat or topic opts in with:

```text
/model claude-opus-5-5 --provider claude-code-bg
```

To make it the default for a home instead, set `model.provider: claude-code-bg` and `model.default: <model id>`. Models: `claude-opus-5-5` (default), `claude-sonnet-5-5`, `claude-haiku-5-5`. The Hermes reasoning effort maps to `--effort` (`none`/`minimal` -> `low`, `ultra` -> `max`).

Claude Code must already trust the conversation directory (`CLAUDE_CODE_BG_CWD`): run `claude` there once and accept the trust prompt.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `CLAUDE_CODE_BG_COMMAND` | `claude` | Claude Code command (for example a wrapper that adds a memory limit) |
| `CLAUDE_CODE_BG_CWD` | `<hermes home>/claude-code-bg/cwd` | Working directory of new conversations |
| `CLAUDE_CODE_BG_SETTINGS` | none | JSON object merged into the `--settings` of every launch; its keys win |
| `CLAUDE_CODE_BG_NAME_PREFIX` | `hb-` | Prefix of the session names in `claude agents` |
| `CLAUDE_CODE_BG_EFFORT` | `high` | Effort when Hermes sends none |
| `CLAUDE_CODE_BG_SYSTEM_PROMPT_FILE` | none | File with the first-turn persona (replaces the default text) |
| `CLAUDE_CODE_BG_PERSONA` | on | `0` turns the default persona off |
| `CLAUDE_CODE_BG_MAX_LIVE` | `3` | Live Claude Code sessions on the host (all kinds) above which a start or resume is refused |
| `CLAUDE_CODE_BG_MAX_CHATS` | `2` | Live chat sessions of this plugin |
| `CLAUDE_CODE_BG_MIN_MEM_MB` | `1536` | Minimum `MemAvailable` for a start or resume (Linux only) |
| `CLAUDE_CODE_BG_INFLIGHT_GRACE_S` | `120` | When Claude has written its final text but background work it started (a dev server, a watcher) keeps running, wait this long, then return the text with the note "(Background work is still running in this session.)" |
| `CLAUDE_CODE_BG_IDLE_STOP_S` | `600` | Stop the process of a conversation idle this long; the next message resumes it. `0` turns the reaper off. At most 120 while `MemAvailable` is under 1228 MB |
| `CLAUDE_PROJECTS` | `~/.claude/projects` | Where the transcripts are read |
| `CLAUDE_JOBS` | `~/.claude/jobs` | Where the background-job state is read |

Test-only (the unit tests and the smoke suite set them; do not set them in production):

| Variable | Effect |
| --- | --- |
| `CLAUDE_CODE_BG_SLEEP_SCALE` | Multiplies every driver wait; `0` skips them |
| `CLAUDE_CODE_BG_MEMINFO` | Path read instead of `/proc/meminfo` |
| `CLAUDE_CODE_BG_TMUX` | tmux command used to deny a permission prompt |
| `CLAUDE_CODE_BG_FAULT` | `after_send` raises once per turn after the send, to test the retry de-duplication |

## Hermes chat commands

- `/retry`: sends the last user message again to the same Claude session, as a new message. Claude gets a new turn and answers again; the cached reply is not reused. (A Hermes retry of a call that failed or timed out is different: it waits for the turn already sent and never sends it twice.)
- `/undo`: removes the last turn from Hermes' history only. It does not rewind the Claude session: Claude still remembers the undone message and its reply. Use `/new` for a clean session.

## State on disk

Under `<hermes home>/claude-code-bg/`: `conv/<conversation>.json` (state) and `.lock`, `events.jsonl` (event log, no message text beyond 90 characters). Under `<hermes home>/claude-watch/`: one ledger record per Claude Code session with `mode: "chat"`.

## Live smoke suite

`scripts/smoke/run.sh` runs the plugin end to end against an isolated Hermes home in `.smoke/` with the real `claude`, `claude-haiku-5-5` and effort `low`. It needs `SMOKE_CWD` (a directory Claude Code trusts). Optional `SMOKE_OTHER_PROVIDER_PLUGIN` points at a second model-provider plugin for the auxiliary and `/model` cases. Without it the auxiliary case is skipped, and the `/model` case switches one chat to `claude-sonnet-5-5` for one short turn, then back to Haiku. A case that leaves a `smoke-` row is a FAIL. Run it before a Claude Code upgrade: an update can break prompt caching on resume.
