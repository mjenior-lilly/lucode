# MCP package split implementation plan

## 1. Status and context

**Status: Implementation-ready**

### Planning sources

- User request in the current session: remove the `mcp_` prefix from files moved under `src/lucode/mcp/`, split the 1,624-line MCP registration module into maintainable submodules, and update all imports.
- Latest investigation in the current session, which identified the broken migration, six responsibility groups, direct consumers, stale documentation, and missing characterization coverage.
- Current repository evidence at revision `2d048b8a14e3c7a178f5f56ffe6bd5a79b8f348a`.

### Repository and workspace

- Repository root: `/Users/L146025/Library/CloudStorage/OneDrive-EliLillyandCompany/Desktop/repos/lucode`
- Relevant dirty state: tracked `src/lucode/mcp.py`, `src/lucode/mcp_proxy.py`, and `AGENTS.md` are deleted; untracked `src/lucode/mcp/mcp.py` and `src/lucode/mcp/mcp_proxy.py` contain byte-identical copies of the MCP files. `AGENTS.md` disappeared during planning and its deletion intent is unknown.
- The moved files and deleted `AGENTS.md` are user work. Preserve the moved contents while redistributing them. Do not restore `AGENTS.md` or overwrite unrelated work without explicit confirmation.
- No dependency, lockfile, generated-file, version, persisted-data, or schema change is required.

## 2. Goal and scope

### Goal

Complete the MCP package migration so the existing CLI and managed-configuration flows behave as before while MCP implementation concerns live in focused modules under `lucode.mcp`.

### User-visible behavior

The following behavior must remain unchanged:

- `lucode configure mcp`, including `--location`, `--services`, source selection, picker back-navigation, add/remove summaries, and cancellation behavior.
- `lucode configure skills` and skill-download registration, including the single skills-entry invariant and schema-less registration.
- `lucode mcp-proxy`, including OAuth/PAT preflight, per-request token refresh, stderr-only auth diagnostics, and exit code 2 for terminal authentication failures.
- OpenCode MCP configuration, cross-workspace cleanup, state persistence, warning/error wording, and the existing `mcp_servers` state shape.

### Success criteria

1. `src/lucode/mcp/` is a regular Python package with `__init__.py` and no aggregate exports.
2. No source file under that package is named `mcp.py` or starts with `mcp_`; `mcp_proxy.py` becomes `proxy.py`.
3. The former monolith is divided into `config.py`, `resources.py`, `picker.py`, `commands.py`, and `skills.py` with the dependency direction defined in this plan.
4. Production and test code import each symbol from its owning submodule. No `from lucode.mcp import ...`, `lucode.mcp_proxy`, `from lucode import mcp`, or `from lucode import mcp_proxy` references remain.
5. CLI contracts, state formats, MCP URLs, client argv, messages, and authentication behavior do not change.
6. Focused tests, import smoke checks, lint, formatting, and type checks pass. The full suite introduces no failures beyond any recorded baseline failures.

### Non-goals

- Do not rename MCP-prefixed constants, state keys, API paths, or the `mcp-proxy` CLI command.
- Do not change Databricks API behavior in `lucode.databricks.mcp_discovery`.
- Do not change supported MCP clients, add compatibility wrappers, add aggregate package exports, or preserve the old Python import paths.
- Do not redesign the picker, alter user-facing copy, change concurrency semantics, change authentication, or refactor unrelated CLI, managed-config, or skill-download behavior.
- Do not change dependencies, versions, `uv.lock`, generated files, persisted state, or configuration schemas.

### Smallest complete change

Create the package initializer, rename the proxy module, extract the monolith into five responsibility-based modules, update direct consumers and tests, add focused characterization coverage for high-risk moved logic, update two stale architecture references, and delete the redundant moved monolith.

## 3. Evidence and decisions

### Confirmed evidence

- **confirmed:** `src/lucode/mcp/mcp.py` is 1,624 lines with 61 top-level functions and two classes. Stable locators include `configure_client_mcp_server`, `discover_all_mcp_service_names`, `_scrolling_checkbox`, `apply_mcp_server_changes`, `configure_mcp_command`, and `configure_skills_mcp_command`.
- **confirmed:** `src/lucode/mcp/mcp_proxy.py` is 170 lines and owns only the stdio/HTTP bridge and authentication behavior. Its public surface is `AUTH_FAILURE_EXIT_CODE`, `ProxyAuthError`, and `serve` through `__all__`.
- **confirmed:** The moved files are byte-identical to `HEAD:src/lucode/mcp.py` and `HEAD:src/lucode/mcp_proxy.py`; command output reported `byte_identical=True` for both.
- **confirmed:** Production consumers are `src/lucode/cli.py:72`, `src/lucode/cli.py:mcp_proxy_cmd`, `src/lucode/skills_download.py:13`, and `src/lucode/managed/wizard.py:39`.
- **confirmed:** Direct test imports are `tests/test_mcp.py:7`, `tests/test_mcp_proxy.py:12`, and `tests/test_managed_wizard.py:94`.
- **confirmed:** The current checkout cannot collect the full focused MCP tests. `uv run --frozen pytest tests/test_mcp.py tests/test_mcp_proxy.py --collect-only -q` collected 13 registration tests, then failed because `lucode.mcp_proxy` no longer exists.
- **confirmed:** Importing `lucode.cli`, `lucode.skills_download`, or `lucode.managed.wizard` currently fails because the namespace directory has no aggregate symbols and no `__init__.py`.
- **confirmed:** Existing test coverage consists of 13 tests in `tests/test_mcp.py` and 20 proxy tests in `tests/test_mcp_proxy.py`. The registration tests do not directly cover state reconciliation, picker orchestration, location replacement, or skills updates.
- **confirmed:** `HEAD:AGENTS.md:23` assigns MCP state and picker ownership to root `mcp.py`. The working-tree file was readable with that text early in planning, then became deleted; current `git status --short` reports `D AGENTS.md`.
- **confirmed:** `src/lucode/managed/config.py` contains a comment that points to selection prefixes in `mcp.py`.
- **confirmed:** `src/lucode/managed/__init__.py` and `src/lucode/databricks/__init__.py` establish the repository convention of importing from owning submodules without aggregate exports.
- **confirmed:** Repository and README searches found no documented public Python API for `lucode.mcp` or `lucode.mcp_proxy`; only internal source and tests import those paths. The published entry point is `lucode.cli:main` in `pyproject.toml`.

### Decisions

- **D1:** Use `mcp/__init__.py` for package documentation only. Do not re-export symbols. This follows the managed and Databricks package convention and makes module ownership explicit. The tradeoff is that undocumented Python imports of the old modules break, which the user-requested migration already implies.
- **D2:** Use six implementation modules: `config.py`, `resources.py`, `picker.py`, `commands.py`, `skills.py`, and `proxy.py`. This is the smallest split that separates state/client mutation, resource discovery, interactive UI, command orchestration, skill registration, and transport proxy behavior.
- **D3:** Keep `lucode.databricks.mcp_discovery` as the HTTP/API owner. `lucode.mcp.resources` will adapt those API results into MCP server entries and warnings, avoiding a second transport layer.
- **D4:** Rename helpers that become cross-module dependencies instead of importing underscore-prefixed symbols across module boundaries: `_server_name` to `server_name`, `_servers_by_name` to `servers_by_name`, `_mcp_service_entry_name` to `mcp_service_entry_name`, `_mcp_service_original_for_full_name` to `find_mcp_service_entry`, `_catalog_schema_server_name` to `catalog_schema_server_name`, `_resolve_mcp_selection` to `resolve_mcp_selection`, `_skills_entries` to `skills_entries`, and `_skill_mcp_locations` to `skill_mcp_locations`.
- **D5:** Preserve function bodies, messages, state keys, URL construction, operation ordering, and exception behavior unless an import boundary requires a mechanical qualification change.
- **D6:** Split `tests/test_mcp.py` by owner because the implementation split is specifically for maintainability. Keep `tests/test_mcp_proxy.py` under its current filename because it describes the CLI feature, but import `lucode.mcp.proxy`.

### Uncertainty and disposition

- **uncertain, low impact:** Unobserved external Python consumers may import `lucode.mcp` or `lucode.mcp_proxy`. Repository search found no documented public Python API. Reversible default: do not add compatibility shims or aggregate exports, as requested and required by repository change policy. Rollback is restoring the old modules and imports.
- **uncertain, bounded reconnaissance:** The current dirty migration prevents a clean focused baseline. Step S1 runs the focused baseline from a temporary clean checkout at the recorded revision and records any failures before implementation. This unblocks regression classification in S8.
- **uncertain, low impact:** The deletion of `AGENTS.md` may be intentional user work. Reversible default: do not restore it. Step S6 updates its stale MCP ownership text only if the user restores the file before implementation; otherwise report that the governing document was not updated because it remains deleted.

## 4. Plan

### S1. Establish a clean behavioral baseline

- **Title:** Run focused MCP tests from the recorded revision.
- **Outcome:** Record the pre-migration behavior and failures without changing the dirty working tree.
- **Change or evidence:** Create a temporary clean checkout or archive of revision `2d048b8a14e3c7a178f5f56ffe6bd5a79b8f348a`; run the focused tests there; remove the temporary checkout afterward. Do not modify target code.
- **Targets:** `tests/test_mcp.py`, `tests/test_mcp_proxy.py`, `tests/test_cli.py`, `tests/test_skills_download.py`, `tests/test_managed_wizard.py`, `tests/test_agent_opencode.py`.
- **Preconditions and dependencies:** None.
- **Shared resources:** The recorded revision and test environment. No target-file write conflict.
- **Acceptance criteria:** Focused baseline results and any environment-dependent failures are recorded with exact commands and output summaries.
- **Verification:** Run `uv run --frozen pytest tests/test_mcp.py tests/test_mcp_proxy.py tests/test_cli.py tests/test_skills_download.py tests/test_managed_wizard.py tests/test_agent_opencode.py -q` in the temporary clean checkout. Expected result: either all focused tests pass or each failure is recorded as pre-existing before S2.

### S2. Create the package and rename the proxy

- **Title:** Establish `lucode.mcp` and move the proxy to its owner path.
- **Outcome:** `lucode.mcp.proxy` provides the unchanged proxy behavior; the package has no aggregate exports.
- **Change or evidence:** Add `src/lucode/mcp/__init__.py` with a concise ownership docstring. Rename `src/lucode/mcp/mcp_proxy.py` to `src/lucode/mcp/proxy.py` without changing implementation. Update `src/lucode/cli.py:mcp_proxy_cmd` and `tests/test_mcp_proxy.py` to import from `lucode.mcp.proxy`.
- **Targets:** `src/lucode/mcp/__init__.py`, `src/lucode/mcp/proxy.py`, `src/lucode/cli.py:mcp_proxy_cmd`, `tests/test_mcp_proxy.py`.
- **Preconditions and dependencies:** S1 baseline recorded.
- **Shared resources:** `src/lucode/cli.py` is also changed in S6, so one implementer must own or serialize edits to it.
- **Acceptance criteria:** No `src/lucode/mcp/mcp_proxy.py` or root `src/lucode/mcp_proxy.py` remains; proxy tests use the new owner path; proxy code and `__all__` behavior are otherwise unchanged.
- **Verification:** Run `uv run --frozen pytest tests/test_mcp_proxy.py -q` and an import smoke check for `from lucode.mcp.proxy import serve`. Expected result: all proxy tests and the import pass.

### S3. Extract configuration/state and resource discovery leaves

- **Title:** Create the low-level MCP modules.
- **Outcome:** Client configuration and state reconciliation live in `config.py`; Databricks resource adaptation lives in `resources.py`; neither imports picker, commands, or skills.
- **Change or evidence:** Extract into `config.py`: `MCP_USER_SCOPE`, `MCP_CLIENTS`, client availability/configure/remove/revert functions, server-name/index helpers, workspace partition helpers, service-entry naming and lookup, `_Counter`, `apply_mcp_server_changes`, `purge_cross_workspace_mcp_residue`, and `setup_mcp_clients`. Extract into `resources.py`: connection-marker parsing, external connection discovery, MCP service discovery, Genie/app/Vector Search/UC Functions transforms and discovery, warning classification, and catalog/schema naming. Apply D4 names where those helpers cross modules.
- **Targets:** `src/lucode/mcp/config.py`; `src/lucode/mcp/resources.py`; source symbols in `src/lucode/mcp/mcp.py` named above.
- **Preconditions and dependencies:** S2 package exists.
- **Shared resources:** Both extractions read `src/lucode/mcp/mcp.py`; serialize edits if extraction is done in place. `config.py` owns state and client mutation symbols for S4 through S6.
- **Acceptance criteria:** `config.py` and `resources.py` import successfully; their dependency graph points only to existing top-level/Databricks/agent/UI modules; copied behavior and messages remain unchanged; no duplicate authoritative implementation remains after S6.
- **Verification:** Run `uv run --frozen python -m py_compile src/lucode/mcp/config.py src/lucode/mcp/resources.py` and focused tests added in S7. Expected result: compilation succeeds; behavior tests pass after S7.

### S4. Extract the picker

- **Title:** Isolate interactive MCP selection UI.
- **Outcome:** Prompt-toolkit/questionary logic and selection resolution live in `picker.py`, separate from API discovery and state writes.
- **Change or evidence:** Move `_Back`, `_BACK`, picker constants, selection prefixes, `_picker_style`, `_scrolling_checkbox`, choice construction, picker prompts, `MCP_SEARCH_SOURCES`, source-selection prompt, and selection resolution into `picker.py`. Import normalized resource functions from `resources.py` and server-entry helpers from `config.py`. Rename `resolve_mcp_selection` and `catalog_schema_server_name` per D4.
- **Targets:** `src/lucode/mcp/picker.py`; `src/lucode/mcp/mcp.py:_Back`; `build_mcp_picker_choices`; `prompt_for_mcp_server_choices`; `_resolve_mcp_selection`; `prompt_for_mcp_search_sources`.
- **Preconditions and dependencies:** S3 provides `config.py` and `resources.py` ownership boundaries.
- **Shared resources:** Reads the remaining `src/lucode/mcp/mcp.py`; depends on D4 helper names shared with S3.
- **Acceptance criteria:** `picker.py` performs no state persistence or direct client configuration; all existing instructions, key bindings, back/cancel sentinel behavior, choice ordering, and selection URL/name behavior are preserved.
- **Verification:** Run `uv run --frozen python -m py_compile src/lucode/mcp/picker.py`; then run picker tests added in S7. Expected result: compilation and tests pass.

### S5. Extract skills and command orchestration, then remove the monolith

- **Title:** Complete the responsibility split.
- **Outcome:** Skills registration lives in `skills.py`; `configure mcp` orchestration lives in `commands.py`; no `mcp.py` remains.
- **Change or evidence:** Move `SKILLS_MCP_KIND`, `SKILLS_MCP_SERVER_NAME`, skills entry helpers, summaries, update logic, `configure_skills_mcp_command`, `skill_mcp_locations`, and `register_schemaless_skills_connection` into `skills.py`. Move discovery wrappers/progress, selected-source orchestration, location replacement, `configure_mcp_command`, and change summary into `commands.py`. `commands.py` may depend on `config`, `resources`, `picker`, and the skills constants/helpers; `skills.py` may depend on `config`, but must not import `commands.py`. Delete `src/lucode/mcp/mcp.py` after every symbol has exactly one owner.
- **Targets:** `src/lucode/mcp/skills.py`; `src/lucode/mcp/commands.py`; `src/lucode/mcp/mcp.py`; symbols `configure_mcp_command`, `_resolve_location_mcp_servers`, `_discover_selected_mcp_sources`, `configure_skills_mcp_command`, and `register_schemaless_skills_connection`.
- **Preconditions and dependencies:** S3 and S4 complete.
- **Shared resources:** `src/lucode/mcp/mcp.py` and cross-module helper names. This step must be serialized with S3/S4 extraction and finish only after their ownership lists are reconciled.
- **Acceptance criteria:** The package contains only `__init__.py`, `config.py`, `resources.py`, `picker.py`, `commands.py`, `skills.py`, and `proxy.py`; there is no implementation duplication; internal imports follow `config/resources -> picker/skills -> commands` without cycles.
- **Verification:** Run an AST/import smoke script importing all seven package modules and checking that `find src/lucode/mcp -maxdepth 1 -name 'mcp*.py'` returns no prefixed/redundant implementation files except none. Expected result: every import succeeds and the filename search is empty.

### S6. Update production consumers and architecture annotations

- **Title:** Point each caller to the owning submodule.
- **Outcome:** CLI, managed setup, and skill download import directly from owners; documentation names the final package boundaries.
- **Change or evidence:** In `src/lucode/cli.py`, import `configure_mcp_command` from `commands`, client constants/cleanup/revert from `config`, and skills constants/command from `skills`. In `src/lucode/skills_download.py`, import `setup_mcp_clients` from `config` and schema-less registration from `skills`. In `src/lucode/managed/wizard.py`, import the MCP command from `commands` and skills symbols from `skills`, using `skill_mcp_locations`. Inspect the working-tree status of `AGENTS.md`; if the user has restored it, update its MCP ownership text, and if it remains deleted, leave it untouched and report the omitted documentation update. Update the stale `mcp.py` reference in `src/lucode/managed/config.py` to `lucode.mcp.picker`.
- **Targets:** `src/lucode/cli.py`; `src/lucode/skills_download.py`; `src/lucode/managed/wizard.py`; conditional `AGENTS.md`; `src/lucode/managed/config.py`.
- **Preconditions and dependencies:** S5 establishes final owner paths and names.
- **Shared resources:** `src/lucode/cli.py` is shared with S2. `src/lucode/managed/wizard.py` is shared with its tests in S7; serialize source and monkeypatch-target changes.
- **Acceptance criteria:** No production import uses package-level MCP symbols or old root module paths; CLI command names/options and function call signatures remain unchanged; annotations match final ownership and add no duplicate documentation.
- **Verification:** Run `uv run --frozen python -c 'import lucode.cli, lucode.skills_download, lucode.managed.wizard'`. Expected result: all imports succeed.

### S7. Reorganize and strengthen focused tests

- **Title:** Align tests with module ownership and protect moved behavior.
- **Outcome:** Tests patch dependencies where looked up and directly cover high-risk behavior before the refactor is accepted.
- **Change or evidence:** Split `tests/test_mcp.py` into focused owner tests, using `tests/test_mcp_config.py`, `tests/test_mcp_resources.py`, `tests/test_mcp_picker.py`, `tests/test_mcp_commands.py`, and `tests/test_mcp_skills.py` where each file has meaningful coverage. Move existing assertions without changing expectations. Update `tests/test_managed_wizard.py` to import `SKILLS_MCP_KIND` from `lucode.mcp.skills`. Add characterization tests for per-client serial application, cross-workspace cleanup/state persistence, command add/remove reconciliation, location replacement preserving skills, skills replacement/client merging, and picker back-navigation. Patch the module where the dependency is resolved, not an aggregate `mcp` module.
- **Targets:** Existing `tests/test_mcp.py`, `tests/test_mcp_proxy.py`, `tests/test_managed_wizard.py`; new focused MCP test files; production symbols listed in S3 through S5.
- **Preconditions and dependencies:** S3 through S6 provide final module paths. Existing expectations and S1 baseline define behavior.
- **Shared resources:** Test fixtures and monkeypatch targets mirror final ownership. If delegated, one worker must coordinate removal of `tests/test_mcp.py` to avoid duplicated tests.
- **Acceptance criteria:** No test imports old paths or aggregate MCP symbols; each new module has direct tests for its externally consumed or high-risk behavior; all moved expectations retain their original values and messages.
- **Verification:** Run `uv run --frozen pytest tests/test_mcp_config.py tests/test_mcp_resources.py tests/test_mcp_picker.py tests/test_mcp_commands.py tests/test_mcp_skills.py tests/test_mcp_proxy.py tests/test_managed_wizard.py -q`. Expected result: all tests pass.

### S8. Validate the complete migration

- **Title:** Run repository checks and compare with baseline.
- **Outcome:** The package split is mechanically complete and introduces no regression relative to S1.
- **Change or evidence:** Run targeted search, import, test, lint, formatting, and type checks. Review the final diff for accidental behavior changes, stale annotations, duplicate implementations, compatibility shims, generated/lockfile edits, and unrequested formatting.
- **Targets:** Entire final diff; package and consumers from S2 through S7.
- **Preconditions and dependencies:** S1 through S7 complete.
- **Shared resources:** Full working tree and test environment. No parallel writes during final review.
- **Acceptance criteria:** Success criteria 1 through 6 hold; every new failure is fixed or precisely distinguished from S1; `uv.lock` and versions are unchanged.
- **Verification:** Run the commands in Section 7. Expected result: targeted searches are empty, focused checks pass, and the full suite has no failures beyond recorded baseline failures.

### Migration, rollout, rollback, and recovery

- Python import paths change directly; no runtime state migration or backfill is needed because `mcp_servers` serialization remains unchanged.
- Rollout is the normal package release process. No feature flag or staged deployment is needed for a structural refactor with unchanged CLI behavior.
- Rollback restores root `mcp.py` and `mcp_proxy.py` plus their original imports. Persisted state remains readable because this plan does not change its shape.
- No new observability is needed. Existing warnings, errors, and proxy stderr behavior must remain byte-for-byte equivalent where tests assert them.

## 5. Coverage

| Source item | Disposition |
|---|---|
| Remove `mcp_` from moved filenames | S2 renames `mcp_proxy.py`; S5 removes redundant `mcp.py`; S8 filename search verifies completion. |
| Split massive `mcp.py` | D2 and S3 through S5 create five responsibility modules. |
| Update all imports | S2, S6, and S7; S8 stale-path searches verify. |
| Current broken CLI/skill/managed imports | S6 and its import smoke check. |
| Proxy import failure | S2 and `tests/test_mcp_proxy.py`. |
| Separate client/state mutation | S3 `config.py`. |
| Separate resource discovery | S3 `resources.py`, retaining Databricks transport ownership under D3. |
| Separate interactive picker | S4 `picker.py`. |
| Separate command orchestration | S5 `commands.py`. |
| Separate skills registration | S5 `skills.py`. |
| No aggregate exports | D1, S2 package initializer, S8 import review. |
| Cross-module private helper issue | D4 and S3 through S5. |
| Preserve CLI, auth, messages, state, URL, and concurrency behavior | D5; S1 baseline; S7 characterization; S8 regression comparison. |
| Existing coverage gap | S7 adds six high-risk behavior groups. |
| Split tests for maintainability | D6 and S7. |
| Update stale `AGENTS.md` ownership text | S6 updates it only if the user restores the file; otherwise the confirmed deletion is preserved and reported. |
| Update stale managed-config comment | S6. |
| Keep `mcp-proxy` command and MCP terminology | Non-goals; S2 proxy tests and S8 diff review. |
| No schema/dependency/version/lock changes | Scope and non-goals; S8 diff review. |
| Potential undocumented external import consumers | Uncertain item with no-shim reversible default; rollback section. |
| Dirty tree prevents clean baseline | S1 temporary clean-checkout reconnaissance. |
| `AGENTS.md` disappeared during planning | Uncertain low-impact item; S6 preserves the deletion unless the user restores it. |
| Full behavior verification | S8; no behavior is claimed verified by this plan. |

## 6. Delegation and sequencing

- **Serial foundation:** S1, then S2. S2 establishes the package and proxy path used by later work.
- **Potential parallel read/extraction analysis:** After S2, separate implementers may prepare `config.py` and `resources.py`, but writes that remove content from `mcp.py` must be serialized because both use the same source file.
- **Serial integration:** S4 and S5 follow S3 because they depend on final helper owners and names. S5 performs the definitive monolith removal.
- **Potential parallel consumer/test work:** After S5, S6 production consumer updates and S7 test-file preparation may proceed in parallel only if shared edits to `src/lucode/managed/wizard.py`, `tests/test_managed_wizard.py`, and `src/lucode/cli.py` have explicit ownership. Final monkeypatch targets depend on S6 import locations.
- **Final serial gate:** S8 runs after all writes finish.
- Minimum handoff context includes D1 through D6, the owner table implicit in S3 through S5, exact renamed helper names, and S1 baseline output.
- No security-sensitive or destructive implementation is planned. An independent review is still useful for import-cycle detection and behavior-preservation of auth, state writes, and concurrent client operations; the implementation session should apply its current delegation policy.

## 7. Verification

### Recorded current diagnostic

- Command: `uv run --frozen pytest tests/test_mcp.py tests/test_mcp_proxy.py --collect-only -q`
- Result: 13 registration tests collected; collection then failed at `tests/test_mcp_proxy.py:12` because `lucode.mcp_proxy` no longer exists.
- Classification: caused by the current incomplete file migration, before the planned implementation.

### Required checks

| Check | Expected result | Coverage |
|---|---|---|
| S1 focused baseline in temporary clean checkout | Passes or records exact pre-existing failures | Regression reference for S2 through S8 |
| `uv run --frozen pytest tests/test_mcp_proxy.py -q` | All proxy tests pass | S2, proxy path and behavior |
| New focused MCP module tests from S7 | All pass | S3 through S7 ownership and characterization |
| `uv run --frozen pytest tests/test_cli.py tests/test_skills_download.py tests/test_managed_wizard.py tests/test_agent_opencode.py -q` | All pass | Consumer imports and production-reachable behavior |
| Import smoke for every `lucode.mcp` submodule and direct consumer | All imports succeed | S2 through S6, no cycles |
| `uv run --frozen ruff check .` | Passes | Imports/style across final tree |
| `uv run --frozen ruff format --check src/ tests/` | Passes or only S1-recorded unrelated formatting failures remain | Formatting |
| `uv run --frozen ty check src/` | Passes | Cross-module types/imports |
| `uv run --frozen pytest` | No failures beyond S1 baseline | Repository regression gate |
| `grep -R "from lucode\.mcp import\|lucode\.mcp_proxy\|from lucode import mcp\|from lucode import mcp_proxy" -n src tests` | No matches | Old/aggregate import removal |
| `find src/lucode/mcp -maxdepth 1 \( -name 'mcp.py' -o -name 'mcp_*.py' \)` | No matches | Filename migration |
| `git diff --check` and final diff review | Clean; no lock/version/generated/unrelated changes | Scope control and annotation completion |

### Manual and integration checks

- No live Databricks workspace test is required solely for module movement if all source bodies and contracts remain unchanged and focused tests cover orchestration boundaries.
- `not verified: live MCP discovery and registration require workspace credentials and network access that are not available as planning evidence.`
- `not verified: end-to-end stdio proxy traffic against a Databricks MCP endpoint requires external infrastructure; existing proxy transport tests remain the highest-fidelity local check.`
- No migration, backfill, feature-flag, or observability validation is required because the plan preserves persisted and CLI contracts.

## 8. Risks and assumptions

| Risk or assumption | Impact | Mitigation / evidence | Reversible default |
|---|---|---|---|
| Cross-module extraction changes lookup locations used by monkeypatches | Tests may patch the wrong module while production behavior silently differs | S7 patches where dependencies are resolved; S8 runs production-reachable caller tests | Keep direct module imports and adjust tests to final owners |
| Circular imports between `commands`, `skills`, `picker`, and `config` | CLI import failure | Enforce dependency order from D2/S5 and run all-module import smoke checks | Move only shared constants/helpers downward into `config`; do not add aggregate exports |
| Concurrent config operations are accidentally parallelized within one client | Lost client configuration writes | Preserve `apply_mcp_server_changes` body and add serial-per-client characterization | Retain current thread-pool structure unchanged |
| Skills entries are dropped by normal MCP configuration | Existing skill tools disappear | Keep skills constants/helpers in `skills`, preserve entries in location and picker flows, add tests | Preserve current `kind == "skills"` filtering exactly |
| External consumers use undocumented old Python paths | Import break outside repository | User explicitly requested migration; repository search found no documented public Python API | No shim; rollback restores old modules if downstream evidence appears |
| Source behavior changes during extraction | CLI or state regression | Byte-identical source evidence, S1 baseline, focused characterization, final diff review | Prefer mechanical movement over cleanup |
| Dirty moved files are overwritten | Loss of user work | Both moved files are confirmed byte-identical to HEAD; preserve content and avoid checkout/revert on them | Stop if content changes during implementation and re-run comparison |
| Deleted `AGENTS.md` is restored without approval | Overwrites potentially intentional user work | S6 checks current status and leaves the deletion untouched by default | Update only if the user restores it before implementation |

## 9. Open questions or blockers

No material blocker remains.

Low-impact follow-up: if downstream evidence later identifies supported Python imports of `lucode.mcp` or `lucode.mcp_proxy`, handle that as a separate published-contract decision. The default for this implementation is no compatibility layer because the current request explicitly requires import migration and repository documentation exposes only the CLI contract.
