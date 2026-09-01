# Pi and OpenCode user-model preservation plan

## 1. Status and context

**Status: Implementation-ready**

### Planning sources

- The user request to investigate whether Pi consumes user-defined models from `models.json` and determine the OpenCode changes needed for equivalent behavior.
- The completed investigation in this session, including:
  - installed Pi `0.84.4` loading a temporary `models.json` and listing both custom models;
  - the current Pi configuration containing 13 nonempty Databricks model entries, all 13 of which appeared in `pi --list-models`;
  - an OpenCode writer characterization in which `write_tool_config()` removed a user-defined model and replaced it with the discovered model;
  - focused tests passing before planning.
- Current repository evidence at revision `28cc9d0cb584333e402a8fbfabebb140db56c1df`.

### Repository and workspace

- Repository root: `/Users/L146025/Library/CloudStorage/OneDrive-EliLillyandCompany/Desktop/repos/lucode`
- Branch: `main`, tracking `origin/main`.
- Git submodules: none. "OpenCode submodule" therefore means `src/lucode/agents/opencode.py`, not a Git submodule or an upstream OpenCode checkout.
- Dirty state: `uv.lock` is modified. Its diff reorders the editable `lucode` package block and adds `rich` to that block. This is unrelated to the planned source changes and must not be overwritten, staged, or included without owner review.
- Workspace constraint: no `opencode` executable is installed. The plan can verify the Python config writer but cannot require a live OpenCode check unless the executable is independently available during implementation. Do not install it as part of this work.
- Artifact constraint: this plan is the only planned artifact. Target code was not edited while preparing it.

## 2. Goal and scope

### Intended behavior

1. Pi must always launch against lucode's intended isolated agent directory and consume its `models.json`, even if the parent environment already defines `PI_CODING_AGENT_DIR`.
2. During ordinary, unmanaged OpenCode configuration and token refresh, a valid user-maintained `models` map already present under a lucode Databricks provider in `opencode.json` must remain authoritative for model membership and user metadata.
3. lucode must continue applying gateway-required model fields, provider URLs, credentials, and telemetry headers.
4. If no user-maintained OpenCode model map exists for an active provider, discovered models must continue to bootstrap that provider. This preserves current first-configuration behavior.
5. A workspace-managed OpenCode model inventory must continue to replace local model membership exactly and must remain separate from persisted discovery state.

### Observable success criteria

- An unmanaged OpenCode rewrite retains every existing user model ID and its custom fields for each active Databricks provider; it does not add newly discovered IDs to an existing valid `models` map.
- Required per-model values still win where gateway correctness requires them: `User-Agent`, Anthropic `options.toolStreaming = false`, and known OSS `limit` values.
- A provider with no existing valid `models` map is populated from discovery as it is today.
- A managed inventory excludes local and discovered model IDs not present in policy.
- Background OpenCode token refresh changes only the Databricks credential fields and leaves model maps and unrelated configuration unchanged.
- Pi's runtime environment explicitly points `PI_CODING_AGENT_DIR` to `PI_CONFIG_DIR`.
- Existing MCP entries, non-lucode providers, top-level OpenCode settings, backup behavior, model selector prefixes, and state persistence continue to work.
- Focused tests, the full test suite, and Ruff pass without adding a `uv.lock` change to the implementation diff.

### Non-goals

- Do not modify upstream Pi or upstream OpenCode.
- Do not make OpenCode read Pi's `models.json`; each agent keeps its native configuration format.
- Do not add model families or providers, change discovery APIs, alter managed-policy schema, or change public CLI options.
- Do not change model ordering or the current default-model policy beyond teaching it to read the separate transient managed inventory.
- Do not migrate, rewrite, or backfill existing user files. The next normal configuration write applies the corrected merge behavior.
- Do not install OpenCode or any dependency.
- Do not include or normalize the existing `uv.lock` modification.

### Smallest complete change

Update the internal managed-state representation, OpenCode's config merge and refresh paths, Pi's runtime environment, focused tests, and the directly governing comments/documentation. No new module or compatibility layer is needed.

## 3. Evidence and decisions

### Confirmed facts

- **C1:** Pi's overlay accepts `existing_config` and preserves existing provider `models` arrays during unmanaged discovery. Managed inventories replace them. Evidence: `src/lucode/agents/pi.py:115-208`, `src/lucode/agents/pi.py:225-305`, and `tests/test_agent_pi.py:179-218,408-437`.
- **C2:** Pi refreshes credentials through a token-only path rather than rebuilding model definitions. Evidence: `src/lucode/agents/pi.py:321-352`.
- **C3:** Pi's runtime environment sets `HOME` but not `PI_CODING_AGENT_DIR`. Installed Pi gives `PI_CODING_AGENT_DIR` precedence over the default home-derived agent directory. Evidence: `src/lucode/agents/pi.py:360-364`; installed package `/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/dist/config.js:getAgentDir`; isolated command result where an inherited value bypassed temporary `HOME` until the variable was unset.
- **C4:** OpenCode currently builds each provider's model map from discovered `opencode_models`. Evidence: `src/lucode/agents/opencode.py:89-157`.
- **C5:** OpenCode reads the existing file only after rendering, removes all lucode Databricks provider blocks, and merges the generated blocks. Evidence: `src/lucode/agents/opencode.py:160-195`.
- **C6:** OpenCode background refresh calls the full writer. Evidence: `src/lucode/agents/opencode.py:237-249`.
- **C7:** Required OpenCode fields currently live per model: `User-Agent`, Anthropic `toolStreaming`, and known OSS limits. Evidence: `src/lucode/agents/opencode.py:75-150` and `tests/test_agent_opencode.py:124-187`.
- **C8:** Managed OpenCode and discovered OpenCode inventories currently share `opencode_models`, while Pi uses the separate `pi_models` key. Evidence: `src/lucode/managed/resolve.py:48-71`, `src/lucode/agents/models.py:8-55`, and `tests/test_managed_resolve.py:237-303`.
- **C9:** `_managed_overlay` is emitted only when a resolved value differs from local state, so it cannot reliably identify a policy inventory that happens to equal local discovery state. Evidence: `src/lucode/managed/resolve.py:179-196`.
- **C10:** `deep_merge_dict()` recursively merges dictionaries with overlay leaves winning. Evidence: `src/lucode/config.py:204-213` (`deep_merge_dict`).
- **C11:** Baseline checks pass: `.venv/bin/pytest tests/test_agent_pi.py tests/test_agent_opencode.py tests/test_managed_resolve.py -q` reported `143 passed`; focused Ruff reported `All checks passed!`.
- **C12:** The OpenCode characterization confirmed destructive replacement: a custom `user-model` with custom `name` and `limit` was absent after `write_tool_config()`, while the discovered model remained.

### Decisions

- **D1:** Preserve models from OpenCode's native `opencode.json`; do not couple OpenCode to Pi's `models.json`. Tradeoff: the preservation logic must handle OpenCode's object-shaped model map rather than reusing Pi's array logic, but agent ownership remains clear.
- **D2:** Use this model-source precedence per active provider: managed inventory, then an existing valid user `models` dictionary, then discovery. An explicitly empty dictionary is valid and remains authoritative. Tradeoff: discovery still gates provider activation, matching Pi and current workspace-family routing, while first-time configuration remains usable.
- **D3:** Merge required lucode model overlays into copies of user entries. Preserve all other user metadata; required gateway fields win on conflicts. This prioritizes request correctness without discarding user annotations or optional settings.
- **D4:** Introduce a transient internal key named `opencode_managed_models`. Do not infer managed mode from `_managed_overlay`. The transient key is not a published schema and must be stripped by the existing managed-overlay save mechanism.
- **D5:** Make OpenCode background refresh credential-only. Update both `options.apiKey` and `options.headers.Authorization` for existing lucode Databricks providers, and do not add missing provider structures during refresh.
- **D6:** Set `PI_CODING_AGENT_DIR` explicitly to `PI_CONFIG_DIR` in Pi's runtime environment. Explicit assignment is more deterministic than deleting an inherited variable.
- **D7:** Preserve current default selection and provider gating behavior. The managed default path will check `opencode_managed_models` before discovered `opencode_models`; no broader default-selection redesign is in scope.

### Rejected alternatives

- Preserve the entire old OpenCode provider block without rebuilding it: rejected because lucode must still update routes, credentials, package selection, required headers, and compatibility settings.
- Merge discovered model IDs into a user-maintained map: rejected because it defeats the requested user-defined available-model list.
- Always omit discovered models as Pi does: rejected because OpenCode currently relies on discovery to bootstrap a missing model map, and removing that behavior would break first configuration.
- Use `_managed_overlay` as the managed marker: rejected by C9.
- Continue full config rewrites on every token refresh: rejected because it repeatedly exposes user-owned model definitions to merge defects and performs unnecessary writes.

### Remaining uncertainty

- **U1, low impact:** A live OpenCode version is unavailable to execute its native config parser. Disposition: preserve the existing OpenCode config shape and verify through unit tests; if an executable is independently present during implementation, run a non-requesting model-list/config-load check. Absence of the executable does not authorize installation and does not block the Python writer change.

## 4. Plan

### S1. Separate managed OpenCode inventory from discovery state

- **Outcome:** Managed policy has an unambiguous transient inventory, while persisted `opencode_models` continues to represent developer/workspace discovery state.
- **Change or evidence:**
  - Change `managed_state_overrides()` so a servable managed OpenCode list is bucketed into `opencode_managed_models`, not `opencode_models`.
  - Update `opencode_default_model()` to use `opencode_default_model`, then `opencode_managed_models`, then discovered `opencode_models`.
  - Keep unservable-model diagnostics unchanged.
  - Confirm that `resolve_state()` records the transient key under `_managed_overlay` and `save_state()` removes it when the developer had no prior value.
  - Update governing docstrings and tests to name the separate transient key and explain why equality with local discovery must still count as managed policy.
- **Targets:** `src/lucode/managed/resolve.py:managed_state_overrides`, `src/lucode/managed/resolve.py:resolve_state`, `src/lucode/agents/models.py:opencode_default_model`, `tests/test_managed_resolve.py`, `tests/test_agent_opencode.py`, and any direct assertions found by searching for `opencode_models` before editing.
- **Preconditions and dependencies:** None.
- **Shared resources:** `src/lucode/managed/resolve.py`, `src/lucode/agents/models.py`, `tests/test_managed_resolve.py`, and `tests/test_agent_opencode.py`. Serialize this step with S2 because both touch OpenCode tests and the writer consumes the new key.
- **Acceptance criteria:**
  - Managed OpenCode buckets appear only under `opencode_managed_models` in resolved memory.
  - Managed model selection works even when managed and discovered inventories contain identical values.
  - Saved workspace state retains the developer's original `opencode_models` and contains no transient managed key.
  - An entirely unservable policy still falls back to discovered models and reports the existing diagnostic.
- **Verification:** Run `.venv/bin/pytest tests/test_managed_resolve.py tests/test_agent_opencode.py -q`; expect all tests to pass, including new equal-inventory and persistence cases.

### S2. Preserve user OpenCode model maps while retaining bootstrap and managed replacement

- **Outcome:** Unmanaged rewrites preserve user model membership and metadata; managed policy remains exact; missing model maps still bootstrap from discovery.
- **Change or evidence:**
  - Read `OPENCODE_CONFIG_PATH` before calling `render_overlay()`.
  - Extend `render_overlay()` with `existing_config` and `managed_provider_models` inputs.
  - For each active provider, choose model membership using D2.
  - Copy every existing model entry before merging so no source object or sibling model shares mutable dictionaries. Replace the current Anthropic `dict.fromkeys()` construction with per-model objects.
  - Merge the current required model overlays into each selected entry. Preserve unknown user keys and non-required nested values. Required `User-Agent`, Anthropic `toolStreaming = false`, and known OSS limits win.
  - Continue replacing lucode-owned Databricks provider blocks after the overlay is fully built, preserving unrelated providers and top-level config.
  - Keep managed keys, provider IDs, package names, URLs, auth placement, and selector prefixes unchanged.
- **Targets:** `src/lucode/agents/opencode.py:render_overlay`, `_oss_model_overlay`, `write_tool_config`, `_resolve_model_selector`; `tests/test_agent_opencode.py:TestRenderOverlay` and write-config tests; existing merge helper `src/lucode/config.py:deep_merge_dict`.
- **Preconditions and dependencies:** S1 defines `opencode_managed_models` and its precedence.
- **Shared resources:** `src/lucode/agents/opencode.py` and `tests/test_agent_opencode.py`; serialize with S1 and S3.
- **Acceptance criteria:**
  - Existing Anthropic, Gemini, and OSS model maps retain exact membership and custom metadata after unmanaged writes.
  - Discovery does not add IDs when an existing valid map, including `{}`, is present.
  - Discovery populates providers whose model maps are absent or invalid, preserving first-run behavior.
  - Managed policy replaces existing and discovered IDs exactly and removes provider families excluded by policy.
  - Required model fields have the expected values after merge.
  - Unrelated providers, MCP entries, and top-level settings survive.
- **Verification:** Add focused tests for all three provider families and run `.venv/bin/pytest tests/test_agent_opencode.py tests/test_managed_resolve.py -q`; expect all tests to pass. Re-run the investigation's temporary-file characterization and expect `user-model` plus its custom fields to remain while required fields are present.

### S3. Make OpenCode token refresh credential-only

- **Outcome:** Background refresh updates Databricks credentials without rebuilding providers or changing model configuration.
- **Change or evidence:**
  - Add a helper that walks only the known lucode Databricks provider IDs and updates existing `options.apiKey` and `options.headers.Authorization` fields.
  - Preserve every other field and do not create providers or options that are absent.
  - Add `_refresh_token_in_file()` and a `token_only` branch to `_refresh_token_once()`, following Pi's established structure.
  - Make `_refresh_forever()` request token-only refresh; keep launch-time configuration as a full write.
  - Preserve warning-once retry behavior.
- **Targets:** `src/lucode/agents/opencode.py:_refresh_token_once`, `_refresh_forever`, new credential-update helper, and `tests/test_agent_opencode.py`.
- **Preconditions and dependencies:** S2 establishes the model-preserving full-write path. Implement after S2 because both edit the same module.
- **Shared resources:** `src/lucode/agents/opencode.py` and `tests/test_agent_opencode.py`.
- **Acceptance criteria:**
  - Token-only refresh changes both credential locations for existing Databricks providers.
  - Model maps, custom metadata, MCP entries, unrelated providers, and the selected model remain structurally equal before and after refresh.
  - Missing provider/auth structures are not invented.
  - Launch-time `_refresh_token_once()` still performs a complete configuration write.
- **Verification:** Run the new token-refresh tests plus the existing warning retry test with `.venv/bin/pytest tests/test_agent_opencode.py -q`; expect all tests to pass.

### S4. Pin Pi's explicit agent directory

- **Outcome:** Pi always reads lucode's `models.json` regardless of an inherited `PI_CODING_AGENT_DIR`.
- **Change or evidence:**
  - Set `env["PI_CODING_AGENT_DIR"] = str(PI_CONFIG_DIR)` in `build_runtime_env()` alongside `HOME`.
  - Add a regression test that seeds a conflicting inherited value and verifies the returned environment uses `PI_CONFIG_DIR`.
  - Update the nearby module/runtime comment to state why both variables are set.
- **Targets:** `src/lucode/agents/pi.py:build_runtime_env`, `tests/test_agent_pi.py:TestBuildRuntimeEnv`.
- **Preconditions and dependencies:** None. It can be implemented in parallel with S1-S3 because it uses separate files.
- **Shared resources:** `src/lucode/agents/pi.py` and `tests/test_agent_pi.py` only.
- **Acceptance criteria:** `build_runtime_env()` overwrites a conflicting inherited `PI_CODING_AGENT_DIR` with `PI_CONFIG_DIR` while retaining `HOME` and `OAUTH_TOKEN` behavior.
- **Verification:** Run `.venv/bin/pytest tests/test_agent_pi.py -q`; expect all tests to pass. Repeat the isolated Pi `models.json` diagnostic with a conflicting parent variable and verify Pi lists the models from the explicitly configured lucode directory.

### S5. Synchronize documentation and complete verification

- **Outcome:** Code-associated documentation states ownership and precedence accurately, and the complete repository check finds no regression.
- **Change or evidence:**
  - Update `src/lucode/agents/opencode.py`'s module and function documentation to describe user-map preservation, discovery fallback, managed replacement, and credential-only refresh.
  - Update Pi's runtime comment for the explicit agent directory.
  - Add a concise README statement near managed local files or model discovery explaining that existing user model inventories are preserved unless workspace policy supplies an exact inventory. Keep OpenCode's native `opencode.json` and Pi's native `models.json` distinct.
  - Apply the repository's update-annotation completion check during implementation so comments and README text remain evidence-backed and additive.
  - Review the final diff and exclude `uv.lock` and unrelated formatting.
- **Targets:** `src/lucode/agents/opencode.py`, `src/lucode/agents/pi.py`, `README.md:118-145`, focused tests, and final diff.
- **Preconditions and dependencies:** S1-S4 complete.
- **Shared resources:** Documentation in agent modules overlaps prior steps; make final prose edits after functional changes settle.
- **Acceptance criteria:** Documentation matches tested behavior; no secret values or stale references are added; only requested files appear in the intended implementation diff.
- **Verification:** Run the commands in Section 7. Inspect `git diff -- src/lucode/agents/opencode.py src/lucode/agents/pi.py src/lucode/agents/models.py src/lucode/managed/resolve.py tests/test_agent_opencode.py tests/test_agent_pi.py tests/test_managed_resolve.py README.md` and confirm each hunk maps to this plan.

## 5. Coverage

| Source item | Coverage |
|---|---|
| Pi loads user-defined `models.json` inventories | Confirmed by C1 and prior runtime diagnostic; protected by S4 and its regression test |
| Inherited `PI_CODING_AGENT_DIR` can bypass lucode's isolated Pi home | S4 |
| OpenCode deletes user-defined model membership and metadata | S2 characterization test and implementation |
| Preserve user model maps for Anthropic, Gemini, and OSS | S2 |
| Keep gateway-required model fields | D3, S2 |
| Keep discovery bootstrap when no model map exists | D2, S2 |
| Managed inventories must replace local inventory exactly | S1 and S2 |
| Managed and discovered inventories need distinct representation | D4, S1 |
| `_managed_overlay` equality gap | C9, addressed by S1 |
| Background refresh must not rewrite models | D5, S3 |
| Preserve unrelated providers, MCP, top-level settings, selectors, and backups | S2 and S3 acceptance tests |
| Update code comments and user documentation | S5 |
| Do not modify upstream agents or install dependencies | Non-goals; final diff and command review in S5 |
| No Git submodule exists | Context only; no implementation step needed |
| Live OpenCode runtime unavailable | U1 and Section 7 `not verified` entry |
| Existing `uv.lock` modification | Workspace constraint; S5 excludes it from the intended diff |
| Focused baseline is green | C11 and Section 7 |
| Full regression, lint, and optional live checks | S5 and Section 7 |
| Default-model redesign | Explicitly deferred by D7 and non-goals; current precedence retained except for the separate managed key |
| Migration/backfill | Not required; next normal config write and token refresh apply behavior without schema migration |
| Rollback/recovery | Section 8; revert the scoped source change and rely on existing config backups |
| Observability | No new logs required; existing warning-once refresh failure behavior is retained and tested in S3 |

## 6. Delegation and sequencing

- **Parallel group P1:** S1 and S4 may proceed in parallel. S1 may modify managed resolution and model selection; S4 is limited to Pi runtime environment and tests.
- **Serial group P2:** S2 follows S1 because it consumes `opencode_managed_models`.
- **Serial group P3:** S3 follows S2 because both change `opencode.py` and `test_agent_opencode.py`.
- **Final group P4:** S5 follows all functional work and owns documentation synchronization and final verification.
- Any delegated review of P2/P3 should independently check provider-diverse behavior across Anthropic, Gemini, and OSS, with emphasis on dictionary merge precedence and credential-only writes. The implementation session must apply the then-current sub-agent policy and must not preselect an agent or model from this plan.

## 7. Verification

### Baseline already run

- `.venv/bin/pytest tests/test_agent_pi.py tests/test_agent_opencode.py tests/test_managed_resolve.py -q`
  - Result: `143 passed in 0.44s`.
  - No baseline failure is known in these files.
- `.venv/bin/ruff check src/lucode/agents/pi.py src/lucode/agents/opencode.py src/lucode/managed/resolve.py tests/test_agent_pi.py tests/test_agent_opencode.py tests/test_managed_resolve.py`
  - Result: `All checks passed!`.
- Temporary Pi custom-model diagnostic under Pi `0.84.4`:
  - Result: both configured custom models appeared in `pi --list-models` when the intended agent directory was selected.
- Temporary OpenCode writer characterization:
  - Result: the existing `user-model` was removed and replaced by the discovered model, confirming the regression before implementation.

### Required implementation checks

1. **Focused behavior:**
   - `.venv/bin/pytest tests/test_agent_pi.py tests/test_agent_opencode.py tests/test_managed_resolve.py -q`
   - Covers S1-S4 and all success criteria tied to model precedence, state persistence, refresh, and Pi environment pinning.
   - Expected: all tests pass.
2. **Affected caller regression:**
   - `.venv/bin/pytest tests/test_agents_init.py tests/test_cli.py -q`
   - Covers managed launch/default selection and configuration dispatch callers.
   - Expected: all tests pass with no changed public CLI behavior.
3. **Full suite:**
   - `.venv/bin/pytest -q`
   - Covers repository-wide config, state, managed policy, MCP, and launch regressions.
   - Expected: all tests pass. If failures occur, compare them with a same-worktree pre-change baseline before classifying them.
4. **Lint:**
   - `.venv/bin/ruff check .`
   - Expected: `All checks passed!`.
5. **Pi runtime diagnostic:**
   - Use a temporary directory containing a non-secret test `models.json`, deliberately seed a conflicting parent `PI_CODING_AGENT_DIR`, construct the environment through `build_runtime_env()`, and run `pi --list-models` with network access disabled.
   - Expected: only the temporary lucode-directory test models are selected from that config.
6. **OpenCode writer diagnostic:**
   - Repeat the temporary-file characterization for all three provider families without real credentials.
   - Expected: user membership and custom metadata survive; required overlays are present; token-only refresh changes credentials only.
7. **Optional live OpenCode parser check:**
   - If `command -v opencode` succeeds without installation, inspect `opencode --help` to identify its non-requesting model/config listing command, run it against a temporary XDG config, and confirm no schema error.
   - Expected: the config loads and lists the retained test models without sending an inference request.
8. **Diff hygiene:**
   - `git status --short` and scoped `git diff` review.
   - Expected: intended implementation files only; the pre-existing `uv.lock` modification remains separate and untouched.

`not verified: live OpenCode config parsing and model listing because no OpenCode executable is installed in the planning environment.`

`not verified: live Databricks inference because it is unnecessary for config-preservation logic and would require credentials and network services.`

## 8. Risks and assumptions

- **R1: Merge precedence could retain unsafe gateway settings.** Impact: requests may fail if user values override required compatibility fields. Mitigation: D3 makes only the identified required fields lucode-owned and tests their precedence for each provider. Owner: implementation reviewer.
- **R2: Treating `{}` as authoritative yields no selectable models for that provider.** Impact: a user who accidentally empties a map sees no models rather than silent repopulation. Mitigation: this is the reversible default consistent with an explicit user-defined allowlist; deleting the `models` key restores discovery bootstrap. Owner: user/config author.
- **R3: Separate managed state could leak into persisted state.** Impact: admin policy could overwrite or contaminate developer discovery state. Mitigation: S1 adds persistence tests for both prior-value and no-prior-value cases. Owner: implementation reviewer.
- **R4: Credential-only refresh could miss one auth location.** Impact: OpenCode may use an expired key or Authorization header. Mitigation: update and assert both existing fields, preserving the current dual-auth shape. Owner: implementation reviewer.
- **R5: No live OpenCode executable is available.** Impact: unit tests may miss an upstream schema change. Mitigation: preserve the already-generated schema shape, avoid speculative fields, and run the optional parser check only if the executable is independently available. Evidence needed to remove the risk: a successful native non-requesting config-load check.
- **R6: Existing `uv.lock` modification may be accidentally included.** Impact: unrelated dependency/lock churn enters the change. Mitigation: do not run lock-mutating commands, do not revert owner work, and use scoped diff/staging review. Owner: implementation session.
- **R7: Rollback and recovery.** Impact: a faulty merge could alter generated config. Mitigation: existing backup behavior remains unchanged; rollback is the scoped source revert, and `lucode revert` can restore the user's backed-up managed file where appropriate. No data migration or backfill is required.

## 9. Open questions or blockers

No material blocker remains.

- **Low-impact follow-up:** If an OpenCode executable becomes available, perform the optional native parser check in Section 7. Default if unavailable: rely on focused writer tests and report the integration path as not verified.
