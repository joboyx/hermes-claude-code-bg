# hermes-claude-code-bg

A Hermes Agent model-provider plugin that answers each Hermes chat with a Claude Code background session (`claude --bg`).

- One Hermes session maps to one Claude Code session. The first turn starts it; later turns resume it with only the newest user message. When a chat switches to this provider mid-conversation, the messages the Claude session has not seen go before the newest one as a quoted context block (at most 12,000 characters, newest kept). A session stopped before Claude Code saved its transcript (an interrupt in its first seconds) cannot be resumed: the next turn starts a new session with the whole chat as that block.
- Claude Code is the agent: it runs its own tools, skills and hooks. Hermes gets the final text, with no tool calls.
- The login is the Claude Code subscription already on the host. The plugin holds no credentials and calls only the unmodified `claude` CLI.

## Requirements

- Hermes Agent 0.21.4 or newer.
- Claude Code 2.1.304 or newer, logged in. Older versions do not reuse the prompt cache on resume.

## Security

Every launch runs `claude` with `--permission-mode bypassPermissions`, as the host user: Claude Code can read and write that user's files, run any shell command and use the host's Claude Code login without asking. Anyone who can post in a chat that uses this provider gets that access.

- Let only the operator reach the Hermes gateway: set a user allowlist on every platform that can route to this provider, and do not opt in group chats that other people can post in.
- The safer setup is a sandboxed wrapper in `CLAUDE_CODE_BG_COMMAND` (a container, a separate user, or `systemd-run` with limits) that runs the unmodified `claude` with only the files it needs.
- A permission prompt that still shows up (from a hook or a managed setting) is denied, not approved; no one can approve it from the chat.

## Install and enable

```sh
scripts/install.sh [HERMES_HOME]   # links claude_code_bg to <home>/plugins/claude-code-bg
```

Restart Hermes, add the provider block from [docs/configuration.md](docs/configuration.md) to its `config.yaml`, then opt a chat in:

```text
/model claude-opus-5-5 --provider claude-code-bg
```

## Tests

- Unit: `scripts/test.sh` (stub `claude`, no network).
- Live smoke: `SMOKE_CWD=<trusted dir> scripts/smoke/run.sh` (real `claude`, Haiku, isolated Hermes home in `.smoke/`). See [docs/configuration.md](docs/configuration.md#live-smoke-suite).

## License

MIT. Parts are adapted from `claude-subscription-directsdk` in `NousResearch/hermes-official-plugins` (MIT); see `NOTICE`.
