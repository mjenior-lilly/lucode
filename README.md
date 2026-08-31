# Unity AI Gateway Coding CLI (`ucode`)

`ucode` configures and launches Pi and OpenCode through Databricks AI Gateway.
It discovers supported Anthropic, OpenAI Responses/GPT, Gemini, and OSS model
families from your workspace and writes each agent's native configuration.

## Requirements

- Python 3.12+
- `uv`
- `npm` when Pi or OpenCode must be installed automatically
- Databricks CLI authentication for the target workspace

## Installation

```bash
uv tool install git+https://github.com/databricks/ucode
```

Check the installed revision with `ucode --version`.

## Launch an agent

```bash
ucode pi
ucode opencode
```

Arguments after the command are passed to the selected agent:

```bash
ucode pi --help
ucode opencode --help
```

On first launch, `ucode` prompts for a Databricks workspace, authenticates,
discovers models, and writes the selected agent's config. Subsequent launches
reuse the saved workspace and refresh the token while the session runs.

## Configure workspaces and agents

Configure interactively:

```bash
ucode configure
```

Or choose agents and workspaces explicitly:

```bash
ucode configure --agents pi,opencode
ucode configure \
  --workspaces https://first.databricks.com,https://second.databricks.com \
  --agents pi,opencode
```

Existing Databricks CLI profiles can supply workspace hosts:

```bash
ucode configure --profiles DEFAULT --agents pi,opencode
```

For a headless profile that uses a personal access token, add `--use-pat`.
`--skip-validate` writes configuration without sending a test message, and
`--skip-upgrade` avoids optional agent upgrades:

```bash
ucode configure --profiles DEFAULT --agents pi,opencode \
  --use-pat --skip-validate --skip-upgrade
```

## MCP servers

OpenCode is the supported MCP client. Pi MCP registration is not currently
provided.

```bash
ucode configure mcp
ucode configure --agents opencode --mcp system.ai.slack
```

Databricks MCP services are registered as local stdio servers that run
`ucode mcp-proxy`. The proxy bridges to the workspace's streamable-HTTP MCP
endpoint and obtains fresh Databricks credentials for requests.

## Skills

```bash
# Register the schema-less skills MCP connection without downloading files.
ucode configure skills

# Download all skills from one schema into .agents/skills/.
ucode configure skills --location main.default --path /abs/project/dir

# Download selected skills.
ucode configure skills --location main.default --skill my-skill

# Expose one or more schemas through the skills MCP connection.
ucode configure skills --location main.default,ml.prod --mcp
```

When `--path` is omitted, downloaded skills are written under the user's home
directory. MCP mode downloads no files.

## Workspace-managed configuration

Workspace admins can author and publish a Pi/OpenCode policy:

```bash
ucode setup
ucode setup show
ucode setup --dry-run
ucode setup --from-file ./managed-settings.json
ucode apply
ucode apply --yes
```

Managed agent inventories are flat model lists. Pi accepts Anthropic,
OpenAI Responses/GPT, and Gemini models; OpenCode accepts Anthropic, Gemini,
and supported OSS models. Fable models are excluded. Policies can also include
MCP services, skills, and spend-based model or agent recommendations.

`ucode apply` updates only fields owned by this client. In particular, it does
not clear a remote tracing field during an unrelated update.

## Other commands

| Command | Description |
|---|---|
| `ucode status` | Show the current workspace, models, and managed files |
| `ucode usage` | Show Pi/OpenCode AI Gateway usage and available budget spend |
| `ucode usage --warehouse-id <id>` | Query a specific SQL warehouse |
| `ucode revert` | Clear saved state and restore backed-up managed files |
| `ucode configure --dry-run` | Preview configuration writes |

## Managed local files

| File | Owner |
|---|---|
| `~/.config/opencode/opencode.json` | OpenCode |
| `~/.pi/agent/models.json` | Pi |
| `~/.ucode/managed-settings.json` | Managed policy authored by `ucode setup` |

Existing agent files are backed up before `ucode` overwrites them. `ucode
revert` restores those backups. Stale state keys from older clients are ignored
at runtime and are not destructively migrated.

## Development

```bash
uv run pytest
uv run ruff check .
```

Run the live-workspace coverage when credentials are available:

```bash
UCODE_TEST_WORKSPACE=<workspace-url> \
  uv run pytest tests/test_e2e.py tests/test_e2e_uc.py tests/test_e2e_user_agent.py -v
```

## Documentation and security

- [Databricks AI Gateway overview](https://docs.databricks.com/aws/en/ai-gateway/overview-beta)
- [Databricks CLI authentication](https://docs.databricks.com/aws/en/dev-tools/cli/authentication)
- [Monitor AI Gateway usage](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints#track-usage-of-an-endpoint)

Report security vulnerabilities to security@databricks.com rather than opening
a public issue. See [LICENSE.md](./LICENSE.md) and [NOTICE.md](./NOTICE.md).
