# Install and maintain optimized lucode Pi mode definitions

## 1. Status and context

**Status:** Implementation-ready

**Planning sources:**

- User follow-up requiring customized `@neilurk12/pi-agent-modes` Markdown definitions to replace the installed definitions after extension installation.
- The investigation and recommendations immediately preceding this plan.
- User decisions in this session:
  - Optimize the definitions for lucode before packaging and synchronization.
  - Always replace installed definitions, including locally changed or unrecognized package files.
  - Install `@neilurk12/pi-agent-modes` during `lucode init --extensions`, before synchronization.
  - Fail initialization when the required extension installation, synchronization, validation, or verification fails.
- Existing modes-policy work in `lucode-pi-modes-config-integration-plan.md` and the current implementation diff.
- Current authored definitions under `modes/{plan,ask,code,orchestrator,yolo}.md`.

**Repository root:** `/Users/L146025/Library/CloudStorage/OneDrive-EliLillyandCompany/Desktop/repos/lucode`

**Revision:** `7aa54ae7d4344cea7d5f3c77aeb6453d32b91872` on `main`.

**Relevant dirty state:** The modes-config implementation remains uncommitted. It modifies `README.md`, `src/lucode/agents/pi.py`, `src/lucode/bootstrap.py`, `src/lucode/cli.py`, and focused tests; deletes root `modes-config.yaml`; and adds `src/lucode/defaults/pi-modes-config.yaml`. `modes/` and this plan are untracked. The workspace also contains unrelated rename work and a pre-existing deletion of `pi-agent-modes-bash-policy-plan.md`. The implementation must not overwrite or fold in unrelated changes.

**Workspace constraint:** The incomplete `ucode` to `lucode` rename still causes unrelated collection and lint failures. Use the focused baseline in section 7 to distinguish regressions.

## 2. Goal and scope

### Intended behavior

When the user accepts extensions, `lucode init --extensions` uses Pi's supported package command, under lucode's isolated Pi environment and Artifactory registry, to install `@neilurk12/pi-agent-modes`. Lucode then replaces the package's five mode definitions with optimized, packaged lucode definitions before the first interactive Pi session can load the extension.

Every subsequent `lucode init --extensions` reinstalls or resolves the extension and reapplies the definitions. Installed files are verified byte-for-byte. Any displaced non-lucode bytes are retained in numbered backups, but the user decision is to replace them without a second confirmation. Installation or synchronization failure makes `init` fail with an actionable error.

The durable `PI_lucode_HOME/.pi/modes/config.yaml` override remains in place. It owns the security-relevant Bash patterns and effective prompt/tool pins, while the installed Markdown supplies complete mode metadata, optimized instructions, and a compatible built-in fallback.

### User-visible behavior

- `lucode init --extensions` installs `npm:@neilurk12/pi-agent-modes` before reporting initialization complete.
- It replaces `PI_CONFIG_DIR/npm/node_modules/@neilurk12/pi-agent-modes/modes/{plan,ask,code,orchestrator,yolo}.md` with packaged lucode definitions.
- The first Pi session sees the customized definitions; no second launch is required.
- PLAN, ASK, ORCHESTRATOR, CODE, and YOLO accurately describe their effective tools and lucode responsibilities.
- Relevant modes can call and are explicitly directed to use `ask_user` for focused structured questions.
- ORCHESTRATOR follows the installed `pi-subagents` and `sub-agent-definitions` contracts rather than obsolete `Agent`, `blitz`, `grind`, or `seeker` instructions.
- A later extension install or update can replace package files, but rerunning `lucode init --extensions` restores the packaged definitions.
- If installation or synchronization fails, init exits unsuccessfully and names the failed package or target without exposing credentials.
- `lucode init --revert` restores displaced installed definitions only when the installed bytes still match lucode's recorded bytes. Numbered recovery backups remain available.

### Success criteria

- **SC1:** All five optimized Markdown files ship as package data under `lucode.defaults`.
- **SC2:** Extension installation uses Pi's public `pi install npm:@neilurk12/pi-agent-modes` command, not direct writes into an assumed npm layout before installation.
- **SC3:** The install subprocess receives `HOME=PI_lucode_HOME`, `PI_CODING_AGENT_DIR=PI_CONFIG_DIR`, and the configured Artifactory registry environment.
- **SC4:** No package installation or Markdown synchronization occurs with `--no-extensions`.
- **SC5:** All five files are synchronized before `init` reports success and before any interactive Pi process starts.
- **SC6:** Installed files match packaged bytes after synchronization.
- **SC7:** PLAN, ASK, and ORCHESTRATOR explicitly enable `ask_user`; CODE and YOLO retain intentional empty-list all-tools semantics and name `ask_user` in their guidance when they have a prompt suffix.
- **SC8:** No optimized prompt starts with a duplicate `[MODE: ...]` header because the extension prepends it.
- **SC9:** ORCHESTRATOR contains no hard-coded obsolete agent/tool workflow and directs delegation through current installed skills and `pi-subagents` policy.
- **SC10:** A rerun leaves byte-identical installed definitions unchanged after package installation and rewrites any package-update or local divergence as the user requested.
- **SC11:** Displaced divergent content is recoverable from non-clobbering numbered backups.
- **SC12:** Revert does not overwrite edits made after lucode's last synchronization and does not delete numbered backups.
- **SC13:** Install, version, target-layout, write, validation, or post-write mismatch failures make init fail with a redacted actionable error.
- **SC14:** The YAML override and Markdown definitions agree on mode policy, effective tools, and prompt guidance.
- **SC15:** No new third-party Python runtime dependency and no public version change.

### Non-goals

- Modifying the installed extension's JavaScript evaluator.
- Treating mode prompts or Bash regexes as a security boundary.
- Supporting arbitrary extension versions without validation.
- Merging user edits into package-managed Markdown. The user chose unconditional replacement.
- Patching the stale `PI_CONFIG_DIR/node_modules` copy merely because it exists locally; only Pi's verified managed installation root is authoritative.
- Installing every package in `pi-settings.json` during this change. The required eager install is limited to `@neilurk12/pi-agent-modes`.
- Repairing unrelated rename debt, collection failures, lint failures, or other untracked files.
- Changing package or project version numbers.

### Smallest complete change

Optimize and move the five authored definitions into package data; add a Pi-extension installation and definition-synchronization helper using Pi's supported command and managed npm root; call it from the extension-approved init flow; persist digests and backups for revert/recovery; align the YAML override; add focused tests and documentation.

## 3. Evidence and decisions

### Confirmed

- **E1:** The authored sources now exist at `modes/plan.md`, `modes/ask.md`, `modes/code.md`, `modes/orchestrator.md`, and `modes/yolo.md` (`find . -path '*/{plan,ask,code,orchestrator,yolo}.md'`).
- **E2:** PLAN enables `ask_user` but its prompt inventory omits it (`modes/plan.md`, `enabled_tools` and `prompt_suffix`).
- **E3:** ASK includes `ask_user`, `ask_user_question`, and `ask_users`; its body says only questionnaire and read tools are enabled, contradicting its frontmatter (`modes/ask.md`). The installed investigation established `ask_user` as the registered pi-ask-user tool; `ask_users` is unmatched in the inspected extension version.
- **E4:** CODE and YOLO use `enabled_tools: []`, which this extension interprets as all registered tools (`modes/code.md`, `modes/yolo.md`, and prior engine investigation E6 in `lucode-pi-modes-config-integration-plan.md`).
- **E5:** ORCHESTRATOR hard-codes `Agent`, `get_subagent_result`, `steer_subagent`, `todo`, `blitz`, `grind`, and `seeker` (`modes/orchestrator.md`). The current lucode policy requires `sub-agent-definitions` as the source for sub-agent roles, models, thinking, invocation, and review.
- **E6:** All four existing prompt suffixes begin with `[MODE: ...]`; the installed extension already prepends that header (`lucode-pi-modes-config-integration-plan.md:E15`).
- **E7:** `src/lucode/defaults/pi-settings.json` configures `npm:@neilurk12/pi-agent-modes`, `npm:pi-ask-user`, and `npm:pi-subagents` in the accepted extension package list.
- **E8:** `build_runtime_env` pins `HOME`, `PI_CODING_AGENT_DIR`, and `NPM_CONFIG_REGISTRY` for Pi (`src/lucode/agents/pi.py:build_runtime_env`).
- **E9:** `src/lucode/agents/pi.py:launch` currently builds that environment immediately before starting Pi and has no extension-install/synchronization preflight.
- **E10:** `pi install --help`, run with isolated non-existent planning paths, confirms the public command `pi install <source>` and example `pi install npm:@foo/bar`. It states that install adds the package to settings.
- **E11:** Pi's package manager installs user npm packages under its agent directory's `npm` root (`DefaultPackageManager.getNpmInstallRoot` and `installNpm` in the installed `dist/core/package-manager.js`). The verified lucode target is therefore `PI_CONFIG_DIR / "npm" / "node_modules" / "@neilurk12" / "pi-agent-modes"`.
- **E12:** Pi's resource loader calls package-manager resolution before loading extension resources (`dist/core/resource-loader.js:reload`). Letting the interactive Pi process perform the first install gives lucode no reliable synchronization point before extension load.
- **E13:** Two local package copies exist and have drifted, but the package-manager evidence identifies the `npm/node_modules` tree as the managed user-package root. Presence of the other tree does not make it an active target.
- **E14:** Current modes YAML initialization already has digest-based ownership, rotating force backups, outcome reporting, and revert handling (`src/lucode/bootstrap.py:InitializeResult`, `initialize`, `revert`). This work can extend that journal rather than create a second state file.
- **E15:** Package data under `src/lucode/defaults` ships through the existing package layout, and current tests use `importlib.resources` to catch missing packaged defaults (`tests/test_model_parameters.py:TestPackagedDefaultsAreImportable`).
- **E16:** Focused baseline passes: `uv run pytest tests/test_bootstrap.py tests/test_model_parameters.py tests/test_cli.py tests/test_setup_script.py -q` reports `100 passed`.
- **E17:** `git diff --check` passes on the current working diff.

### Likely

- **L1:** Re-running `pi install npm:@neilurk12/pi-agent-modes` is idempotent enough for init and may reinstall/update the package. **Disposition:** S3 must establish this with an isolated temporary Pi home before production wiring. If it is not idempotent, use `pi list` plus install only when absent and `pi update` only under an explicit update rule; do not guess.
- **L2:** The package version remains 0.4.2 during implementation. **Disposition:** reversible default is an exact supported-version allowlist initialized with the installed version; S3 records actual package metadata after install. Unsupported versions fail closed per the user decision until definitions are reviewed.
- **L3:** `ask_user_question`, `questionnaire`, and legacy orchestration tools may still be registered by other installed extensions. **Disposition:** S1 performs a bounded runtime catalog probe. Keep only tools confirmed to be required and registered; `ask_user` remains mandatory.

### Uncertain

- **U1:** Whether `pi install` writes settings byte-identically when the package is already listed. **Disposition:** S3 isolated reconnaissance; blocks final subprocess arguments and settings-journal handling, not prompt optimization.
- **U2:** Whether Pi exposes a stable machine-readable package path or list output. **Disposition:** S3 checks the public package command. If none exists, use the package-manager-derived `PI_CONFIG_DIR/npm/node_modules/...` path and validate `package.json` before writes.
- **U3:** Live effective tool availability and prompt injection after all configured extensions load. **Disposition:** S9 manual integration check. Static source claims do not replace it.

### Committed decisions

- **D1:** Optimize the definitions before packaging. Rejected: copying the current files unchanged and limiting changes to `ask_user` spelling. Tradeoff: lucode owns more model-facing text and must re-review it on extension upgrades.
- **D2:** Install the extension during `lucode init --extensions`, before synchronization. Rejected: launch-only preflight and post-first-session repair. Tradeoff: init gains a network-dependent installation step but the first session is correct.
- **D3:** Always replace divergent installed definitions. Rejected: preserving local edits or requiring confirmed force. Tradeoff: package-local edits are not authoritative. S5 must preserve displaced bytes in numbered backups so replacement remains recoverable.
- **D4:** Fail initialization on required extension install, validation, synchronization, or verification failure. Rejected: warning and continuing with an incomplete extension setup. Tradeoff: extension-approved init is stricter and may need rerun after transient registry failures.
- **D5:** Keep the YAML override as the durable policy layer. Rejected: relying only on mutable package files. Tradeoff: Markdown and YAML must have drift checks.
- **D6:** Patch only Pi's verified managed user-package root. Rejected: blindly patching both observed node_modules trees. Tradeoff: stale unmanaged copies remain untouched; S9 verifies the loaded path.
- **D7:** Back up displaced divergent files despite unconditional replacement. This does not add a prompt or skip. It provides rollback and avoids irreversible loss.
- **D8:** Do not encode model/provider selection tables or fixed agent names in ORCHESTRATOR. It must direct the agent to the installed `sub-agent-definitions` and `pi-subagents` contracts, which own those details.

## 4. Plan

### S1 — Establish the effective mode contract

- **Title:** Verify tool names and define optimized mode responsibilities.
- **Outcome:** A current, evidence-backed contract for all five definitions, with no obsolete tool or agent assumptions.
- **Change or evidence:** Through the installed extension engine and a temporary lucode Pi home, enumerate effective registered tool names after the configured extension set is available. Confirm `ask_user`, empty-list semantics, and whether `questionnaire`, `ask_user_question`, `Agent`, `get_subagent_result`, `steer_subagent`, `todo`, and `request_mode_switch` exist. Record only names, not credentials or extension payloads. Define mode behavior: PLAN explores and plans without edits; ASK gathers only consequential missing requirements and stops when the request is complete; CODE implements and verifies; ORCHESTRATOR loads and follows current delegation skills; YOLO disables extension Bash filtering but not repository safety and authority rules.
- **Targets:** Discovery targets: installed pi-agent-modes catalog APIs and the tool registry produced by configured packages; authored files under `modes/` are read-only in this step.
- **Preconditions and dependencies:** D1, D8.
- **Shared resources:** Mode tool-list contract used by S2 and S7.
- **Acceptance criteria:** Every retained tool name is confirmed registered or required by an explicit current contract; obsolete names have a documented replacement or removal; `ask_user` is mandatory where relevant.
- **Verification:** A read-only probe prints the five effective mode names and sanitized tool names. Expected: no secret-bearing values and enough evidence to finalize S2.

### S2 — Package and optimize the five definitions

- **Title:** Make optimized Markdown package data and remove the repo-only source.
- **Outcome:** Five canonical definitions ship under `lucode.defaults`; root `modes/` no longer creates a second source of truth.
- **Change or evidence:** Move the five files to `src/lucode/defaults/pi-agent-modes/`. Apply D1 and S1 evidence. Remove duplicate `[MODE: ...]` lines. Make enabled-tool inventories and prompt prose agree. PLAN names `ask_user`, gathers repository evidence before asking, and produces dependency-ordered plans. ASK uses `ask_user` for one focused question per call, does not ask for discoverable facts, and summarizes a complete spec without forcing a question. CODE follows repository skills, makes the smallest complete change, and reports executed verification. ORCHESTRATOR directs delegation through `sub-agent-definitions`/`pi-subagents`, permits bounded parent work, verifies child evidence, and contains no fixed provider/model/agent table. YOLO states that unrestricted Bash policy does not waive repository safety, secret, Artifactory, or confirmation rules. Preserve frontmatter fields required by the extension.
- **Targets:** New `src/lucode/defaults/pi-agent-modes/{plan,ask,code,orchestrator,yolo}.md`; delete `modes/` copies after byte comparison. Contract: pi-agent-modes Markdown/frontmatter parser.
- **Preconditions and dependencies:** S1.
- **Shared resources:** The definitions feed S4 synchronization and S7 YAML alignment; serial before both.
- **Acceptance criteria:** Five non-empty parseable definitions; names match filenames; prompts have no duplicate header; tool prose matches frontmatter; ORCHESTRATOR has no obsolete names; no source duplicate remains.
- **Verification:** Load all five through the installed extension mode parser. Expected: zero parse/validation errors and five expected mode names.

### S3 — Verify and encapsulate Pi extension installation

- **Title:** Add a supported, isolated installer using Pi's package command.
- **Outcome:** Lucode can install the required extension into its own Pi home through the supported interface and Artifactory.
- **Change or evidence:** First run isolated reconnaissance for L1/U1/U2 using temporary `HOME` and `PI_CODING_AGENT_DIR`: install twice, inspect settings changes, package version, resolved path, and exit behavior. Then add a helper near Pi bootstrap/install code that runs `[PI binary, "install", "npm:@neilurk12/pi-agent-modes"]` with `build_runtime_env`-equivalent path/registry variables but no token requirement. Use the existing subprocess timeout convention and capture only bounded stderr for errors. Validate the resulting package root, `package.json` name/version, and `modes/` directory. Do not call public npm directly.
- **Targets:** Prefer extending `src/lucode/agents/pi.py` for Pi-specific paths/environment and `src/lucode/agents/install.py` only if installation orchestration fits its existing contract; tests in `tests/test_bootstrap.py` or a focused Pi test file. Discovery target: exact `pi install` repeat behavior.
- **Preconditions and dependencies:** None for reconnaissance; production helper depends on its result and D2/D4.
- **Shared resources:** Pi environment/path constants shared with S4 and bootstrap call wiring in S6.
- **Acceptance criteria:** Install targets the temporary/lucode agent directory, uses Artifactory, validates the exact package, is repeatable, and raises a redacted actionable error on failure.
- **Verification:** Mocked subprocess tests plus one isolated install probe when Artifactory is reachable. Expected: correct argv/env/timeout; package at managed root; second run has documented idempotent behavior.

### S4 — Add version-aware definition synchronization

- **Title:** Replace installed definitions and verify exact bytes.
- **Outcome:** The managed extension installation contains the five packaged lucode definitions after every extension-approved init.
- **Change or evidence:** Add a focused synchronization function, extending an existing Pi/bootstrap module before creating a new module. Load packaged definitions with `importlib.resources`; reject missing/empty payloads. Validate package name and supported version. Under the existing init lock, compare every target. Leave equal bytes untouched. Before replacing divergent bytes, copy them to `APP_DIR/modes-backups/installed-definitions/<version>/` with non-clobbering numbered names, then atomically write packaged bytes. Verify all five post-write digests. Return a structured aggregate outcome: installed, refreshed, current, or failed, plus backup paths. Always replace divergence per D3; do not prompt or skip. Record package version, target root, per-file packaged digest, and backup mapping in the init journal. Never log file bodies.
- **Targets:** `src/lucode/bootstrap.py` and/or a narrowly scoped existing Pi module; `JOURNAL_PATH` schema; `src/lucode/config.py` write helpers by reuse; constants beside existing `PI_MODES_*` constants in `src/lucode/agents/pi.py`.
- **Preconditions and dependencies:** S2, S3.
- **Shared resources:** Init journal and modes backup directory, shared with S5/S6; serial with those steps.
- **Acceptance criteria:** Five files match packaged bytes; equal files preserve mtime; divergent files are replaced; every displaced byte sequence has a unique backup; unsupported or malformed packages fail before any target write; partial-write errors are surfaced with recovery paths.
- **Verification:** Unit matrix covering missing package, wrong name, unsupported version, missing mode directory, first replacement, current no-op, package-update replacement, local divergence replacement, second divergence creating `.1`, atomic-write failure, and post-write mismatch. Expected: all branches match D3/D4/D7.

### S5 — Extend revert and recovery

- **Title:** Restore displaced package definitions without destroying later edits.
- **Outcome:** Revert safely removes lucode's installed-definition overlay while retaining recovery history.
- **Change or evidence:** Extend `bootstrap.revert()` to read the installed-definition journal. For each target, restore the recorded displaced file only when the current target digest still equals lucode's recorded digest. If current bytes differ, leave the target and backup untouched. Clear active ownership records after processing. Never delete numbered backup history. Do not attempt package uninstall unless separately requested; package installation is extension consent, while revert here concerns lucode's file overlay.
- **Targets:** `src/lucode/bootstrap.py:revert`, journal schema from S4, focused bootstrap tests.
- **Preconditions and dependencies:** S4.
- **Shared resources:** `src/lucode/bootstrap.py`, journal, backup mapping. Serial after S4 and before S6 tests finalize output.
- **Acceptance criteria:** Unchanged lucode definitions restore displaced originals; later edits survive; missing targets do not cause data loss; backups remain; existing settings and YAML revert tests still pass.
- **Verification:** Revert tests for recognized original, divergent pre-copy bytes, post-sync user edit, missing target, multiple backup generations, and coexistence with YAML revert. Expected: only digest-matching lucode bytes are replaced.

### S6 — Wire installation and synchronization into init

- **Title:** Complete extension setup before reporting init success.
- **Outcome:** `lucode init --extensions` installs, synchronizes, verifies, and reports the definitions; `--no-extensions` remains network- and package-write-free.
- **Change or evidence:** In `bootstrap.initialize`, after settings contain the accepted package list and while initialization serialization is held, call S3 then S4 when `extensions=True`. Ensure settings are persisted before `pi install`, because the public command also reads/writes that file; reload and merge journal-safe settings afterward if the isolated probe shows the command rewrites settings. Widen `InitializeResult` with installed-definition outcome and backup summary. In `cli.init_cmd`, report installed/refreshed/current and backup count. Convert install/sync failures to the repository's standard CLI error path and non-zero exit, naming the package or target but not environment values. Do not reuse the YAML `--force` confirmation for D3's unconditional package-file replacement.
- **Targets:** `src/lucode/bootstrap.py:initialize`, `InitializeResult`; `src/lucode/cli.py:init_cmd`; possible helper in S3; `tests/test_bootstrap.py`; `tests/test_cli.py`.
- **Preconditions and dependencies:** S3, S4, S5.
- **Shared resources:** Bootstrap/CLI return contract and journal. Serial with S4/S5.
- **Acceptance criteria:** Extensions true invokes install then sync in order; extensions false invokes neither; no success message precedes verification; every failure exits unsuccessfully; existing YAML outcome messages remain accurate.
- **Verification:** Mock call-order and CLI tests. Expected order: settings persistence as required by S3 evidence, package install, package validation, five-file sync, digest verification, final success.

### S7 — Align the durable YAML override

- **Title:** Keep Markdown and `pi-modes-config.yaml` behavior consistent.
- **Outcome:** Effective runtime overrides do not undo or contradict optimized definitions.
- **Change or evidence:** Update `src/lucode/defaults/pi-modes-config.yaml` from the finalized S2 contract. Preserve the sanitized Bash patterns. Ensure PLAN/ASK/ORCHESTRATOR required tool lists and all prompt suffixes agree with Markdown. Keep CODE's intentional all-tools semantics. Decide YOLO override inclusion from the effective contract: add only fields needed to preserve lucode guidance; do not change its `bash_policy: off`. Add a repository validation helper or tests that compare normalized frontmatter values and prompt text so future edits cannot drift silently.
- **Targets:** `src/lucode/defaults/pi-modes-config.yaml`; `tests/test_model_parameters.py`; definitions from S2.
- **Preconditions and dependencies:** S1, S2.
- **Shared resources:** Prompt/tool contract. Serial after S2; can run in parallel with S3/S4 code until shared tests merge.
- **Acceptance criteria:** No contradictory tool inventory or prompt; no duplicate mode header; Bash policy unchanged except previously approved sanitization; drift test detects an intentional mismatch fixture.
- **Verification:** Installed engine parses the YAML and Markdown, merges them, and reports expected policy/tools/prompts for five modes with zero diagnostics.

### S8 — Add package-data, lifecycle, and failure tests

- **Title:** Cover installation, replacement, recovery, and first-session ordering.
- **Outcome:** Regressions in every success criterion are caught without writing the real Pi home.
- **Change or evidence:** Extend package-data tests for all five Markdown files. Add temporary-tree unit tests for S3-S7. Mock subprocess rather than accessing the network in routine tests. Add an integration-style test that simulates `pi install` creating upstream files, synchronization replacing them, then launch being allowed only after verification. Assert no install/sync under `extensions=False`, no target under ambient HOME, no secret-bearing env in diagnostics, and no third-party Python import.
- **Targets:** `tests/test_model_parameters.py`, `tests/test_bootstrap.py`, `tests/test_cli.py`, and a focused Pi modes test module only if existing files become unclear.
- **Preconditions and dependencies:** S2-S7.
- **Shared resources:** Test fixtures for Pi paths and journal; coordinate with bootstrap tests.
- **Acceptance criteria:** SC1-SC15 have automated coverage except the live observations assigned to S9.
- **Verification:** Focused pytest commands in section 7. Expected: all focused tests pass; baseline-relative checks add no failure.

### S9 — Verify a clean first-session installation

- **Title:** Exercise init and live Pi from an isolated home.
- **Outcome:** Evidence that the public installation path and actual extension loader use the synchronized definitions on the first session.
- **Change or evidence:** With Artifactory access and a disposable `LUCODE_HOME`, run extension-approved init, inspect only package metadata and mode hashes, then launch Pi. Confirm the loaded extension path is the managed `npm/node_modules` target; `/mode status` shows intended policies; each mode exposes `ask_user`; prompts have one mode header; ORCHESTRATOR uses current delegation; allowed/blocked Bash spot checks match the YAML policy. Repeat init and confirm current/no-op behavior. Simulate an extension reinstall, rerun init, and confirm lucode definitions are restored.
- **Targets:** Disposable runtime state only; no repository writes.
- **Preconditions and dependencies:** S2-S8; Artifactory and interactive terminal.
- **Shared resources:** None.
- **Acceptance criteria:** First session uses synchronized definitions; reinstall recovery works; no unmanaged package tree is required.
- **Verification:** Manual transcript containing redacted paths, versions, hashes, and observed mode/tool results. Expected: all checks pass with no credentials captured.

### S10 — Document ownership, failure, and upgrade obligations

- **Title:** Explain why init installs and overwrites package definitions.
- **Outcome:** Users and maintainers understand lifecycle, recovery, and version coupling.
- **Change or evidence:** Update the README init section to state that accepting extensions invokes Pi's package installer, replaces five package-managed mode definitions, keeps backups, and fails if required setup is incomplete. Explain that local edits under the managed package are overwritten by decision, the durable YAML remains the policy layer, rerunning init repairs extension updates, and prompt/frontmatter copies must be reviewed on supported-version changes. Update CLI help if `--extensions` semantics need more explicit network/install wording.
- **Targets:** `README.md`; `src/lucode/cli.py:init_cmd` help/docstring.
- **Preconditions and dependencies:** S3-S7 final behavior.
- **Shared resources:** README and CLI prose; coordinate with S6.
- **Acceptance criteria:** Documentation states install timing, overwrite semantics, backup location, failure behavior, rerun repair, version coupling, and non-security-boundary posture.
- **Verification:** Read-back checklist for those seven points and CLI help snapshot. Expected: no claim of sandboxing or silent preservation of package-local edits.

## 5. Coverage

| Source item | Disposition |
| --- | --- |
| Copy customized Markdown after extension installation | S2-S6 |
| Replace installed originals in managed npm path | D3, D6; S3-S4 |
| First session must use definitions | D2; S6, S9 |
| Optimize mode context | D1; S1-S2 |
| `ask_user` access and awareness | SC7; S1-S2, S7-S9 |
| PLAN prompt omits enabled `ask_user` | E2; S2 |
| ASK frontmatter/body contradiction and `ask_users` | E3; S1-S2 |
| CODE/YOLO empty allowlist semantics | E4; S1-S2, S7 |
| Obsolete ORCHESTRATOR tools/agents | E5, D8; S1-S2 |
| Duplicate mode headers | E6; SC8; S2, S7 |
| Existing package list includes agent-modes, ask-user, subagents | E7; S3, S9 |
| Isolated HOME/agent dir/registry | E8; SC3; S3, S8 |
| Pi installs packages before extension load | E12; D2; S3, S6 |
| Two local node_modules copies | E13; D6; S3, S9 |
| Always replace local divergence | User decision D3; S4 |
| Recover displaced bytes | D7; S4-S5 |
| Install during init | User decision D2; S3, S6 |
| Fail incomplete extension setup | User decision D4; S3-S4, S6 |
| Keep durable YAML | D5; S7 |
| Package data | SC1; S2, S8 |
| Reinstall/update repair | SC10; S4, S8-S9 |
| Safe revert | SC12; S5, S8 |
| No new dependency/version change | SC15; S8 and repository boundaries |
| Public Pi install command | E10; SC2; S3 |
| Idempotence/settings rewrite uncertainty | L1/U1; S3 reconnaissance |
| Stable path-output uncertainty | U2; S3 reconnaissance with verified fallback |
| Live tool/prompt behavior uncertainty | U3; S9 |
| Install all other configured packages during init | Deferred non-goal; unnecessary for the requested agent-modes copy |
| Modify evaluator/security boundary | Deferred non-goal; existing documented upstream limitation |
| Repair unrelated rename debt | Deferred non-goal; baseline only |

## 6. Delegation and sequencing

- **Read-only reconnaissance:** S1 and the reconnaissance portion of S3 may run in parallel. They share no files. Their outputs are required before S2 and production S3 code respectively.
- **Definition branch:** S2, then S7. They share the mode prompt/tool contract and must be serial.
- **Lifecycle branch:** S3 production helper, then S4, then S5, then S6. S4-S6 share Pi paths, the init journal, and `InitializeResult`; keep one writer or serialize changes.
- **Convergence:** S8 follows S2-S7. S10 may begin after behavior settles but must merge after S6/S7 wording is final. S9 follows all automated implementation and tests.
- **Handoff minimum:** Supported package version, verified managed root, `pi install` repeat behavior, final five definition digests, normalized tool lists, journal keys, aggregate outcome values, and backup layout.
- **Review:** S2 changes model-facing instructions; S3 adds a network/package installation path; S4 unconditionally overwrites files; S5 restores backups; S6 changes init failure behavior. Route these through independent review. Because this includes destructive replacement and security-relevant mode policy, require provider diversity under the implementation session's current `sub-agent-definitions` policy.

## 7. Verification

### Baseline

- `uv run pytest tests/test_bootstrap.py tests/test_model_parameters.py tests/test_cli.py tests/test_setup_script.py -q` currently passes: **100 passed**.
- `git diff --check` currently passes.
- The broader repository retains the previously documented incomplete-rename failures; capture a fresh full baseline before implementation if the dirty workspace has changed materially.

### Planned checks

| Check | Covers | Expected |
| --- | --- | --- |
| Isolated `pi install` twice under temporary HOME/agent dir | S3, L1, U1, U2 | Managed root and settings behavior documented; both runs succeed or production logic branches on evidence |
| `uv run pytest tests/test_bootstrap.py -q` | S4-S6, SC4-SC6, SC10-SC13 | All pass |
| `uv run pytest tests/test_model_parameters.py -q` | S2, S7-S8, SC1, SC7-SC9, SC14 | All pass |
| `uv run pytest tests/test_cli.py -q` | S6, S10, failure/reporting behavior | All pass |
| `uv run pytest tests/test_setup_script.py -q` | Init remains setup entry point | All pass |
| New focused Pi mode lifecycle tests | S1-S9 | All pass without real-home writes |
| `uv run ruff check` on changed files | Style | No new findings |
| `uv run ty check` on changed files | Types/contracts | No new findings |
| `git diff --check` | Patch hygiene | Pass |
| Direct runtime import lint | SC15 | No new third-party import; only documented baseline failures, if any |
| Installed engine parse/merge probe | S2, S7, SC7-SC9, SC14 | Five modes, zero diagnostics, expected effective tools/prompts/policies |
| Disposable-home first-session test | S9, SC2-SC6, SC10 | Synchronized definitions loaded on first session |
| Reinstall then rerun init | S4, S9, SC10-SC11 | Upstream files displaced, backed up, and lucode bytes restored |
| Revert after sync and after later edit | S5, SC12 | Original restored only when current bytes remain lucode-owned |

### Manual checks

S9 requires Artifactory access and an interactive terminal. Record `/mode status`, tool availability, prompt header count, ORCHESTRATOR delegation behavior, and selected Bash-policy probes without retaining tokens or authorization output.

### Not verified

- `not verified: actual package installation, because planning rules prohibit installing dependencies; only the public help and installed package-manager source were inspected`.
- `not verified: whether repeated pi install rewrites settings or updates the package, assigned to S3 isolated reconnaissance`.
- `not verified: live first-session extension path, ask_user rendering, and optimized prompt behavior, assigned to S9`.
- `not verified: wheel inclusion of the five new files, because they are not yet under package data and no build was run`.

## 8. Risks and assumptions

- **R1 — Unconditional overwrite destroys package-local edits.** Impact: user modifications under the managed extension disappear. Mitigation: D3 is explicit; S4 creates numbered backups before replacement and reports backup count. Owner: lucode maintainers. Recovery: S5 or manual backup restore.
- **R2 — Init becomes network-dependent.** Impact: transient Artifactory or npm failures make extension-approved init fail. Mitigation: bounded timeout, actionable redacted error, atomic settings/journal writes, rerunnable operation. Owner: environment/operator; evidence from S3.
- **R3 — `pi install` may rewrite settings.** Impact: it could race or erase init ownership data. Mitigation: S3 establishes exact behavior; S6 orders and reloads writes accordingly under the init lock. Do not authorize code from assumption U1.
- **R4 — Extension version drift.** Impact: frontmatter schema or mode behavior can change while lucode overwrites files built for 0.4.2. Mitigation: exact supported-version validation and fail closed; review and update definitions/YAML before widening. Owner: whoever upgrades the extension.
- **R5 — Mutable node_modules is not a stable API.** Impact: reinstall replaces lucode files. Mitigation: this is intended; init always reapplies after install, YAML remains durable, and S9 tests reinstall repair.
- **R6 — Prompt/YAML duplication can drift.** Impact: effective behavior differs from displayed built-in guidance. Mitigation: S7 normalized drift checks and one packaged Markdown source.
- **R7 — Tool registry varies with extension set.** Impact: a non-empty enabled-tools list can block required tools. Mitigation: S1 runtime evidence, mandatory `ask_user`, exact list tests, and supported package-set documentation.
- **R8 — The Bash policy is not a sandbox.** Impact: optimized wording could overstate safety. Mitigation: preserve prompt-reduction language and existing evaluator limitation documentation. Owner: upstream evaluator maintainer.
- **R9 — Partial synchronization.** Impact: some modes could be lucode-authored and others upstream after I/O failure. Mitigation: validate all inputs before writes, back up each displacement, verify all outputs, fail init, and report recovery paths. Full directory transaction is unavailable; idempotent rerun is the reversible recovery.
- **A1 — Managed root is `PI_CONFIG_DIR/npm/node_modules`.** Confidence: confirmed by installed package-manager source. S3 still validates package metadata before writing.
- **A2 — Installing only agent-modes eagerly is sufficient.** Confidence: likely and reversible. Pi will resolve the remaining accepted extensions at startup; S9 confirms `ask_user` is available when modes load.

## 9. Open questions or blockers

No implementation blocker remains. Low-impact follow-ups:

- **Q1:** If S3 shows `pi install` always updates to latest, decide whether to pin the source as `npm:@neilurk12/pi-agent-modes@0.4.2`. Reversible default: pin the supported version to keep S4's compatibility check deterministic.
- **Q2:** Decide backup retention only after observing real growth. Reversible default: never prune automatically because backups hold overwritten user/package content.
- **Q3:** If Pi later exposes a stable machine-readable resolved-package API, replace the derived managed-root path. Reversible default: validate the package-manager-derived path and package metadata on each run.
