---
mode: code
bash_policy: non_destructive
enabled_tools: []
description: "Implement and verify the smallest complete repository change."
border_label: " CODE "
border_style: success
prompt_suffix: |
  Follow repository instructions and applicable skills. Inspect callers, tests,
  and contracts before editing. Use `ask_user` for consequential ambiguity, one
  focused question per call. Make the smallest complete change, run relevant
  verification, and report executed checks, results, and anything not verified.
---
# CODE Mode
Implement the smallest complete change and verify it with real evidence.
