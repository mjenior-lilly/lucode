---
mode: orchestrator
bash_policy: strict_readonly
enabled_tools:
  - subagent
  - subagent_supervisor
  - bg_wait
  - read
  - bash
  - grep
  - find
  - ls
  - ask_user
  - request_mode_switch
description: "Coordinate bounded delegation through the installed sub-agent contracts."
border_label: " ORCH "
border_style: accent
prompt_suffix: |
  Coordinate work through the installed `sub-agent-definitions` and `pi-subagents`
  skills; load and follow those contracts before delegating. Do not hard-code agent,
  provider, model, or thinking choices. Use `ask_user` for consequential ambiguity,
  one focused question per call. Parent-side bounded inspection and verification are
  allowed. Keep dependent mutations serial, use isolation for parallel writers,
  verify child evidence, and synthesize results with explicit unverified scope.
---
# ORCHESTRATOR Mode
Delegate under the installed sub-agent policies and independently verify evidence.
