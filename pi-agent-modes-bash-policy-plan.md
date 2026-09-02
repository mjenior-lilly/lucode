# Pi agent-modes Bash policy implementation plan

## Goal

Configure `@neilurk12/pi-agent-modes` so new Pi sessions can run common inspection and exploratory verification commands without granting the entire Bash tool for each session. Keep PLAN, ASK, and ORCHESTRATOR on `strict_readonly`, with explicit exceptions for bounded package metadata checks, dependency dry runs, test execution, import-only Python probes, syntax checks, Node.js heredoc diagnostics, and selected runtime metadata probes. Preserve non-destructive Bash behavior in CODE, with narrowly defined exceptions for workspace-local file creation and movement. Expose the `ask_user` tool from the `pi-ask-user` extension in every mode except YOLO so agents can present structured questions through its custom interface.

This plan targets `@neilurk12/pi-agent-modes` 0.4.2. Its evaluator accepts a shell expression when any safe pattern matches and no destructive pattern matches. Because many built-in safe patterns are not end-anchored, `cat README.md; python -c "..."` can pass `strict_readonly` even though the Python suffix is not classified. Treat correction of that evaluator behavior as a prerequisite for calling this policy safe. Until the extension requires every command and pipeline stage to pass, the configuration below reduces approval prompts but does not enforce a read-only shell boundary. Review the installed extension before applying the plan to a later version because its configuration schema or pattern evaluator may change.

## Why persistent configuration is required

The extension intercepts each Pi `tool_call` independently of Pi's project-trust setting. Its interactive choices, including "Allow once" and "Allow for session," are held in memory and cleared when a new session initializes. `defaultProjectTrust: "always"` does not make these grants persistent.

The extension loads persistent user overrides from:

```text
$HOME/.pi/modes/config.yaml
```

The extension builds this path with Node.js `os.homedir()`. That normally resolves to the operating-system home directory, but it follows an overridden `HOME` value on this installation. Here, `HOME` and `os.homedir()` resolve to `/Users/L146025/.ucode/pi-home`, so the effective file is `/Users/L146025/.ucode/pi-home/.pi/modes/config.yaml`.

Do not edit the package's built-in mode Markdown or generated `dist/index.js`. Package installation or upgrade can overwrite those files.

## Policy design

The configuration has seven parts:

1. Shared safe patterns supplement the extension's built-in read commands. The added commands cover ripgrep-family search tools, read-only Git operations, path inspection, text filters, binary inspection, checksums, runtime discovery, syntax checks, and bounded package metadata inspection.
2. Exact patterns allow pytest and selected cross-language test runs, Python probes whose source contains only import statements, Node.js heredoc diagnostics, and a Ruby YAML availability probe. These are execution exceptions rather than read-only commands because tests, imported modules, project configuration, and Node.js source can have side effects.
3. Direct `python`, `python3`, `.venv/bin/python`, and `venv/bin/python` probes are covered in addition to `uv run python`. The import grammar permits comma-separated imports such as `import json, re` but not statements following a semicolon.
4. Shell conditionals are allowed only when the evaluator parses the complete compound command and validates every condition and every branch. The specific `UCODE_USER_NAME` probe uses non-mutating `[[ -v ... ]]` and `[[ -n ... ]]` tests and prints only a status label, not the variable value.
5. PLAN, ASK, and ORCHESTRATOR use `strict_readonly`. CODE uses `non_destructive`.
6. CODE marks a bounded set of otherwise destructive filesystem commands as allowed. These patterns accept syntactically relative paths and reject absolute paths, parent traversal, shell substitution, and unrestricted shell composition.
7. PLAN, ASK, ORCHESTRATOR, and CODE expose `ask_user`. YOLO needs no override because its empty `enabled_tools` list already exposes every registered baseline tool.

The extension already treats Pi's native `read`, `grep`, `find`, and `ls` tools separately from Bash. Those tools do not pass through `bash_policy`. Shell invocations such as `bash("grep ...")` do pass through the Bash policy and depend on the command patterns below.

Mode overrides replace `enabled_tools` arrays rather than appending to them. Each mode with a restricted list must therefore repeat the complete built-in list while adding `ask_user`; an override containing only `ask_user` would disable the mode's other tools. CODE retains `enabled_tools: []`, which means all baseline tools and therefore includes `ask_user` without a separate permission override. The `pi-ask-user` extension must be installed and loaded so the tool exists in the baseline catalog.

## Configuration to install

Create `$HOME/.pi/modes/config.yaml` with this exact content:

```yaml
# Shared command families that supplement the extension's built-in safelist
# (cat, grep, find, rg, fd, bat, eza, jq, and standard inspection commands).
# The uv and language-execution additions are fully anchored. The evaluator
# prerequisite must also prevent a built-in safe fragment from authorizing an
# unknown command elsewhere in the same shell expression.
bash_patterns:
  safe:
    add:
      - '(?:^|[;&|]{1,2})\s*(?:rg|ripgrep|ack|ag|pt|sift)\b'
      - '(?:^|[;&|]{1,2})\s*git\s+(?:grep|ls-files|rev-parse|name-rev|describe)\b'
      - '(?:^|[;&|]{1,2})\s*(?:locate|mdfind|realpath|readlink|basename|dirname|pathchk)\b'
      - '(?:^|[;&|]{1,2})\s*(?:cut|paste|join|comm|cmp|nl|column|tr)\b'
      - '(?:^|[;&|]{1,2})\s*(?:strings|hexdump|xxd|od|shasum|sha256sum|md5sum|md5)\b'
      - '^uv[ \t]+(?:-V|--version)$'
      - '^uv[ \t]+help(?:$|[ \t])[^;&|`$<>\r\n]*$'
      - '^uv[ \t]+[\w-]+[ \t]+--help$'
      - '^uv[ \t]+[\w-]+[ \t]+--help[ \t]*\|[ \t]*sed[ \t]+-n[ \t]+(["\x27])[0-9]+,[0-9]+p\1$'
      - '^uv[ \t]+[\w-]+[ \t]+--help[ \t]*\|[ \t]*sed[ \t]+-n[ \t]+(["\x27])[0-9]+,[0-9]+p\1[ \t]*;[ \t]*printf[ \t]+(["\x27])[^;&|`$<>\r\n]*\2$'
      - '^uv[ \t]+(?:tree|version)(?:[ \t]+--(?:offline|no-cache|no-progress|no-config))*$'
      - '^uv[ \t]+pip[ \t]+(?:list|freeze|show|check|tree)\b[^;&|`$<>\r\n]*$'
      - '^uv[ \t]+sync\b(?=[^;&|`$<>\r\n]*--(?:dry-run|check)\b)[^;&|`$<>\r\n]*$'
      - '^uv[ \t]+run(?:[ \t]+--(?:no-sync|locked|frozen))*[ \t]+(?:pytest|python[ \t]+-m[ \t]+pytest)\b[^;&|`$<>\r\n]*$'
      - '^uv[ \t]+run(?:[ \t]+--(?:no-sync|locked|frozen))*[ \t]+protocol-builder[ \t]+run[ \t]+resume[ \t]+--help$'
      - '^uv[ \t]+run[ \t]+python[ \t]+-c[ \t]+(["\x27])(?:import|from)[^;&|`$<>\r\n]+\1$'
      - '^uv[ \t]+run[ \t]+python[ \t]+-[ \t]+<<["\x27]?([A-Za-z_]\w*)["\x27]?\r?\n(?:[ \t]*(?:import|from)[^;&|`$<>\r\n]+\r?\n)+\1[ \t]*$'
      - '^(?:(?:\.venv|venv)/bin/)?python3?[ \t]+-c[ \t]+(["\x27])(?:import|from)[^;&|`$<>\r\n]+\1$'
      - '^(?:(?:\.venv|venv)/bin/)?python3?[ \t]+-[ \t]+<<["\x27]?([A-Za-z_]\w*)["\x27]?\r?\n(?:[ \t]*(?:import|from)[^;&|`$<>\r\n]+\r?\n)+\1[ \t]*$'
      - '^(?:(?:\.venv|venv)/bin/)?python3?[ \t]+-m[ \t]+pip[ \t]+(?:list|freeze|check)$'
      - '^(?:(?:\.venv|venv)/bin/)?python3?[ \t]+-m[ \t]+pip[ \t]+show[ \t]+[\w.-]+$'
      - '^uv[ \t]+(?:-V|--version)[ \t]+&&[ \t]+uv[ \t]+sync\b(?=[^;&|`$<>\r\n]*--(?:dry-run|check)\b)[^;&|`$<>\r\n]*$'
      - '^node(?:[ \t]+--input-type=(?:module|commonjs))?[ \t]+<<["\x27]?([A-Za-z_]\w*)["\x27]?\r?\n[\s\S]*\r?\n\1[ \t]*$'
      - '^node[ \t]+--check[ \t]+(?!/)(?![^\r\n]*\.\.)[\w.@%+=:,/ -]+$'
      - '^(?:pytest|(?:(?:\.venv|venv)/bin/)?python3?[ \t]+-m[ \t]+pytest|(?:\.venv|venv)/bin/pytest)\b[^;&|`$<>\r\n]*$'
      - '^node[ \t]+--test\b[^;&|`$<>\r\n]*$'
      - '^(?:npm|pnpm|yarn|bun)[ \t]+(?:test|run[ \t]+test)\b[^;&|`$<>\r\n]*$'
      - '^(?:go|cargo|dotnet|swift)[ \t]+test\b[^;&|`$<>\r\n]*$'
      - '^(?:mvn|gradle)[ \t]+test\b[^;&|`$<>\r\n]*$'
      - '^bundle[ \t]+exec[ \t]+rspec\b[^;&|`$<>\r\n]*$'
      - '^(?:vendor/bin/)?phpunit\b[^;&|`$<>\r\n]*$'
      - '^command[ \t]+-v[ \t]+[\w.-]+$'
      - '^command -v python python3 node ruby perl php go cargo rustc java javac mvn gradle dotnet swift R Rscript sqlite3 psql jq yq 2>/dev/null \|\| true$'
      - '^(?:python3?|cargo|rustc)[ \t]+(?:-V|--version)$'
      - '^(?:node|ruby|perl|php|mvn|gradle|npm|pnpm|yarn|bun)[ \t]+(?:-v|--version)$'
      - '^(?:dotnet|swift|R|Rscript|sqlite3|psql|jq|yq)[ \t]+--version$'
      - '^(?:java|javac)[ \t]+-version$'
      - '^go[ \t]+version$'
      - '^(?:bash|sh|zsh)[ \t]+-n[ \t]+(?!/)(?![^\r\n]*\.\.)[\w.@%+=:,/ -]+$'
      - '^ruby[ \t]+-c[ \t]+(?!/)(?![^\r\n]*\.\.)[\w.@%+=:,/ -]+$'
      - '^php[ \t]+-l[ \t]+(?!/)(?![^\r\n]*\.\.)[\w.@%+=:,/ -]+$'
      - '^gofmt[ \t]+-d[ \t]+(?!/)(?![^\r\n]*\.\.)[\w.@%+=:,/ -]+$'
      - '^rustfmt[ \t]+--check[ \t]+(?!/)(?![^\r\n]*\.\.)[\w.@%+=:,/ -]+$'
      - '^cargo[ \t]+metadata[ \t]+--no-deps[ \t]+--offline[ \t]+--locked$'
      - '^cargo[ \t]+tree[ \t]+--offline[ \t]+--locked$'
      - '^dotnet[ \t]+--(?:info|list-sdks|list-runtimes)$'
      - '^sqlite3[ \t]+-readonly[ \t]+[^;&|`$<>\r\n]+[ \t]+(["\x27])\.(?:schema|tables)\1$'
      - '^ruby[ \t]+-e[ \t]+\x27require[ \t]+"yaml";[ \t]+puts[ \t]+YAML\.class\x27$'
      - '^command[ \t]+-v[ \t]+ruby;[ \t]*ruby[ \t]+-e[ \t]+\x27require[ \t]+"yaml";[ \t]+puts[ \t]+YAML\.class\x27$'
  # Permit only a final stderr-to-terminal/null redirect for bounded read
  # commands, plus a bounded find-to-filter pipeline. Classify mutating uv
  # commands and arbitrary ruby -e programs as destructive in every mode,
  # then allow only the exact inspection exceptions registered below.
  destructive:
    add:
      - '(?:^|[;&|]{1,2})\s*uv\s+(?:auth|init|add|remove|version|sync|lock|format|venv|build|publish)\b'
      - '(?:^|[;&|]{1,2})\s*uv\s+(?:pip\s+(?:install|uninstall|sync)|tool\s+(?:install|uninstall|upgrade)|python\s+(?:install|uninstall|upgrade)|cache\s+(?:clean|prune)|self\s+update)\b'
      - '(?:^|[;&|]{1,2})\s*ruby\s+-e\b'
      - '^command -v python python3 node ruby perl php go cargo rustc java javac mvn gradle dotnet swift R Rscript sqlite3 psql jq yq 2>/dev/null \|\| true$'
      - '^uv[ \t]+(?:tree|version)(?:[ \t]+--(?:offline|no-cache|no-progress|no-config))*$'
      - '^uv[ \t]+sync\b(?=[^;&|`$<>\r\n]*--(?:dry-run|check)\b)[^;&|`$<>\r\n]*$'
      - '^uv[ \t]+(?:-V|--version)[ \t]+&&[ \t]+uv[ \t]+sync\b(?=[^;&|`$<>\r\n]*--(?:dry-run|check)\b)[^;&|`$<>\r\n]*$'
      - '^ruby[ \t]+-e[ \t]+\x27require[ \t]+"yaml";[ \t]+puts[ \t]+YAML\.class\x27$'
      - '^command[ \t]+-v[ \t]+ruby;[ \t]*ruby[ \t]+-e[ \t]+\x27require[ \t]+"yaml";[ \t]+puts[ \t]+YAML\.class\x27$'
      - '^(?:cat|head|tail|less|more|grep|rg|ripgrep|ack|ag|pt|sift|ls|fd|bat|eza)\b[^;&|`$<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$'
      - '^find\b(?![^\r\n]*-(?:delete|exec|execdir|ok|okdir)\b)(?![^\r\n]*\$(?!HOME\b))[^;&|`<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$'
      - '^find\b(?![^\r\n]*-(?:delete|exec|execdir|ok|okdir)\b)(?![^\r\n]*\$(?!HOME\b))[^;&|`<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)[ \t]*\|[ \t]*(?:head|tail|wc)\b[^;&|`$<>\r\n]*$'
      - '^find "\$HOME/\.ucode/pi-home/\.pi/agent" -path \x27[^\x27\r\n]+\x27 -print 2>/dev/null \| head -20; command -v ruby; ruby -e \x27require "yaml"; puts YAML\.class\x27$'
      - '^(?:file|stat|du|df|tree|which|whereis|type|env|printenv|uname|whoami|id|date|cal|uptime|ps)\b[^;&|`$<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$'
      - '^git[ \t]+(?:status|log|diff|show|branch|remote|grep|ls-files|rev-parse|name-rev|describe)\b[^;&|`$<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$'
      - '^(?:jq|cut|paste|join|comm|cmp|nl|column|tr|strings|hexdump|xxd|od|shasum|sha256sum|md5sum|md5)\b[^;&|`$<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$'
    severity:
      '^command -v python python3 node ruby perl php go cargo rustc java javac mvn gradle dotnet swift R Rscript sqlite3 psql jq yq 2>/dev/null \|\| true$': allow
      '^uv[ \t]+(?:tree|version)(?:[ \t]+--(?:offline|no-cache|no-progress|no-config))*$': allow
      '^uv[ \t]+sync\b(?=[^;&|`$<>\r\n]*--(?:dry-run|check)\b)[^;&|`$<>\r\n]*$': allow
      '^uv[ \t]+(?:-V|--version)[ \t]+&&[ \t]+uv[ \t]+sync\b(?=[^;&|`$<>\r\n]*--(?:dry-run|check)\b)[^;&|`$<>\r\n]*$': allow
      '^ruby[ \t]+-e[ \t]+\x27require[ \t]+"yaml";[ \t]+puts[ \t]+YAML\.class\x27$': allow
      '^command[ \t]+-v[ \t]+ruby;[ \t]*ruby[ \t]+-e[ \t]+\x27require[ \t]+"yaml";[ \t]+puts[ \t]+YAML\.class\x27$': allow
      '^(?:cat|head|tail|less|more|grep|rg|ripgrep|ack|ag|pt|sift|ls|fd|bat|eza)\b[^;&|`$<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$': allow
      '^find\b(?![^\r\n]*-(?:delete|exec|execdir|ok|okdir)\b)(?![^\r\n]*\$(?!HOME\b))[^;&|`<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$': allow
      '^find\b(?![^\r\n]*-(?:delete|exec|execdir|ok|okdir)\b)(?![^\r\n]*\$(?!HOME\b))[^;&|`<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)[ \t]*\|[ \t]*(?:head|tail|wc)\b[^;&|`$<>\r\n]*$': allow
      '^find "\$HOME/\.ucode/pi-home/\.pi/agent" -path \x27[^\x27\r\n]+\x27 -print 2>/dev/null \| head -20; command -v ruby; ruby -e \x27require "yaml"; puts YAML\.class\x27$': allow
      '^(?:file|stat|du|df|tree|which|whereis|type|env|printenv|uname|whoami|id|date|cal|uptime|ps)\b[^;&|`$<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$': allow
      '^git[ \t]+(?:status|log|diff|show|branch|remote|grep|ls-files|rev-parse|name-rev|describe)\b[^;&|`$<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$': allow
      '^(?:jq|cut|paste|join|comm|cmp|nl|column|tr|strings|hexdump|xxd|od|shasum|sha256sum|md5sum|md5)\b[^;&|`$<>\r\n]*[ \t]+2>[ \t]*(?:/dev/null|&1)$': allow

plan:
  bash_policy: strict_readonly
  enabled_tools:
    - read
    - bash
    - grep
    - find
    - ls
    - questionnaire
    - ask_user_question
    - ask_user

ask:
  bash_policy: strict_readonly
  enabled_tools:
    - read
    - bash
    - grep
    - find
    - ls
    - questionnaire
    - ask_user_question
    - ask_users
    - ask_user

orchestrator:
  bash_policy: strict_readonly
  enabled_tools:
    - Agent
    - bash
    - subagent
    - get_subagent_result
    - steer_subagent
    - todo
    - read
    - grep
    - find
    - ls
    - questionnaire
    - ask_user_question
    - ask_user
    - request_mode_switch

code:
  bash_policy: non_destructive
  enabled_tools: [] # All registered baseline tools, including ask_user.
  # CODE may perform syntactically relative creation, copying, moving, linking,
  # and simple text output. These patterns reject absolute/path-option forms,
  # parent traversal, shell substitution, and unrestricted command composition.
  bash_patterns:
    destructive:
      add:
        - '^mkdir(?: -p)? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/ -]+$'
        - '^touch (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/ -]+$'
        - '^cp(?: -[Rrpaifn]{1,8})? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+ [\w.@%+=:,/ -]+$'
        - '^mv(?: -[fin]{1,3})? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+ [\w.@%+=:,/ -]+$'
        - '^ln(?: -[sfn]{1,3})? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+ [\w.@%+=:,/-]+$'
        - '^(?:printf|echo) [^;&|`$<>\r\n]*>{1,2} *(?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+$'
        - '^(?:printf|echo) [^;&|`$<>\r\n]*\| *tee(?: -a)? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+$'
      severity:
        '^mkdir(?: -p)? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/ -]+$': allow
        '^touch (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/ -]+$': allow
        '^cp(?: -[Rrpaifn]{1,8})? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+ [\w.@%+=:,/ -]+$': allow
        '^mv(?: -[fin]{1,3})? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+ [\w.@%+=:,/ -]+$': allow
        '^ln(?: -[sfn]{1,3})? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+ [\w.@%+=:,/-]+$': allow
        '^(?:printf|echo) [^;&|`$<>\r\n]*>{1,2} *(?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+$': allow
        '^(?:printf|echo) [^;&|`$<>\r\n]*\| *tee(?: -a)? (?!/)(?![^\r\n]*\.\.)(?![^\r\n]*(?: -| /|=/))[\w.@%+=:,/-]+$': allow
```

The same regex must appear in `destructive.add` and `destructive.severity`. The first entry registers the pattern with the destructive-pattern engine. The matching `severity: allow` entry converts that exact match from blocked to allowed.

## Evaluator prerequisite

Before treating `strict_readonly` as a security boundary, change or upgrade the extension so it validates the complete shell expression. The accepted implementation must satisfy all of these rules:

1. Split shell lists, Boolean chains, and pipelines with a real shell parser, not a regular expression.
2. Require every simple command to match an allowed complete-command form.
3. Apply destructive checks to every stage before considering an allow-severity override.
4. Treat redirections as operations attached to a parsed command. Allow only the exact stderr redirects and read-only cases documented in this plan.
5. For `if`, `case`, loops, and shell Boolean constructs, validate every condition and every branch statically. Do not exempt a branch because it would be unreachable for the current environment.
6. Permit shell test primitives only through an operation-aware allowlist. For the documented environment probe, allow `[[ -v NAME ]]` and the exact nonblank check `[[ -n "${UCODE_USER_NAME//[[:space:]]/}" ]]`; reject command substitution, file mutation, regex code execution, and unsupported test operators.
7. Reject command substitution, process substitution, unsupported shell syntax, and nested shell evaluators unless an exact exception covers the complete expression.
8. Add regression tests for safe prefixes and suffixes around Python, Node.js, Ruby, redirection, installers, and every branch of a shell conditional.

If changing the extension is outside the deployment scope, a stricter configuration-only fallback is to remove every built-in unanchored safe pattern and replace it with fully anchored equivalents. That fallback will reject many quoted and compound commands because regex cannot parse shell grammar. Do not edit generated `dist/index.js` in an installed package. Make the evaluator change in the extension source and install the reviewed build through the approved Artifactory path.

## Apply the configuration

1. Confirm that `@neilurk12/pi-agent-modes` and `pi-ask-user` are installed and active. Confirm that Pi registers a tool named exactly `ask_user`.
2. Confirm that the evaluator prerequisite is implemented. Run the malicious compound-command tests before installing the broader exploratory exceptions.
3. Create `$HOME/.pi/modes` if it does not exist.
4. Back up an existing `$HOME/.pi/modes/config.yaml` before replacing or merging it.
5. Write the configuration above to `$HOME/.pi/modes/config.yaml`.
6. Run `/mode reload` in an existing Pi session. New sessions load the file automatically.
7. Run `/mode status` and check that PLAN, ASK, and ORCHESTRATOR report `strict_readonly`, while CODE reports `non_destructive`.
8. Switch through PLAN, ASK, ORCHESTRATOR, and CODE. Confirm that `ask_user` remains available in each mode and opens the structured question interface. YOLO requires no override because it exposes all registered tools.

Do not replace unrelated existing mode overrides. Merge this configuration with them and keep one `bash_patterns` object at the document root and at most one object for each mode.

## Verification matrix

Verify the policy in a disposable workspace. Do not run destructive test commands against valuable files.

### Expected without a Bash approval prompt in every configured mode

```text
pwd
ls
find . -maxdepth 2 -type f
rg "pattern" .
git status --short
git ls-files
realpath .
ls missing-path 2>/dev/null
rg "pattern" . 2>/dev/null
uv --version
uv run --help
uv run protocol-builder run resume --help
uv run --help | sed -n '1,180p'; printf '\n--- PYTEST ---\n'
uv --version && uv sync --dry-run
uv sync --check
uv tree
uv pip list
uv run pytest -q
uv run python -m pytest -q
uv run python -c "import pytest_asyncio"
uv run python - <<'PY'
import pytest_asyncio
PY
python3 - <<'PY'
import json, re
from pathlib import Path
PY
.venv/bin/python -c 'import json, re'
python3 -m pip list
node <<'NODE'
const patterns = [];
console.log(patterns.length);
NODE
node --input-type=module <<'NODE'
import path from 'node:path';
console.log(path.sep);
NODE
node --check scripts/example.js
node --test
npm test
go test ./...
cargo test
python --version
node --version
go version
java -version
bash -n setup.sh
ruby -c scripts/example.rb
php -l scripts/example.php
gofmt -d main.go
rustfmt --check src/main.rs
cargo metadata --no-deps --offline --locked
dotnet --list-sdks
sqlite3 -readonly example.sqlite '.schema'
command -v python python3 node ruby perl php go cargo rustc java javac mvn gradle dotnet swift R Rscript sqlite3 psql jq yq 2>/dev/null || true
if [[ -v UCODE_USER_NAME ]]; then if [[ -n "${UCODE_USER_NAME//[[:space:]]/}" ]]; then echo 'UCODE_USER_NAME=set_nonblank'; else echo 'UCODE_USER_NAME=set_blank'; fi; else echo 'UCODE_USER_NAME=unset'; fi
command -v ruby
ruby -e 'require "yaml"; puts YAML.class'
command -v ruby; ruby -e 'require "yaml"; puts YAML.class'
grep -E 'read-only|readonly|uv|find' pi-agent-modes-bash-policy-plan.md
find "$HOME/.ucode/pi-home/.pi/agent" "$HOME/.pi" -path '*pi-agent-modes*' -maxdepth 8 -print 2>/dev/null | head -100
find "$HOME/.ucode/pi-home/.pi/agent" -path '*/js-yaml/package.json' -print 2>/dev/null | head -20; command -v ruby; ruby -e 'require "yaml"; puts YAML.class'
```

Pi's native `read`, `grep`, `find`, and `ls` tools should also run without a Bash approval prompt because they are not Bash calls. The `ask_user` tool should remain visible and callable in PLAN, ASK, ORCHESTRATOR, and CODE.

### Expected without a Bash approval prompt only in CODE

Run these only in a disposable directory:

```text
mkdir scratch
touch scratch/example.txt
cp scratch/example.txt scratch/copy.txt
mv scratch/copy.txt scratch/moved.txt
ln -s scratch/example.txt example-link
printf sample > scratch/output.txt
echo sample | tee scratch/tee-output.txt
```

### Expected to remain blocked or require explicit approval

```text
rm scratch/example.txt
rmdir scratch
sudo any-command
git add .
git commit -m test
git push
npm install package-name
pip install package-name
uv add package-name
uv sync
uv run python -c "open('output.txt', 'w')"
python3 -c "open('output.txt', 'w').write('data')"
python3 - <<'PY'
from pathlib import Path
Path('output.txt').write_text('data')
PY
python3 - <<'PY'
import json; open('output.txt', 'w').write('data')
PY
ruby -e 'File.write("output.txt", "data")'
mkdir /tmp/outside-workspace
cp scratch/example.txt ../outside.txt
find . -delete 2>/dev/null
find "$PATH" -print 2>/dev/null | head -100
uv run protocol-builder run resume --help (
if [[ -v UCODE_USER_NAME ]]; then echo safe; else touch output.txt; fi
if [[ -v UCODE_USER_NAME ]]; then echo safe; else curl https://example.invalid; fi
```

The trailing `(` in the malformed `protocol-builder` example is unmatched shell syntax and must remain blocked. The conditional examples must be rejected even when the unsafe `else` branch would not run for the current environment.

Also test commands that append or prepend an unclassified interpreter to a safe command:

```text
cat README.md; python3 -c "open('output.txt', 'w').write('data')"
python3 -c "open('output.txt', 'w').write('data')"; cat README.md
printf safe && node -e "require('node:fs').writeFileSync('output.txt', 'data')"
```

All three must remain blocked under `strict_readonly`. With the unmodified 0.4.2 evaluator, the first and third can pass because a built-in safe pattern matches one fragment of the compound expression. This verification is the acceptance test for the evaluator prerequisite, not a configuration-only test.

## Expected behavior and limits

This configuration reduces Bash session-approval requests. It does not eliminate every request. Unknown commands, unsupported syntax, or commands classified as destructive still trigger the extension's policy flow. Removing all Bash prompts would require `bash_policy: off` or `permissions.bash: allow`, either of which bypasses the protections preserved by this plan.

The extension matches regular expressions rather than parsing a shell abstract syntax tree. In 0.4.2, safe patterns use `some(pattern.test(command))`; they do not require the pattern to cover the full expression or require every shell stage to pass. Quoting, aliases, wrapper scripts, multiline commands, and command composition therefore may not classify as intended. Correct the evaluator to parse the shell expression or require complete anchored coverage before relying on `strict_readonly`. Keep configuration exceptions anchored with `^` and `$`, exclude shell metacharacters, and prefer exact command forms. The explicit `uv --version && uv sync --dry-run` pattern is necessary because a start-only pattern for `uv --version` could otherwise authorize an unknown command after `&&`.

Do not solve the evaluator defect by broadly safelisting wrappers such as `bash -c`, `sh -c`, `eval`, `env`, `xargs`, `make`, `task`, `npx`, or `npm exec`. Each can execute an arbitrary nested command. If the extension cannot validate every parsed stage, keep compound expressions outside the safelist except for exact, reviewed forms.

`uv sync --dry-run` does not write the lockfile or modify the project environment, and `uv sync --check` only checks synchronization. They may still read or populate uv's external cache unless `--no-cache` is used. Test runners execute repository code and can write files, alter external services, or trigger plugin side effects. Plain `uv run` can also synchronize the environment and lockfile before execution. Prefer `--no-sync`, `--locked`, or `--frozen` when the task permits. The Python probe patterns constrain source text to lines beginning with `import` or `from`, reject semicolons and shell metacharacters, and support comma-separated imports. Importing a module can still execute arbitrary module initialization. These commands are intentionally allowed exploratory-execution exceptions to `strict_readonly`, not guarantees of side-effect-free behavior.

The direct Python patterns cover `python`, `python3`, `.venv/bin/python`, and `venv/bin/python`. They deliberately reject absolute interpreter paths and other virtual-environment names. `py_compile` and `compileall` remain outside the strict safelist because they normally create `__pycache__` files. `python -m json.tool` is not included because its optional second positional argument writes an output file and regex cannot reliably infer the intended arity after shell quoting.

The Node.js heredoc patterns accept arbitrary JavaScript between a matching delimiter pair, including `node --input-type=module`. Regex cannot distinguish diagnostic JavaScript from code that writes files, invokes subprocesses, reads credentials, or uses the network. These are explicit unrestricted-language execution exceptions. Remove them if PLAN, ASK, and ORCHESTRATOR must provide a security boundary rather than a prompt-reduction policy. The Ruby exception is narrower and permits only the exact `require "yaml"; puts YAML.class` probe. Global destructive patterns prevent arbitrary `ruby -e` programs and mutating `uv` subcommands from passing CODE's `non_destructive` policy; exact severity overrides preserve the documented `uv sync` dry-run/check, `uv version`, Ruby inspection, and runtime-discovery commands.

Syntax-only checks are safer than general interpreter execution, but they are not identical across languages. `node --check`, `ruby -c`, `php -l`, shell `-n`, `gofmt -d`, and `rustfmt --check` are included with relative paths. `perl -c` is excluded because Perl compile checks can execute `BEGIN`, `CHECK`, and similar blocks. Project-aware linters and formatters such as ESLint, Prettier, Ruff, mypy, RuboCop, and TypeScript can load repository configuration or plugins and remain execution exceptions requiring separate review.

Metadata commands are constrained to forms that do not intentionally install dependencies or update project state. Cargo metadata requires `--no-deps --offline --locked`; SQLite inspection requires `-readonly` and an exact `.schema` or `.tables` command. Maven, Gradle, Swift Package Manager, Go package listing, and similar dependency-resolution commands can download dependencies, populate caches, execute project configuration, or update generated state, so only their version probes are included.

`uv run protocol-builder run resume --help` is an exact execution exception. `--help` does not guarantee that a console script is side-effect-free, and plain `uv run` can synchronize the environment before invoking it. Prefer `uv run --no-sync protocol-builder run resume --help` when supported, but do not broaden the regex to arbitrary executables ending in `--help` because programs may ignore that flag or perform initialization first. An unmatched trailing `(` is invalid shell syntax and is not part of the allowed command.

The `UCODE_USER_NAME` conditional reads only variable state and emits one of three fixed labels. It does not print the variable value. A general `if` safelist would be unsafe because commands in any condition or branch can mutate files, invoke subprocesses, use the network, or expose data. The unmodified 0.4.2 evaluator blocks this probe because its safe patterns do not recognize commands following the `then` and `else` reserved words. Do not work around that result with a broad `if` or `echo` fragment pattern. The evaluator must parse the compound command and validate all branches.

The runtime-discovery exception allows the exact multi-runtime inventory command, including its final `2>/dev/null || true`. The same anchored regex appears in `safe.add`, `destructive.add`, and `destructive.severity` because the built-in redirection rule otherwise classifies it as destructive. The ordinary `command -v NAME` pattern remains limited to one command name and no composition.

The find redirect exceptions allow literal paths and `$HOME` expansion only. Other variable expansions, command substitution, mutation actions such as `-delete` and `-exec`, additional pipelines, and output redirection remain outside the exception. One exact compound pattern covers the reported js-yaml search followed by `command -v ruby` and the Ruby YAML probe. It does not permit other commands after the pipeline.

A Bash command written as `grep /read-only|readonly|uv|find/` contains unquoted pipe operators, not a single grep regular expression. Use `grep -E 'read-only|readonly|uv|find' file`. Pi's native `grep` tool does not pass through this Bash policy, including interfaces that display its pattern as `/.../`.

The CODE write patterns enforce syntactically relative paths, not a filesystem boundary. A relative path can escape through an existing symbolic link. Regex policy cannot reliably prevent that. Use a sandbox or operating-system filesystem controls when a hard workspace boundary is required.

The configuration cannot provide `ask_user` if the `pi-ask-user` extension is absent, fails to load, or registers the tool under another name. Verify the registered tool name on each machine before applying the mode lists.

Package upgrades may add built-in read patterns, alter destructive patterns, rename tools, or change user-override semantics. Reinspect the installed packages and rerun the verification matrix after upgrading `@neilurk12/pi-agent-modes` or `pi-ask-user`.

## Completion criteria

The implementation is complete when:

1. The extension evaluator requires every command and pipeline stage to pass, or equivalent complete-command coverage prevents a built-in safe fragment from authorizing an unclassified suffix.
2. The persistent override exists at `$HOME/.pi/modes/config.yaml`.
3. All four restricted modes load their intended Bash policy.
4. `ask_user` is available in PLAN, ASK, ORCHESTRATOR, and CODE without reducing each mode's existing tool set.
5. The inspection, runtime-discovery, syntax-check, and exploratory-execution matrix runs without session-wide Bash grants.
6. The bounded CODE write matrix runs only in CODE.
7. Destructive, external-path, Git mutation, installer, privileged, and malicious compound commands remain blocked or require explicit approval.

If criterion 1 cannot be met with the installed extension, report the policy as prompt reduction only. Do not claim that PLAN, ASK, or ORCHESTRATOR provides a read-only Bash security boundary.
