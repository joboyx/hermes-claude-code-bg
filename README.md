# hermes-claude-code-bg

A Hermes Agent model-provider plugin that answers each Hermes chat with a Claude Code background session (`claude --bg`).

- One Hermes session maps to one Claude Code session. The first turn starts it; later turns resume it with only the newest user message.
- Claude Code is the agent: it runs its own tools, skills and hooks. Hermes gets the final text, with no tool calls.
- The login is the Claude Code subscription already on the host. The plugin holds no credentials and calls only the unmodified `claude` CLI.


## Requirements

- Hermes Agent 0.21.4 or newer.
- Claude Code 2.1.304 or newer. Older versions do not reuse the prompt cache on resume.

## License

MIT. Parts are adapted from `claude-subscription-directsdk` in `NousResearch/hermes-official-plugins` (MIT); see `NOTICE`.
