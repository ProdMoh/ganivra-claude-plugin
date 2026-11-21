---
description: Claude Code cost by project, model and day, whether your plan is worth it, and what the same tokens would cost on other models
argument-hint: "[--days N] [--csv] [--plan pro|max|ultra|api] [--plan-price USD]"
allowed-tools: Bash(ganivra-cc:*)
---

!`ganivra-cc report $ARGUMENTS`

The text above is the output of the Ganivra usage report, computed locally from this machine's
Claude Code session logs. **The user cannot see it**: it was inserted into your instructions, not shown
on screen.

Your reply must start with the complete report, reproduced verbatim as Markdown: every heading, table
and number, in order. Do not summarise, shorten, round, recompute or add figures. If the output is CSV,
reproduce all of it in a code block and offer to save it to a file the user names.

After the full report, add at most one sentence pointing out the biggest cost driver.
