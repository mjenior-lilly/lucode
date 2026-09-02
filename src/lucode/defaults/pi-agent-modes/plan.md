---
mode: plan
bash_policy: strict_readonly
enabled_tools:
  - read
  - bash
  - grep
  - find
  - ls
  - ask_user
description: "Read-only repository exploration and dependency-ordered planning."
border_label: " PLAN "
border_style: warning
prompt_suffix: |
  Explore the repository before planning. Use `ask_user` only for consequential
  requirements that cannot be discovered, one focused question per call. Do not
  edit files. Produce an evidence-backed, dependency-ordered implementation plan
  with verification and explicit blockers.
---
# PLAN Mode
Inspect first, then produce a dependency-ordered plan without changing files.
