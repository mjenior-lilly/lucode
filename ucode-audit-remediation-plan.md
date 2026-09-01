# Implementation plan — `ucode` audit remediation

## 1. Status and context

**Status: `Implementation-ready`**

- **Planning source:** `slop-audit-20260831-82a161c.md` (repo root, untracked), the code-quality audit produced earlier in this session. Audit finding F13 is excluded by the user's later instruction; every other finding remains in scope.
- **Repository root:** `/Users/L146025/Library/CloudStorage/OneDrive-EliLillyandCompany/Desktop/repos/lucode`
- **Revision:** `82a161c` on `main`
- **Dirty state:** `slop-audit-20260831-796b096.md` is deleted-unstaged (pre-existing, inherited); `slop-audit-20260831-82a161c.md` and this plan are untracked. **No source file is modified.** S9 resolves the audit artifacts; the plan remains a separate untracked artifact.
- **Artifact path:** `ucode-audit-remediation-plan.md` at the repository root, moved here by the user after the initial draft.
- **Baseline established:** `uv run --locked pytest --ignore=tests/test_e2e.py --ignore=tests/test_e2e_uc.py --ignore=tests/test_e2e_user_agent.py -q` -> **911 passed in 9.84s, zero failures.** Any failure after these changes is a genuine regression, not a pre-existing one.

No secrets, credentials, tokens, or personal data appear in this plan.

---

## 2. Goal and scope

### Intended behavior

Eliminate the audit's one data-loss defect and its confirmed correctness defects, then reduce the repository's dominant failure mode — operations that fail silently and report success — so that a failed read, a partial result, or a swallowed worker exception is distinguishable from genuine absence.

### User-visible behavior

- `ucode configure --dry-run` never modifies or deletes a file, matching its documented contract.
- `ucode setup --from-file` rejects a malformed manifest with an actionable message instead of persisting it and later crashing with a traceback, and never silently clears a workspace-wide policy field.
- `ucode configure skills` reports a per-skill warning on network timeout instead of aborting, and an accepted overwrite leaves no stale files behind.
- Discovery pickers distinguish "this failed" from "none found".
- Long-running agent sessions surface token-refresh failures instead of degrading silently 30 minutes later.
- No change to any command name, flag, output contract, or on-disk state schema.

### Observable success criteria

| ID | Criterion | Covered by |
| --- | --- | --- |
| SC1 | With dry-run active, `restore_file` performs no filesystem mutation; `ucode revert` behavior is unchanged. | S1 |
| SC2 | A manifest with a wrong container type for `mcp_servers`, `skills.names`, `budget_policy`, or `tiers` produces a validation error and is not persisted. | S6 |
| SC3 | A socket timeout during a skill download yields `(None, reason)` and a per-skill warning, not an escaping exception. | S2 |
| SC4 | Full suite stays green: 911 passed plus new tests, zero regressions. | V1 |
| SC5 | `ruff check`, `ruff format --check`, and `ty check src/` continue to pass. | V2 |

### Non-goals

- No new features, no command or flag additions, no dependency upgrades.
- No change to the managed-config fail-open *policy* (S12 fixes only the message and the caller's ability to distinguish cases).
- No directory restructuring (F23 deferred), no split of `configure_shared_state` (F19 deferred).
- No behavior change to `ucode revert`, which legitimately restores files.
- Not verifying anything against a live Databricks workspace.

### Smallest complete change

Phases A–C below. Phase A alone closes the critical and remaining high finding and is independently shippable; B and C are additive.

---

## 3. Evidence and decisions

### Confirmed facts (each re-verified against `82a161c` during planning)

| Claim | Locator |
| --- | --- |
| `restore_file` has no dry-run guard; both siblings do | `config_io.py:59-72` vs `:44-45` (`backup_existing_file`), `:70-72` (`write_text_file`) |
| `save_state` **already has** the exact missing guard and runs 3 lines after the unguarded `restore_file` on the same failure path | `state.py:51` (`if is_dry_run(): return`); `cli.py:593` then `cli.py:596` |
| `revert` never enables dry-run, so guarding `restore_file` cannot affect it | Complete `set_dry_run` caller set is `cli.py:1173`, `:1384`, `:1637`; `revert_cmd` at `cli.py:1703-1710` calls none |
| `--dry-run --agent pi` is accepted and reaches validation | `cli.py:1384` sets flag; `:1414-1427` dispatches; `:582-584` returns only on `--skip-validate`; `:585-586` runs `validate_tool` |
| `validate_tool` runs a real subprocess with no dry-run guard | `agents/__init__.py:259-294` |
| `validate_manifest` gates on `isinstance` with no `else` branch | `managed_setup.py:347` (`mcp_servers`), `:364-367` (`skills`/`names`), `:369` (`budget_policy`), `:417` (`tiers`) |
| The update mask deliberately sends every path so a re-run **clears** removed fields | `databricks/managed.py:155-168` and its comment: "Sending every path ucode owns ... is what lets a re-run *clear* a field the admin removed" |
| `_http_get_bytes` lacks the `OSError` catch its JSON sibling has, with a comment naming this exact failure | `transport.py:305-331` vs `:211-215`; `issubclass(TimeoutError, urllib.error.URLError)` is `False`, `issubclass(TimeoutError, OSError)` is `True` (verified via `python3 -c`) |
| `_drain_with_deadline` swallows all worker exceptions and records nothing | `mcp_discovery.py:279-292`; 5 production drains at `:432, :527, :551, :631, :657` |
| Pagination discards `last_reason` on partial success, by documented design | `models.py:168-172` ("a mid-pagination blip still returns whatever we collected"), `:187-191` |
| `_persisted_fallback`'s silence with no cache is **deliberate and documented** | `managed_config.py:381-394` docstring: "With nothing persisted there is no managed config in play at all, so staying quiet..." |
| `_format_subprocess_result` violates its own docstring on the failure branch | `transport.py:75-83`: says "without leaking tokens" and "stdout ... often contains the access token", then returns that stdout when `returncode != 0` |
| Debug logging is opt-in | `transport.py:21-22`: `os.environ.get("UCODE_DEBUG") == "1"` |
| Both refresh loops discard `RuntimeError` with no warning | `agents/pi.py:373-378`, `agents/opencode.py:252-257`; interval 1,800s |
| `write_skill` never clears the existing dir before writing | `skills_download.py:203-216`; `_write_bundle` only creates parents and writes new paths (`:185-194`). `shutil` is **not** imported (`:3-14`) |
| `load_full_state` validates only `state_version`, not `workspaces` | `state.py:20-31`; `save_state` indexes `full["workspaces"][workspace]` at `:58` |
| `configured_usage_tools` falls through on an explicitly empty list | `usage.py:137-141` (`or` on a falsy `[]`) |
| `prompt_for_text` can never return `None` for empty input with no default | `ui.py:426-435`: loops via `print_err("Please enter a value.")` |
| Dependency guard is a 3-entry literal | `tests/test_lint.py:31-36` |
| 3 import cycles, broken only by deferred imports | AST import-graph: `agents->agents.pi->state->agents`, `agents->state->agents`, `cli->managed_wizard->cli`; 12 function-level imports |
| `transport.py` exposes 1 public symbol, 13 private, 8 of which cross module boundaries | Import sites: `models.py:8`, `mcp_discovery.py:15`, `skills_download.py:11`, `managed.py:9-12`, `auth.py:17-19` |
| Two 61/62-line duplicate catalog/schema walks | `mcp_discovery.py:483-543` and `:584-645`, both calling `_paginated_json_items` with `items_key="catalogs"` |
| Prior audit report is tracked; no ignore rule matches | `git ls-files --error-unmatch` succeeds; `.gitignore` has 8 entries, none matching `slop-audit-*` |

### Likely / uncertain items and their dispositions

| Item | Confidence | Disposition |
| --- | --- | --- |
| F10 — UC identifiers permit hyphens, making the dot->dash collision reachable | uncertain | **Bounded reconnaissance (S16).** The mapping's non-injectivity is confirmed (`a-b.c.d` and `a.b-c.d` both -> `a-b-c-d`); only real-world reachability is open. Repo hint: `SKILL_NAME_PATTERN = ^[a-z0-9-]+$` (`skills_download.py:26`) permits hyphens in skill leaf names. S16 gates S16b; no production change is authorized until it resolves. |
| F7 — a failed `databricks auth token` actually emits token material | likely | **Low-impact assumption with a reversible default (S13).** Apply the existing scrubbers to the failure branch regardless of whether the CLI emits credentials. The fix is correct either way because the docstring already promises it; the open question affects only severity, not the action. |
| F5 — a managed-endpoint 403/500 with valid auth reaches `launch_agent` | likely | **Bounded to a messaging fix (S12).** The fail-open is deliberate and documented, so S12 changes only the user-facing message and the caller's ability to distinguish "read failed" from "no config", not the launch policy. |
| F21 — `bootstrap.py` has no external consumer | uncertain | **Reconnaissance only (S22), no removal.** Requires searching outside this repository. |

### Committed decisions

- **D1 — Guard inside `restore_file`, not at its call sites.** Consistent with `backup_existing_file` and `write_text_file`, which own their own guards, and with `save_state`. Fixes all 6 call sites at once. *Tradeoff:* a future caller that legitimately needs to restore during dry-run would have to bypass the helper; no such caller exists, and `revert` never sets the flag. *Rejected:* guarding at `cli.py:593` and `:937` only — leaves `agents/__init__.py:331,333` exposed and duplicates the check.
- **D2 — Validate container types inside `validate_manifest`, not with a new schema library.** The function already returns a `list[str]` of actionable errors; adding `else` branches matches the existing idiom. *Rejected:* introducing `jsonschema`/`pydantic` — a new runtime dependency for one call path, and the Artifactory-only install constraint makes a new dependency a heavier decision than the defect warrants.
- **D3 — Return a completeness signal from `list_model_services` rather than converting partial success into failure.** The best-effort behavior is deliberate (`models.py:169-170`); callers simply cannot see it. *Tradeoff:* changes an internal return shape, so all callers must be updated in the same step.
- **D4 — Phase A is shippable alone.** Ordering is by severity, so review can stop after Phase A without leaving a partial fix.

### Decisions deliberately NOT made — require approval during implementation

These are recorded as open, not committed. Per the skill, dependency-graph changes, published-surface renames, and destructive operations are not mine to assume.

- **Q1 (S21) — remove `tomlkit`?** Removing the 4 test-only helpers makes `tomlkit` unused, changing `pyproject.toml` and `uv.lock`. `AGENTS.md` says not to modify lock files "unless the dependency graph intentionally changes" — this would be intentional, but it is the user's call. **Reversible default: remove the helpers, keep the dependency declared**, and note the follow-up.
- **Q2 (S19) — rename `transport.py`'s 8 cross-module private symbols?** Mechanical but touches 5 modules and ~11 import statements. **Reversible default: defer**; the finding is `medium` convention-drift with no behavioral impact.
- **Q3 (S15) — delete stale skill files on overwrite?** The correct fix removes the existing directory before writing, which is a destructive filesystem operation on user-visible files. **Reversible default: implement removal scoped strictly to the `root/leaf` directory the user just approved overwriting, after re-verifying `_is_valid_leaf(leaf)`**, never a computed or user-supplied path.
- **Q4 (S1 follow-up) — should `--dry-run` skip validation entirely?** Validating a config that was never written is arguably meaningless. **Reversible default: no** — S1 makes the path safe, and skipping validation would change observable dry-run output.

---

## 4. Plan

### Phase A — critical, high, and cheap confirmed bugs

**S1 — Guard `restore_file` against dry-run**
- **Outcome:** With dry-run active, `restore_file` mutates nothing and returns `False`. `ucode revert` behavior is unchanged.
- **Change:** Add `if _dry_run: return False` as the first statement of `restore_file`, matching `backup_existing_file:44-45`. Add a regression test asserting that under dry-run, a validation-failure rollback leaves both the live config and the backup untouched.
- **Targets:** `src/ucode/config_io.py:59-72`; `tests/test_config_io.py` (existing dry-run fixture `reset_dry_run` at `:29-33`; sibling tests at `:104`, `:121`, `:131`, `:156` establish the pattern).
- **Preconditions:** None.
- **Shared resources:** `src/ucode/config_io.py` and `tests/test_config_io.py` — also touched by S21. Serialize S1 before S21.
- **Acceptance:** New test fails before the change and passes after. Existing `restore_file` tests at `:121-140` still pass (they run with dry-run off via the fixture).
- **Verification:** `uv run pytest tests/test_config_io.py -q` -> all pass, including the new test.
- **Risk note:** Returning `False` is the same signal `backup_existing_file` uses for "did nothing". Confirm no caller treats `False` as an error requiring an abort — `cli.py:593` and `:937` discard the return value; `cli.py:768-777` uses it only for reporting.

**S2 — Catch `OSError` in `_http_get_bytes`**
- **Outcome:** A socket timeout returns `(None, reason)` instead of escaping and aborting the whole skills download.
- **Change:** Add an `except OSError` clause mirroring `_http_get_json:211-215`, including its explanatory comment. Add a test that patches `urlopen` to raise `TimeoutError` and asserts a `(None, reason)` tuple.
- **Targets:** `src/ucode/databricks/transport.py:305-331`; `tests/test_databricks_transport.py`.
- **Preconditions:** None.
- **Shared resources:** `src/ucode/databricks/transport.py` — also touched by S13 and (if approved) S19. Serialize.
- **Acceptance:** Timeout produces a reason string; no exception escapes.
- **Verification:** `uv run pytest tests/test_databricks_transport.py tests/test_skills_download.py -q`.

**S3 — Distinguish an empty `available_tools` from a missing one**
- **Outcome:** `ucode usage` reports "no coding agents configured" after validation has emptied `available_tools`, instead of falling back to stale `managed_configs` keys.
- **Change:** Replace the `or` fallback with an explicit `is None` check at `usage.py:137-141`. Add a test for the empty-list case.
- **Targets:** `src/ucode/usage.py:137-141`; `tests/test_usage.py` (existing assertion at `:248-251` covers the non-empty case).
- **Preconditions:** None.
- **Shared resources:** `src/ucode/usage.py` — also touched by S18. Serialize.
- **Acceptance:** `configured_usage_tools({"available_tools": [], "managed_configs": {"pi": {...}}}, ...)` returns `[]`.
- **Verification:** `uv run pytest tests/test_usage.py -q`.

**S4 — Validate the `workspaces` shape in `load_full_state`**
- **Outcome:** A current-version state file with a missing or non-dict `workspaces` degrades to the empty-state fallback instead of raising `KeyError`/`TypeError`.
- **Change:** Extend the guard at `state.py:29-30` to also require `isinstance(data.get("workspaces"), dict)`. Add tests for a missing `workspaces` key and a non-dict value.
- **Targets:** `src/ucode/state.py:20-31`; `tests/test_state.py` (existing malformed-state tests at `:58-88`).
- **Preconditions:** None.
- **Shared resources:** `src/ucode/state.py` — also touched by S18. Serialize.
- **Acceptance:** Both malformed shapes return the empty structure; `save_state`, `load_state`, and `clear_state` no longer raise on them.
- **Verification:** `uv run pytest tests/test_state.py -q`.

**S5 — Correct the `prompt_for_text` docstring**
- **Outcome:** The docstring describes actual behavior. No behavior change.
- **Change:** Remove the unreachable "or None when there is no default and the user submits nothing" clause; state that with no default and empty input the prompt re-asks, and that `None` is returned only on EOF when `required=False` and no default exists. Per the annotation guidance, this is a documentation correction backed by `ui.py:426-435`.
- **Targets:** `src/ucode/ui.py:401-410` (docstring only).
- **Preconditions:** None.
- **Shared resources:** None.
- **Acceptance:** Docstring matches the code path; no test change needed.
- **Verification:** `uv run pytest tests/test_ui.py -q` -> unchanged pass. `uv run ty check src/`.

**S6 — Reject malformed container types in `validate_manifest`**
- **Outcome:** A wrong container type for `mcp_servers`, `skills`, `skills.names`, `budget_policy`, or `budget_policy.tiers` produces an actionable error and is never persisted, so `_render_summary` cannot crash and `apply` cannot silently clear `skills`.
- **Change:** Add `else` branches to the four `isinstance` gates, each appending a message in the existing style (for example `"mcp_servers must be a list."`). Add tests for `{"mcp_servers": "not-a-list"}` and `{"skills": {"names": "catalog.schema"}}`, asserting a non-empty error list and that `save_managed_settings` is not called.
- **Targets:** `src/ucode/managed_setup.py:346-370` and `:417`; `tests/test_managed_setup.py:293-380` (existing validator tests establish the pattern).
- **Preconditions:** None. Decision D2 committed.
- **Shared resources:** `src/ucode/managed_setup.py` — no other step writes it.
- **Acceptance:** Both malformed manifests fail validation. `managed_wizard.setup_from_file` returns non-zero without persisting. The mask-clearing hazard at `databricks/managed.py:155-168` is unreachable for these shapes.
- **Verification:** `uv run pytest tests/test_managed_setup.py tests/test_managed_wizard.py -q`.
- **Risk note:** Manifests that previously "passed" now fail. That is the intent, but confirm no existing test asserts a malformed shape validates cleanly — the audit found coverage only for valid list shapes and malformed *elements*, not malformed containers.

**S8 — Make the dependency guard discover imports**
- **Outcome:** The test detects an undeclared third-party import anywhere in `src/`, and a removed declaration for any of the 9 runtime distributions.
- **Change:** Replace the 3-entry literal at `test_lint.py:31-36` with an AST walk over `src/ucode/**/*.py` collecting top-level third-party module names, mapped to distribution names, then asserted against `pyproject.toml` `[project].dependencies`. Exclude stdlib via `sys.stdlib_module_names`. Keep the import->distribution mapping explicit for the cases where they differ (`prompt_toolkit`->`prompt-toolkit`, `databricks`->`databricks-sql-connector`).
- **Targets:** `tests/test_lint.py:26-36`; consider folding in the one-off `httpx` assertion at `tests/test_mcp_proxy.py:15-20`.
- **Preconditions:** None.
- **Shared resources:** `tests/test_lint.py` — no other step writes it.
- **Acceptance:** The test passes against the current tree (all 9 declared and imported, verified during the audit) and fails if a declaration is removed.
- **Verification:** `uv run pytest tests/test_lint.py -q`. Then temporarily comment out a `pyproject.toml` dependency line, confirm the test fails, and revert — a local check only, never committed.

**S9 — Stop tracking audit reports**
- **Outcome:** Generated audit reports are not version-controlled.
- **Change:** Add `slop-audit-*.md` to `.gitignore`. Commit the already-pending deletion of `slop-audit-20260831-796b096.md`. The new report at `slop-audit-20260831-82a161c.md` becomes ignored.
- **Targets:** `.gitignore` (8 entries currently); the pending deletion in the worktree.
- **Preconditions:** None.
- **Shared resources:** `.gitignore` — no other step writes it.
- **Acceptance:** `git status --porcelain` is clean; `git check-ignore -v slop-audit-20260831-82a161c.md` matches a rule.
- **Verification:** `git status --porcelain` and `git check-ignore -v`.
- **Note:** This step stages and commits a deletion. Confirm with the user before committing — the audit report may be wanted for reference first.

### Phase B — failure-signal loss

**S10 — Report swallowed discovery worker exceptions**
- **Outcome:** A worker exception surfaces as a failure reason instead of "no resources found".
- **Change:** Have `_drain_with_deadline` count and retain the first worker exception, then have the three public discovery functions incorporate it into their returned reason. Preserve the existing deadline/partial-result behavior and the `# noqa: BLE001` (the blanket catch is still correct; it must simply stop discarding).
- **Targets:** `src/ucode/databricks/mcp_discovery.py:279-292`; the 5 drain sites `:432, :527, :551, :631, :657`; the reason-producing returns in `list_vector_search_catalog_schemas`, `list_uc_functions_catalog_schemas`, `list_all_mcp_services`; `tests/test_databricks_mcp_discovery.py`.
- **Preconditions:** None.
- **Shared resources:** `src/ucode/databricks/mcp_discovery.py` — also S20 and possibly S19. Serialize.
- **Acceptance:** A worker raising a synthetic exception yields a reason mentioning failure, not absence. Cancellation/deadline tests still pass.
- **Verification:** `uv run pytest tests/test_databricks_mcp_discovery.py -q`.

**S11 — Give `list_model_services` a completeness signal**
- **Outcome:** Callers can distinguish a complete walk from a partial one; a partial result is not cached as authoritative.
- **Change:** Per D3, return partial-ness explicitly (either a third element or a non-`None` reason alongside a non-empty list) and skip the cache write when the walk was truncated. Update `discover_model_services` and `configure_shared_state` to propagate it — at minimum, warn rather than persisting silently.
- **Targets:** `src/ucode/databricks/models.py:168-172`, `:187-191`, `:195-239`; `src/ucode/cli.py:465-510`; `tests/test_databricks_models.py`.
- **Preconditions:** D3. Coordinate with S18, which also edits `cli.py`.
- **Shared resources:** `src/ucode/databricks/models.py` (S19), `src/ucode/cli.py` (S12, S18). Serialize.
- **Acceptance:** A mid-pagination failure after a successful first page yields a partial marker, is not cached, and does not silently persist as complete. The existing family-fallback at `cli.py:473-485` still functions.
- **Verification:** `uv run pytest tests/test_databricks_models.py tests/test_cli.py -q`.

**S12 — Distinguish "managed read failed" from "no managed config"**
- **Outcome:** The user is told the read failed when it failed. The documented fail-open launch policy is unchanged.
- **Change:** Preserve the failure reason across `_persisted_fallback` so the caller can tell the two cases apart, then correct the message at `cli.py:1065-1090` and the guidance path at `cli.py:1217-1229`. Do **not** block the launch. Update the docstring at `managed_config.py:381-394`, which currently documents the silence as intentional, and the tests at `test_managed_config.py:296-338` that assert it.
- **Targets:** `src/ucode/managed_config.py:365-370`, `:381-394`; `src/ucode/cli.py:1065-1090`, `:1217-1229`; `tests/test_managed_config.py:296-338`.
- **Preconditions:** None. Scope is limited to messaging per section 3.
- **Shared resources:** `src/ucode/cli.py` (S11, S18). Serialize.
- **Acceptance:** A no-cache fetch failure produces a message distinguishing failure from absence; the agent still launches; tests updated to assert the new message rather than silence.
- **Verification:** `uv run pytest tests/test_managed_config.py tests/test_cli.py -q`.
- **Risk note:** This intentionally changes assertions in existing tests. Update them deliberately and record why in the commit, rather than loosening them.

**S13 — Scrub credentials on the subprocess-failure branch**
- **Outcome:** `_format_subprocess_result` honors its own docstring on every branch.
- **Change:** Apply the existing `_SECRET_KEY_PATTERN`/`_scrub_json` machinery (`transport.py:69`, `:101`) to the failure-branch stdout and stderr. Add a test with a nonzero `CompletedProcess` whose streams contain `access_token`, a `dapi...`-shaped value, and an `Authorization: Bearer ...` header, asserting none survive. Keep the existing assertion that benign diagnostics still appear (`tests/test_databricks_transport.py:128-138`).
- **Targets:** `src/ucode/databricks/transport.py:72-83`; `tests/test_databricks_transport.py:120-140`.
- **Preconditions:** None. Correct regardless of the open F7 question.
- **Shared resources:** `src/ucode/databricks/transport.py` (S2, S19). Serialize after S2.
- **Acceptance:** Credential-shaped content is redacted; benign diagnostic text is preserved; truncation still applies.
- **Verification:** `uv run pytest tests/test_databricks_transport.py -q`.
- **Note:** Use only synthetic fake credentials in tests. Never a real token.

**S14 — Warn on background token-refresh failure**
- **Outcome:** A refresh failure is visible instead of silent for up to 30 minutes.
- **Change:** In both `_refresh_forever` loops, warn on the caught `RuntimeError` before continuing, via the repo's `print_warning` convention. Avoid a warning per 30-minute tick for a persistent failure — warn on transition into the failing state, or rate-limit.
- **Targets:** `src/ucode/agents/pi.py:373-378`; `src/ucode/agents/opencode.py:252-257`; `tests/test_agent_pi.py`, `tests/test_agent_opencode.py`.
- **Preconditions:** None.
- **Shared resources:** None (distinct files from other steps).
- **Acceptance:** A failing refresh emits one warning; the loop keeps retrying; the daemon still does not crash the session.
- **Verification:** `uv run pytest tests/test_agent_pi.py tests/test_agent_opencode.py -q`.

**S15 — Remove stale files when a skill is overwritten** *(requires Q3 approval)*
- **Outcome:** An accepted overwrite leaves only the new bundle's files.
- **Change:** Before `_write_bundle`, remove the existing `root / leaf` directory. Guard strictly: re-verify `_is_valid_leaf(leaf)` (already checked at `:203-205`), operate only on `root / leaf` where `root` comes from the resolved roots list, and never on a path derived from bundle content. `shutil` is not currently imported and must be added. Add a test that an overwrite deletes a file absent from the new bundle.
- **Targets:** `src/ucode/skills_download.py:203-216`, imports at `:3-14`; `tests/test_skills_download.py:260-285`.
- **Preconditions:** **Q3 approval** — this deletes user-visible files.
- **Shared resources:** `src/ucode/skills_download.py` (S19). Serialize.
- **Acceptance:** Stale files are gone; the "Kept existing" decline path still writes nothing; unsafe-path warnings still work.
- **Verification:** `uv run pytest tests/test_skills_download.py -q`.
- **Rollback:** Behavior-only change, revertible by commit. Note in the commit that overwrite is now destructive-by-design within the approved directory.

**S16 — Reconnaissance: do UC identifiers permit hyphens?** *(gates S16b)*
- **Outcome:** F10 resolves to confirmed or refuted. **Unblocks:** whether the dot->dash collision is a real defect.
- **Evidence to gather:** Databricks Unity Catalog identifier rules for catalog, schema, and service names — specifically whether an unquoted identifier may contain `-`. Sources: official Databricks documentation, or observed `full_name` values from a real workspace listing. Repo hint already found: `SKILL_NAME_PATTERN = ^[a-z0-9-]+$` (`skills_download.py:26`) permits hyphens for skill leaves, which is suggestive but not authoritative for catalog/schema.
- **Targets:** documentation lookup; `src/ucode/mcp.py:662`, `:840`, `:1220` (the 3 confirmed mapping sites) as the change surface if confirmed.
- **Preconditions:** None.
- **Shared resources:** None (read-only).
- **Acceptance:** A documented answer with a citation.
- **Verification:** Cited source recorded in the follow-up.
- **S16b (conditional):** If hyphens are permitted, make the state/client key injective — for example, escape `-` before substituting, or key on the dotted `full_name` with dashing only at the client-config boundary. Must handle already-persisted dashed state, so treat it as a migration concern, not a pure rename. If hyphens are not permitted, close F10 as refuted and record why.

**S17 — Align the documented module boundary with `mcp.py`**
- **Outcome:** `AGENTS.md` describes where the MCP TUI actually lives.
- **Change:** Update the boundary sentence at `AGENTS.md:20` so it accounts for `mcp.py` owning the interactive picker (`_scrolling_checkbox` at `mcp.py:471-634`, `build_mcp_picker_choices` at `:637-750`), rather than implying all presentation sits in `ui.py`. Documentation only — do **not** move the widget, which would be a large refactor with no behavioral gain.
- **Targets:** `AGENTS.md:20`.
- **Preconditions:** None.
- **Shared resources:** `AGENTS.md` — no other step writes it.
- **Acceptance:** The stated boundary matches the code a reader will find.
- **Verification:** Manual read against `mcp.py:11-24` and `:471-750`.

### Phase C — structural

**S18 — Rewire the three import cycles**
- **Outcome:** No first-party import cycle; deferred imports exist only where genuinely warranted, each with a comment saying why.
- **Change:** Break `agents->state->agents` and `cli->managed_wizard->cli` by moving the shared symbols to a module both sides can import, or by inverting the dependency. Then lift the deferred imports that exist solely to hide a cycle back to module scope. Preserve the intentional deferrals that exist for startup cost rather than cycles — `cli.py:814` (`ucode_version`) and `cli.py:846` (`mcp_proxy.serve`) look like the latter; confirm before touching them.
- **Targets:** `src/ucode/state.py:148`; `src/ucode/usage.py:506`; `src/ucode/agents/__init__.py:302-303`; `src/ucode/managed_wizard.py:106,126,535,604,620,697`; `src/ucode/cli.py:814,846`.
- **Preconditions:** Run **after** S3, S4, S11, S12, which touch `usage.py`, `state.py`, and `cli.py`.
- **Shared resources:** `state.py` (S4), `usage.py` (S3), `cli.py` (S11, S12), `managed_wizard.py` (S6). Strictly serial — last among the steps touching these files.
- **Acceptance:** An AST import-graph check finds zero cycles. The CLI still starts and all commands still dispatch.
- **Verification:** Re-run the planning AST cycle script -> 0 cycles. Then `uv run pytest -q` (full suite) plus `uv run ucode --help` and `uv run ucode status` as smoke checks, since import-order defects surface at startup and may evade unit tests.
- **Risk note:** Highest-regression step in the plan. Cycle-breaking can change import-time side-effect order. Keep it in its own commit, separate from Phase A.

**S19 — Rename `transport.py`'s cross-module private symbols** *(requires Q2 approval; default is defer)*
- **Outcome:** Symbols crossing module boundaries carry public names; genuinely private ones keep the underscore.
- **Change:** Rename the 8 externally imported symbols (`_http_get_json`, `_http_get_bytes`, `_http_post_json`, `_http_patch_json`, `_http_delete`, `_debug`, `_format_subprocess_result`, `_log_auth_diagnostics`) to public names and update all importers. No behavior change.
- **Targets:** `src/ucode/databricks/transport.py`; importers at `models.py:8`, `mcp_discovery.py:15`, `skills_download.py:11`, `managed.py:9-12`, `auth.py:17-19`; plus any test importing them. Also consider the same drift at `managed_wizard.py:126` (`mcp._skill_mcp_locations`) and `:535,697` (`cli._prompt_for_configuration`).
- **Preconditions:** **Q2 approval.** Run after S2, S10, S11, S13 to avoid churn.
- **Shared resources:** Conflicts with S2, S10, S11, S13, S15. Must run last in Phase C or be skipped.
- **Acceptance:** No cross-module import of an underscore-prefixed symbol from `transport.py`; suite green.
- **Verification:** `uv run pytest -q`; `uv run ruff check .`; `uv run ty check src/`; then grep to confirm no private cross-module imports remain.

**S20 — Extract the duplicated catalog/schema walk**
- **Outcome:** One helper owns catalog enumeration, schema fan-out, deadline handling, and partial-result policy.
- **Change:** Extract the shared orchestration from `list_uc_functions_catalog_schemas` (`:483-543`) and `list_all_mcp_services` (`:584-645`), parameterized by the per-schema probe that is the only real divergence. Both already delegate to `_paginated_json_items`, which stays the lower-level source of truth.
- **Targets:** `src/ucode/databricks/mcp_discovery.py:464-562`, `:565-668`; callers `mcp.py:216`, `mcp.py:375`; `tests/test_databricks_mcp_discovery.py`.
- **Preconditions:** Run **after** S10, which changes reason handling in the same functions.
- **Shared resources:** `src/ucode/databricks/mcp_discovery.py` (S10, S19). Serialize after S10.
- **Acceptance:** Behavior identical for both discovery paths, including deadline and partial-result semantics; the duplicated span is gone.
- **Verification:** `uv run pytest tests/test_databricks_mcp_discovery.py tests/test_mcp.py -q`.

**S21 — Remove the four test-only `config_io` helpers** *(Q1 affects only the dependency half)*
- **Outcome:** `read_toml_safe`, `write_toml_file`, `parse_dotenv`, and `write_dotenv` are gone along with their tests.
- **Change:** Delete the four helpers and their 12 test calls plus imports. Then, per Q1: `tomlkit` becomes unused in `src/` — **default is to leave the declaration in place** and record the follow-up rather than touch `pyproject.toml` and `uv.lock` without approval.
- **Targets:** `src/ucode/config_io.py:120-138`, `:141-173`; `tests/test_config_io.py:16-24` (imports), `:178-193`, `:223-271`; `pyproject.toml:26` only if Q1 is approved.
- **Preconditions:** Run **after** S1 (same two files). Q1 for the dependency half only.
- **Shared resources:** `src/ucode/config_io.py`, `tests/test_config_io.py` (S1). Serialize after S1.
- **Acceptance:** Symbols absent; suite green; `ruff` reports no unused `tomlkit` import in `config_io.py`.
- **Verification:** `uv run pytest tests/test_config_io.py -q`; `uv run ruff check .`; grep confirming zero remaining references.
- **Note:** If Q1 is approved, `uv.lock` changes. That is a deliberate dependency-graph change and must be called out in the commit message, per `AGENTS.md`.

**S22 — Reconnaissance: is `bootstrap.py` reachable from outside the repo?** *(no removal)*
- **Outcome:** F21 resolves to confirmed-dead or live. **Unblocks:** a later decision on whether to remove the module.
- **Evidence to gather:** Search release automation, deployment manifests, internal launch scripts, and documentation **outside** this repository for `python -m ucode.bootstrap` or `ucode.bootstrap`.
- **Targets:** external systems; `src/ucode/bootstrap.py:1-20` is the subject.
- **Preconditions:** None.
- **Shared resources:** None (read-only).
- **Acceptance:** A documented answer. If any external consumer exists, close F21 as not-dead and leave the module.
- **Verification:** Search scope and results recorded. **Do not remove the module in this plan** — reachability is `uncertain`.

### Explicitly deferred (no step; recorded with reason)

- **F19 — `configure_shared_state` is 193 lines.** Deferred. Severity `low` after refutation: its 8 parameters are real but nesting is flat sequential phases, and `tests/test_cli.py:611-747` already covers its branches. The scout's companion claim about `configure` was **withdrawn** during the audit — 11 of its 12 parameters are `typer.Option` declarations, so the shape is framework-imposed. Revisit only if a defect is traced to phase interaction.
- **F23 — five `managed_*` modules could be a subpackage.** Deferred. Severity `low`. `managed_config` has 8 importers including `state.py`, `usage.py`, `agents/__init__.py`, and `cli.py`, so it is cross-cutting rather than group-private, and a move would rewrite imports across the package for navigational benefit only. The audit deliberately proposed no target layout; that stands.
- **F21 — `bootstrap.py` removal.** Deferred to S22 reconnaissance. Reachability is `uncertain`; removal is not authorized.

---

## 5. Coverage

Every finding in the source audit maps to a step, a verification check, or a disposition. No item is dropped.

| Finding | Severity | Disposition |
| --- | --- | --- |
| F1 dry-run data loss | critical | **S1** |
| F2 manifest validator container types | high | **S6** |
| F3 discovery exceptions as "none found" | medium | **S10** |
| F4 partial pagination cached as complete | medium | **S11** |
| F5 managed read/absence conflation | medium | **S12** (messaging only; fail-open policy unchanged) |
| F6 `_http_get_bytes` timeout escape | medium | **S2** |
| F7 credential logging on auth failure | medium | **S13** (fix applied regardless of the open severity question) |
| F8 silent token-refresh failure | medium | **S14** |
| F9 stale files survive skill overwrite | medium | **S15** (Q3 approval) |
| F10 dot->dash collision | medium | **S16** recon -> **S16b** conditional fix |
| F11 malformed state crashes CLI | medium | **S4** |
| F12 empty `available_tools` fallthrough | medium | **S3** |
| F14 dependency guard covers 3 of 9 | medium | **S8** |
| F15 `AGENTS.md` boundary vs `mcp.py` TUI | medium | **S17** (document; widget not moved) |
| F16 `prompt_for_text` false contract | low | **S5** |
| F17 three import cycles | medium | **S18** |
| F18 `transport.py` private public API | medium | **S19** (Q2; default defer) |
| F19 `configure_shared_state` length | low | **Deferred** — reason in section 4 |
| F20 four test-only `config_io` helpers | medium | **S21** (Q1 gates only the `tomlkit` half) |
| F21 `bootstrap.py` reachability | medium | **S22** recon only; removal not authorized |
| F22 duplicated catalog/schema walk | medium | **S20** |
| F23 `managed_*` topology | low | **Deferred** — reason in section 4 |
| F24 audit report tracked in git | low | **S9** |
| SC1–SC5 success criteria | — | Section 2 table maps each to its step; V1–V2 in section 7 |
| Q1–Q4 open decisions | — | Section 3; each has a reversible default |

---

## 6. Delegation and sequencing

**Parallel group P1 (Phase A, no shared files):** S5 (`ui.py`), S6 (`managed_setup.py`), S8 (`test_lint.py`), S9 (`.gitignore`), and any one of S1/S2/S3/S4. S1–S4 each own distinct source files, so all four can also run concurrently — their only constraint is against later steps.

**Serial chains (shared-resource conflicts):**
- `config_io.py` + `tests/test_config_io.py`: **S1 -> S21**
- `transport.py`: **S2 -> S13 -> S19**
- `mcp_discovery.py`: **S10 -> S20 -> S19**
- `state.py`: **S4 -> S18**
- `usage.py`: **S3 -> S18**
- `cli.py`: **S11 -> S12 -> S18**
- `skills_download.py`: **S15 -> S19**
- S18 must follow every step touching `state.py`, `usage.py`, `cli.py`, `managed_wizard.py`.
- S19 must run last in Phase C, or be skipped under its default.

**Phase gates:** Phase A is independently shippable (D4). Do not start Phase C until Phase A and B are green, because S18 carries the highest regression risk and should not be debugged alongside behavioral changes.

**Read-only steps:** S16 and S22 are reconnaissance and must not modify code.

**Independent review requirements** (the implementation session applies its current `sub-agent-definitions` policy; no models, tiers, or agent names are specified here):
- **S1** — fixes a data-loss defect on a documented contract. Requires independent review.
- **S15** — introduces directory deletion. Requires independent review plus Q3 approval.
- **S18** — cycle-breaking with import-time side-effect risk. Requires independent review.
- **S19, S21** — cross-module rename and dependency-graph change. Require approval before implementation, then independent review.

---

## 7. Verification

**Baseline (already run):** `uv run --locked pytest --ignore=tests/test_e2e.py --ignore=tests/test_e2e_uc.py --ignore=tests/test_e2e_user_agent.py -q` -> **911 passed in 9.84s, 0 failed.** No pre-existing failures, so every post-change failure is a regression. `uv run ruff check .` -> "All checks passed!" (run during the audit).

| ID | Check | Covers | Expected |
| --- | --- | --- | --- |
| V1 | `uv run --locked pytest --ignore=tests/test_e2e.py --ignore=tests/test_e2e_uc.py --ignore=tests/test_e2e_user_agent.py -q` | SC4; every implementation step | >=911 passed (911 plus new tests), 0 failed |
| V2 | `uv run ruff check .`; `uv run pytest tests/test_lint.py -q` (covers `ruff format --check` and `ty check src/`) | SC5; all steps | All pass |
| V3 | `uv run pytest tests/test_config_io.py -q` | SC1 / S1, S21 | Pass, including the new dry-run rollback test |
| V4 | `uv run pytest tests/test_managed_setup.py tests/test_managed_wizard.py -q` | SC2 / S6 | Pass, including both malformed-container tests |
| V5 | `uv run pytest tests/test_databricks_transport.py tests/test_skills_download.py -q` | SC3 / S2, S13, S15 | Pass, including timeout and scrubbing tests |
| V6 | AST import-cycle script (the one used in planning) | S18 | 0 cycles |
| V7 | `uv run ucode --help`; `uv run ucode status` | S18 | CLI starts, commands dispatch, no ImportError |
| V8 | `git status --porcelain`; `git check-ignore -v slop-audit-20260831-82a161c.md` | S9 | Clean tree; report matches an ignore rule |

**Manual and unverifiable checks:**
- **S16** — resolved by documentation lookup, not a repository command.
- **S22** — resolved by searching systems outside this repository.
- **S15** — after implementing, manually confirm on a scratch directory that only the intended `root/leaf` path is removed. `not verified: no live Databricks workspace is available for an end-to-end skills download.`
- **Nothing in this plan is verified against a live Databricks workspace.** All Databricks-facing findings rest on traced static control flow.

**Verification limits:** these checks validate the plan's targets and the existing baseline. They do not verify that the planned behavior works. That requires implementation.

---

## 8. Risks and assumptions

| Risk / assumption | Impact | Mitigation | Owner / evidence needed |
| --- | --- | --- | --- |
| S18 cycle-breaking changes import-time side-effect order | High — could break CLI startup in a way unit tests miss | Own commit, separate from Phase A; V6 plus V7 startup smoke checks | Implementer |
| S6 makes previously-"valid" manifests fail | Medium — intended, but could break a user's existing file | Actionable error messages; check whether any test asserts a malformed shape passes | Implementer |
| S12 requires rewriting tests that assert silence | Medium — loosening them would hide the defect | Update assertions to the new message deliberately; record the reason in the commit | Implementer |
| S15 deletes user-visible files | Medium — destructive by design | Q3 approval; scope strictly to the approved `root/leaf`; re-verify `_is_valid_leaf` | User (Q3) |
| S21 + Q1 changes `uv.lock` | Low–Medium — `AGENTS.md` restricts lock-file edits | Default is to keep `tomlkit` declared; call out the change explicitly if approved | User (Q1) |
| S19 touches 5 modules for no behavioral gain | Low | Default is defer; run last if approved | User (Q2) |
| F7's real severity is unresolved | Low for the fix, higher for the finding | S13 is correct either way; severity revisits only if real CLI output shows token material | Needs real `databricks auth token` failure output |
| F10 may be unreachable | Low | S16 gates any change; S16b is conditional | Needs UC identifier rules |
| F21 may have an external consumer | Low | S22 recon only; no removal | Needs an external-repo search |
| Assumption: `restore_file` returning `False` under dry-run breaks no caller | Low | Verified: `cli.py:593`/`:937` discard the value; `cli.py:768-777` uses it for reporting only | Confirmed during planning |
| Assumption: the 911-test baseline is deterministic | Low | Re-run V1 before starting to confirm | Implementer |

---

## 9. Open questions and follow-ups

All four are low-impact for implementation and each has a reversible default, so the plan remains `Implementation-ready`. Q1–Q3 must be answered before their specific steps run, not before the plan starts.

1. **Q1 — remove `tomlkit` once S21 lands?** Default: keep it declared, note the follow-up. Blocks only the dependency half of S21.
2. **Q2 — rename `transport.py`'s 8 cross-module private symbols (S19)?** Default: defer. Blocks only S19.
3. **Q3 — approve directory removal on skill overwrite (S15)?** Default: implement scoped to the approved `root/leaf` only. Blocks only S15.
4. **Q4 — should `--dry-run` skip agent validation entirely?** Default: no. S1 makes the current path safe; skipping validation would change observable dry-run output. Pure follow-up, blocks nothing.

Additional follow-up: **S9 stages a commit.** Confirm the user has finished with `slop-audit-20260831-796b096.md` before committing its deletion.
