# Unity AI Gateway Coding CLI (`lucode`)

`lucode` configures and launches Pi and OpenCode through Databricks AI Gateway.
It discovers supported models in your workspace and writes each agent's native
configuration.

## Requirements

- Python 3.12+
- `uv`
- `npm` if Pi or OpenCode must be installed automatically
- Databricks CLI authentication for the target workspace

## Install

```bash
uv tool install git+https://github.com/mjenior-lilly/lucode
```

## Launch an agent

```bash
lucode pi
```

For OpenCode, replace `pi` with `opencode`. On first launch, `lucode` prompts
for a workspace, authenticates, discovers models, and writes the agent
configuration. Later launches reuse the workspace and refresh the token while
the session runs.

## Configure workspaces and agents

```bash
lucode configure
```

The interactive flow selects workspaces and agents. Existing Databricks CLI
profiles can supply workspace hosts. The command help covers non-interactive
configuration, personal access tokens, validation, and upgrade controls.

## Configure MCP servers

```bash
lucode configure mcp
```

OpenCode is the supported MCP client. A local proxy obtains fresh credentials
and forwards requests to the workspace endpoint. Pi MCP registration is not
provided.

## Configure skills

```bash
lucode configure skills
```

This registers the schema-less skills MCP connection without downloading
files. The command help covers downloading skills from a Unity Catalog schema
and exposing schemas through MCP.

## Manage workspace policy

Workspace admins can author a Pi/OpenCode policy interactively:

```bash
lucode setup
```

Policies can define model inventories, MCP services, skills, and spend-based
recommendations. Pi supports Anthropic, OpenAI Responses/GPT, and Gemini
models. OpenCode supports Anthropic, Gemini, and supported OSS models. Fable
models are excluded.

The apply command publishes the reviewed policy. It updates only fields owned
by this client and does not clear unrelated remote tracing configuration.

## Other commands

| Command | Description |
|---|---|
| `lucode status` | Show the current workspace, models, and managed files |
| `lucode apply` | Publish the workspace policy authored by `lucode setup` |
| `lucode revert` | Clear saved state and restore backed-up managed files |
| `lucode configure --dry-run` | Preview configuration writes |

## Managed files

| File | Owner |
|---|---|
| `~/.config/opencode/opencode.json` | OpenCode |
| `~/.lucode/pi-home/.pi/agent/models.json` | Pi |
| `~/.lucode/managed-settings.json` | Workspace policy authored by `lucode setup` |

`lucode` preserves user model inventories unless workspace policy supplies an
exact managed inventory. It backs up existing agent files before overwriting
them, and `lucode revert` restores those backups.

## Develop

```bash
uv run pytest
```

Live-workspace tests require credentials. See the test suite for the relevant
environment variables and targets.

## Documentation and security

- [Databricks AI Gateway overview](https://docs.databricks.com/aws/en/ai-gateway/overview-beta)
- [Databricks CLI authentication](https://docs.databricks.com/aws/en/dev-tools/cli/authentication)

Report security vulnerabilities to security@databricks.com rather than opening
a public issue. See [LICENSE.md](./LICENSE.md) and [NOTICE.md](./NOTICE.md).
