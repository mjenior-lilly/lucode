# Unity AI Gateway Coding CLI (`lucode`)

`lucode` configures and launches Pi and OpenCode through Databricks AI Gateway.
It discovers supported models in your workspace and writes each agent's native
configuration.

## Requirements

- Python 3.12+ and `uv` for installation
- `npm` and access to Lilly npm Artifactory if Pi or OpenCode must be installed
- `git` for shared prompt updates
- Databricks CLI authentication for the target workspace

## Install and initialize

Install the package, then run initialization:

```bash
uv tool install git+https://github.com/mjenior-lilly/lucode
lucode init
```

From a cloned checkout, install the current directory and initialize it:

```bash
uv tool install .
lucode init
```

Installation does not edit shell startup files or create model inventories.
Initialization adds only absent, non-security Pi preferences and asks separately before adding the
displayed extension list or setting global project trust. Use explicit
`--extensions/--no-extensions` and `--project-trust/--no-project-trust` flags in
non-interactive runs. `lucode init --revert` removes unchanged values owned by
initialization while preserving later user edits.

`LUCODE_HOME` selects the complete lucode state tree and defaults to
`~/.lucode`; it must be absolute or start with `~`. `PROMPTS_REPO` and
`PROMPTS_REPO_DIR` select the validated prompt source and checkout.

## Launch an agent

```bash
lpi
```

`lpi` safely updates and activates a complete prompt revision before launching
Pi. `lucode prompts status`, `update`, `rollback`, and `update --resume` expose
recovery; rollback holds its selected revision until explicitly resumed. An
update failure retains the active revision and warns before launch. Use
`lucode pi` when no automatic prompt update is wanted.

For OpenCode, run `loc`, or use `lucode opencode` as the long form. On first
launch, `lucode` prompts for a workspace, authenticates, discovers models, and
writes the agent configuration. Later launches reuse the workspace and refresh
the token while the session runs.

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

## Other commands

| Command | Description |
|---|---|
| `lucode status` | Show the current workspace, models, and managed files |
| `lucode revert` | Clear saved state and restore backed-up managed files |
| `lucode configure --dry-run` | Preview configuration writes |

## Managed files

| File | Owner |
|---|---|
| `$LUCODE_HOME/opencode-xdg/opencode/opencode.json` | OpenCode |
| `$LUCODE_HOME/pi-home/.pi/agent/models.json` | Pi |
| `$LUCODE_HOME/prompts/` | Validated prompt revisions and lifecycle state |

`lucode` preserves user model inventories. Installation never seeds inventories. Pi
`databricks-mlflow` membership remains user-maintained. Lucode writes private
state and backups atomically with user-only permissions on POSIX.

Workspace-managed configs are no longer supported. Leftover `managed-*.json` files
under `$LUCODE_HOME` are inert and may be deleted.

### Per-model tuning

Workspace discovery returns bare model ids. The settings that make each model
usable are shipped as package data in `lucode/defaults/` and applied when an
agent config is written:

| Agent | Tuned fields |
|---|---|
| Pi | `contextWindow`, `maxTokens`, `thinkingLevelMap`, per-model `compat`, `input`, `reasoning`, `name` |
| OpenCode | `limit` (context + output), per-call `options`, `name` |

These values are verified against the AI Gateway per model, not derived, so they
cannot be rediscovered if lost. Two rules apply on every write, including a
re-configure:

- **Membership** comes from discovery or an existing user model inventory. Tuning
  never adds a model the agent was not told to serve.
- **Tuning is layered underneath your own config.** Any field set in your
  `models.json` or `opencode.json` wins; only fields you have not set are filled
  in. An explicitly empty model list still means "serve nothing".

A gateway-verified per-model `limit` also outranks the family-substring fallback
in `model_token_limits()`, which cannot tell releases within a family apart.

The user-maintained `databricks-mlflow` provider (Pi, OSS/foundation models) is
not rendered from discovery. `lucode` fills in its route only when absent and
refreshes its token, so a route you set yourself is never overwritten.

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
