# Unity AI Gateway Coding CLI (`lucode`)

`lucode` configures and launches Pi and OpenCode through Databricks AI Gateway.
It discovers supported Anthropic, OpenAI Responses/GPT, Gemini, and OSS model
families from your workspace and writes each agent's native configuration.

## Requirements

- Python 3.12+
- `uv`
- `npm` when Pi or OpenCode must be installed automatically
- Databricks CLI authentication for the target workspace

## Installation

```bash
uv tool install git+https://github.com/databricks/lucode
```

Check the installed revision with `lucode --version`.

## Launch an agent

```bash
lucode pi
lucode opencode
```

Arguments after the command are passed to the selected agent:

```bash
lucode pi --help
lucode opencode --help
```

On first launch, `lucode` prompts for a Databricks workspace, authenticates,
discovers models, and writes the selected agent's config. Subsequent launches
reuse the saved workspace and refresh the token while the session runs.

## Configure workspaces and agents

Configure interactively:

```bash
lucode configure
```

Or choose agents and workspaces explicitly:

```bash
lucode configure --agents pi,opencode
lucode configure \
  --workspaces https://first.databricks.com,https://second.databricks.com \
  --agents pi,opencode
```

Existing Databricks CLI profiles can supply workspace hosts:

```bash
lucode configure --profiles DEFAULT --agents pi,opencode
```

For a headless profile that uses a personal access token, add `--use-pat`.
`--skip-validate` writes configuration without sending a test message, and
`--skip-upgrade` avoids optional agent upgrades:

```bash
lucode configure --profiles DEFAULT --agents pi,opencode \
  --use-pat --skip-validate --skip-upgrade
```

## MCP servers

OpenCode is the supported MCP client. Pi MCP registration is not currently
provided.

```bash
lucode configure mcp
lucode configure --agents opencode --mcp system.ai.slack
```

Databricks MCP services are registered as local stdio servers that run
`lucode mcp-proxy`. The proxy bridges to the workspace's streamable-HTTP MCP
endpoint and obtains fresh Databricks credentials for requests.

## Skills

```bash
# Register the schema-less skills MCP connection without downloading files.
lucode configure skills

# Download all skills from one schema into .agents/skills/.
lucode configure skills --location main.default --path /abs/project/dir

# Download selected skills.
lucode configure skills --location main.default --skill my-skill

# Expose one or more schemas through the skills MCP connection.
lucode configure skills --location main.default,ml.prod --mcp
```

When `--path` is omitted, downloaded skills are written under the user's home
directory. MCP mode downloads no files.

## Workspace-managed configuration

Workspace admins can author and publish a Pi/OpenCode policy:

```bash
lucode setup
lucode setup show
lucode setup --dry-run
lucode setup --from-file ./managed-settings.json
lucode apply
lucode apply --yes
```

Managed agent inventories are flat model lists. Pi accepts Anthropic,
OpenAI Responses/GPT, and Gemini models; OpenCode accepts Anthropic, Gemini,
and supported OSS models. Fable models are excluded. Policies can also include
MCP services, skills, and spend-based model or agent recommendations.

`lucode apply` updates only fields owned by this client. In particular, it does
not clear a remote tracing field during an unrelated update.

## Other commands

| Command | Description |
|---|---|
| `lucode status` | Show the current workspace, models, and managed files |
| `lucode usage` | Show Pi/OpenCode AI Gateway usage and available budget spend |
| `lucode usage --warehouse-id <id>` | Query a specific SQL warehouse |
| `lucode revert` | Clear saved state and restore backed-up managed files |
| `lucode configure --dry-run` | Preview configuration writes |

## Managed local files

| File | Owner |
|---|---|
| `~/.config/opencode/opencode.json` | OpenCode |
| `~/.lucode/pi-home/.pi/agent/models.json` | Pi (launched with `~/.lucode/pi-home` as its isolated `HOME`) |
| `~/.lucode/managed-settings.json` | Managed policy authored by `lucode setup` |

Existing agent files are backed up before `lucode` overwrites them. `lucode
revert` restores those backups. Stale state keys from older clients are ignored
at runtime and are not destructively migrated.

## Development

```bash
uv run pytest
uv run ruff check .
```

Run the live-workspace coverage when credentials are available:

```bash
lucode_TEST_WORKSPACE=<workspace-url> \
  uv run pytest tests/test_e2e.py tests/test_e2e_uc.py tests/test_e2e_user_agent.py -v
```

## Documentation and security

- [Databricks AI Gateway overview](https://docs.databricks.com/aws/en/ai-gateway/overview-beta)
- [Databricks CLI authentication](https://docs.databricks.com/aws/en/dev-tools/cli/authentication)
- [Monitor AI Gateway usage](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints#track-usage-of-an-endpoint)

Report security vulnerabilities to security@databricks.com rather than opening
a public issue. See [LICENSE.md](./LICENSE.md) and [NOTICE.md](./NOTICE.md).
