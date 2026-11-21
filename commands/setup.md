---
description: Install the Ganivra status line (session cost, context, plan limits, today's spend, project)
allowed-tools: Bash(ganivra-cc:*)
---

!`ganivra-cc install-statusline`

The text above is the installer's output; the user cannot see it, so quote the values you need.
The status line files are now in place. Finish the setup with the user:

1. Show the user the current and the proposed `statusLine` values printed above, and the settings
   file path. Explain in one sentence what the line will show, for example:
   `Opus 5.5 · $1.84 · ctx 71% · 5h 82% → 15:40 · today $36.99 · metering`
   (on Pro/Max plans, costs show as ≈ API value, and plan-limit usage appears once Claude Code reports it).
2. Ask for confirmation before changing anything. If a different status line is already set, say it
   will be replaced and ask explicitly.
3. On a yes, edit only the `statusLine` key of that settings file to the proposed value, keeping
   every other setting exactly as it is. Create the file with just that key if it does not exist.
4. Tell the user the line appears on Claude Code's next update, and that `/ganivra:setup` should be
   run again after updating the plugin.
