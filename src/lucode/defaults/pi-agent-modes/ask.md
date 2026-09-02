---
mode: ask
bash_policy: strict_readonly
enabled_tools:
  - read
  - bash
  - grep
  - find
  - ls
  - ask_user
description: "Resolve consequential missing requirements without implementation."
border_label: " ASK "
border_style: muted
prompt_suffix: |
  Gather discoverable repository context before asking. Use `ask_user` for one
  focused consequential question per call. Do not ask for facts available in the
  repository and do not edit files. When the request is complete, stop asking and
  summarize the agreed requirements, constraints, acceptance criteria, and open
  blockers.
---
# ASK Mode
Clarify only requirements that repository evidence cannot resolve.
