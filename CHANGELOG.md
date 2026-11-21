# Changelog

## 0.2.0 — 2026-11-21

- `/ganivra:report` now opens with a plan verdict: your usage at API rates as a monthly figure, next to
  your plan's price ("Your plan covered 18.8x its price in usage. Keep it.").
- Set the plan once with `--plan pro|max|ultra|team-standard|team-premium|api` or `--plan-price USD`.
  It is saved in `~/.ganivra/check.json`, shared with the Ganivra seat check, so a plan you chose there
  is picked up automatically.

## 0.1.0 — 2026-11-21

First release.

- `/ganivra:audit`: tokens loaded before your first prompt (median, range, weekly trend), what carrying
  them costs, how much comes from MCP servers and skills you can switch off, loaded-vs-used for each,
  top three actions, and a one-line summary to share.
- `/ganivra:report [--days N] [--csv]`: cost by project, model and day, and the same tokens priced on
  Sonnet 5.5 and Haiku 5.5.
- `/ganivra:setup`: status line with session cost, context used, plan-limit usage, today's total and
  project.
- Local only: reads token counts and metadata from Claude Code's logs; no network requests.
- Bundled Anthropic price snapshot from llm-prices (as of 2026-11-21).
