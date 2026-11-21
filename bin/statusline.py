#!/usr/bin/env python3
"""Ganivra status line for Claude Code.

Reads the JSON Claude Code passes on stdin and prints one line, for example:
    Opus 5.5 · $1.84 · ctx 71% · 5h 42% → 15:40 · today $6.20 · metering

Session cost, context and plan-limit figures come from Claude Code itself; "today" adds up every
session's usage today from local logs at API list rates. Never fails: any error prints a minimal line.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def _core():
    spec = importlib.util.spec_from_file_location("ganivra_usage", os.path.join(HERE, "ganivra_usage.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def color(text, code):
    if os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def level(value):
    """Plain under 70%, yellow from 70%, red from 90%."""
    text = f"{value:.0f}%"
    if value >= 90:
        return color(text, "31")
    if value >= 70:
        return color(text, "33")
    return text


def money(value):
    return f"${value:,.2f}" if value < 100 else f"${value:,.0f}"


def build(data, today=None):
    parts = []
    model = (data.get("model") or {}).get("display_name") or (data.get("model") or {}).get("id")
    if model:
        parts.append(model)
    limits = data.get("rate_limits") or {}
    subscription = bool(limits)
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(("≈" if subscription else "") + money(cost))
    used = (data.get("context_window") or {}).get("used_percentage")
    if isinstance(used, (int, float)):
        parts.append("ctx " + level(used))
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = limits.get(key) or {}
        value = window.get("used_percentage")
        if not isinstance(value, (int, float)) or (key == "seven_day" and value < 50):
            continue
        text = f"{label} {level(value)}"
        resets = window.get("resets_at")
        if isinstance(resets, (int, float)) and value >= 50:
            text += f" → {datetime.fromtimestamp(resets):%H:%M}"
        parts.append(text)
    if today is not None:
        parts.append(f"today {'≈' if subscription else ''}{money(today)}")
    workspace = data.get("workspace") or {}
    project = (workspace.get("repo") or {}).get("name") or os.path.basename(
        (workspace.get("project_dir") or data.get("cwd") or "").rstrip("/"))
    if project:
        parts.append(color(project, "2"))
    return color(" · ", "2").join(parts)


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    today = None
    try:
        # The cache lives beside this installed copy, whatever GANIVRA_HOME was at install time.
        today = _core().today_cost(cache_path=os.path.join(HERE, "cache", "today.json"))
    except Exception:  # the status line must never break Claude Code
        pass
    try:
        print(build(data, today))
    except Exception:
        print("ganivra")


if __name__ == "__main__":
    main()
