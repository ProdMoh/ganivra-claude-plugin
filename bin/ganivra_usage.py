#!/usr/bin/env python3
"""Ganivra for Claude Code: local cost and context reports from Claude Code session logs.

Reads only usage counts, model names, timestamps, tool and skill names, and the working
directory from ~/.claude/projects (or $CLAUDE_CONFIG_DIR/projects). Prompt and response text is
never stored or printed. Nothing is sent over the network.

Usage:
    python3 -I ganivra_usage.py audit  [--days N]
    python3 -I ganivra_usage.py report [--days N] [--csv]
    python3 -I ganivra_usage.py install-statusline
"""
import glob
import json
import os
import re
import shutil
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PRICE_PATHS = (os.path.join(HERE, "..", "data", "prices.json"), os.path.join(HERE, "prices.json"))
INSTALL_DIR = os.path.join(os.environ.get("GANIVRA_HOME") or os.path.expanduser("~/.ganivra"), "claude-code")
COMPARE = ("claude-sonnet-5-5", "claude-haiku-5-5")
COMMAND_TAG = re.compile(r"<command-name>/?([A-Za-z0-9_.:-]+)</command-name>")
CHARS_PER_TOKEN = 4


# ---------------------------------------------------------------- pricing

def load_prices():
    for path in PRICE_PATHS:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    return {"models": {}, "fast_multiplier": {}, "as_of": "unknown"}


class Pricer:
    def __init__(self, data=None):
        data = data or load_prices()
        self.models = data.get("models", {})
        self.fast = data.get("fast_multiplier", {})
        self.as_of = data.get("as_of", "unknown")
        self._resolved = {}

    def resolve(self, model):
        """Catalog ID for a logged model name (dated, Bedrock/Vertex and [1m] forms included), or None."""
        if model in self._resolved:
            return self._resolved[model]
        name = (model or "").lower().split("[")[0].strip()
        found = None
        start = name.find("claude-")
        if start >= 0:
            name = re.sub(r"(-v\d+(:\d+)?|@\d{8})$", "", name[start:])
            for candidate in (name, re.sub(r"-\d{8}$", "", name), re.sub(r"-latest$", "", name)):
                if candidate in self.models:
                    found = candidate
                    break
        self._resolved[model] = found
        return found

    def rates(self, model, prompt_tokens=0, speed=None):
        p = self.models.get(model)
        if not p or p.get("input") is None:
            return None
        base = p["input"]
        r = {
            "input": base,
            "cache_read": p.get("cached_input") if p.get("cached_input") is not None else base * 0.1,
            "write_5m": p.get("cache_write_5m") if p.get("cache_write_5m") is not None else base * 1.25,
            "write_1h": base * 2,
            "output": p["output"],
        }
        over = p.get("long_prompt_over")
        if over and prompt_tokens > over and p.get("long_prompt_input"):
            factor = p["long_prompt_input"] / base
            r = {k: (v * factor if k != "output" else p.get("long_prompt_output") or v * factor)
                 for k, v in r.items()}
        if speed == "fast" and model in self.fast:
            r = {k: v * self.fast[model] for k, v in r.items()}
        return r

    def cost(self, model, t, speed=None):
        """USD for one request's token parts on `model` (a catalog ID), or None when unpriced."""
        r = self.rates(model, t["input"] + t["cache_read"] + t["write_5m"] + t["write_1h"], speed)
        if r is None:
            return None
        return sum(t[k] * r[k] for k in ("input", "cache_read", "write_5m", "write_1h", "output")) / 1e6


def token_parts(usage):
    def n(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    written = n(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else None
    write_1h = n(split.get("ephemeral_1h_input_tokens")) if split else 0
    write_1h = min(write_1h, written)
    return {
        "input": n(usage.get("input_tokens")),
        "cache_read": n(usage.get("cache_read_input_tokens")),
        "write_5m": written - write_1h,
        "write_1h": write_1h,
        "output": n(usage.get("output_tokens")),
    }


def prompt_tokens(t):
    return t["input"] + t["cache_read"] + t["write_5m"] + t["write_1h"]


def display_model(model):
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d))?", model or "")
    if not m:
        return model or "unknown"
    return f"{m.group(1).title()} {m.group(2)}" + (f".{m.group(3)}" if m.group(3) else "")


# ---------------------------------------------------------------- reading logs

def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def log_files(since=None):
    root = os.path.join(config_dir(), "projects")
    files = glob.glob(os.path.join(root, "*", "*.jsonl"))
    files += glob.glob(os.path.join(root, "*", "*", "subagents", "*.jsonl"))
    if since is not None:
        cutoff = since.timestamp()
        files = [f for f in files if os.path.getmtime(f) >= cutoff]
    return sorted(files)


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


_repo_cache = {}


def project_of(cwd):
    """The git repository's folder name for a working directory, else the directory's name."""
    if not cwd:
        return "unknown"
    if cwd in _repo_cache:
        return _repo_cache[cwd]
    path, name = cwd, None
    while path and os.path.isdir(path):
        if os.path.exists(os.path.join(path, ".git")):
            name = os.path.basename(path)
            break
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    _repo_cache[cwd] = name or os.path.basename(cwd.rstrip("/")) or cwd
    return _repo_cache[cwd]


def server_key(name):
    """MCP server names as they appear inside tool names (mcp__<server>__<tool>)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name or "")


def plugin_of_skill(name):
    return name.split(":", 1)[0] if ":" in name else None


def plugin_of_server(name):
    m = re.match(r"plugin[:_]([A-Za-z0-9-]+)[:_]", name or "")
    return m.group(1) if m else None


class Session:
    def __init__(self, sid):
        self.id = sid
        self.first_prompt_chars = None
        self.start_cwd = None                    # where the session started: its project
        self.started = None                      # first main-chain request, even before the window
        self.mcp_tokens = defaultdict(int)       # server -> estimated tokens loaded every request
        self.skill_tokens = {}                   # skill name -> estimated tokens
        self.used_servers = set()
        self.used_skills = set()
        self.saw_loaded_record = False


def _text_length(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return 0


def _skill_tokens(attachment):
    names = [n for n in attachment.get("names") or [] if isinstance(n, str)]
    content = attachment.get("content") if isinstance(attachment.get("content"), str) else ""
    lines = content.splitlines()
    sizes = {}
    for name in names:
        prefix = f"- {name}:"
        line = next((l for l in lines if l.startswith(prefix)), None)
        sizes[name] = len(line) if line is not None else None
    known = [v for v in sizes.values() if v is not None]
    fallback = (len(content) - sum(known)) / max(1, sum(1 for v in sizes.values() if v is None))
    return {k: round((v if v is not None else fallback) / CHARS_PER_TOKEN) for k, v in sizes.items()}


def read_logs(since, pricer):
    """Requests (one per API message) and per-session metadata from every log touched since `since`."""
    requests, sessions = {}, {}
    for path in log_files(since):
        subagent = os.sep + "subagents" + os.sep in path
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"type"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                kind = d.get("type")
                sid = d.get("sessionId")
                if not sid or kind not in ("assistant", "user", "attachment"):
                    continue
                s = sessions.get(sid) or sessions.setdefault(sid, Session(sid))
                if s.start_cwd is None and not subagent and d.get("cwd"):
                    s.start_cwd = d["cwd"]
                if kind == "attachment" and not subagent:
                    _read_attachment(s, d.get("attachment"))
                elif kind == "user" and not subagent:
                    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        s.used_skills.update(COMMAND_TAG.findall(content))
                    if s.first_prompt_chars is None and not d.get("isMeta") and not d.get("isSidechain"):
                        if isinstance(content, str) or any(isinstance(b, dict) and b.get("type") == "text"
                                                           for b in content or []):
                            s.first_prompt_chars = _text_length(content)
                elif kind == "assistant":
                    _read_assistant(d, s, subagent, since, pricer, requests)
    for r in requests.values():
        r["project"] = project_of(sessions[r["session"]].start_cwd or r["cwd"])
    return list(requests.values()), sessions


def _read_attachment(s, a):
    if not isinstance(a, dict):
        return
    t = a.get("type")
    if t in ("deferred_tools_delta", "mcp_instructions_delta", "skill_listing"):
        s.saw_loaded_record = True
    if t == "deferred_tools_delta":
        names = a.get("addedNames") or []
        lines = a.get("addedLines") or names
        for name, text in zip(names, lines):
            if isinstance(name, str) and name.startswith("mcp__"):
                server = name.split("__")[1]
                s.mcp_tokens[server] += max(1, round(len(str(text)) / CHARS_PER_TOKEN))
    elif t == "mcp_instructions_delta":
        for name, block in zip(a.get("addedNames") or [], a.get("addedBlocks") or []):
            s.mcp_tokens[server_key(name)] += round(len(str(block)) / CHARS_PER_TOKEN)
    elif t == "skill_listing":
        s.skill_tokens.update(_skill_tokens(a))


def _read_assistant(d, s, subagent, since, pricer, requests):
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    usage = msg.get("usage")
    model = msg.get("model")
    for block in msg.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name") or ""
        if name.startswith("mcp__"):
            s.used_servers.add(name.split("__")[1])
        elif name == "Skill":
            skill = (block.get("input") or {}).get("skill")
            if isinstance(skill, str):
                s.used_skills.add(skill)
    if not isinstance(usage, dict) or not model or model.startswith("<"):
        return
    when = parse_time(d.get("timestamp"))
    if when is not None and not subagent and not d.get("isSidechain"):
        s.started = when if s.started is None else min(s.started, when)
    if when is None or (since is not None and when < since):
        return
    key = (s.id, msg.get("id") or d.get("requestId") or d.get("uuid"))
    t = token_parts(usage)
    catalog = pricer.resolve(model)
    speed = usage.get("speed")
    requests[key] = {  # streamed blocks repeat one message; the last entry has the final usage
        "session": s.id,
        "time": when,
        "model": catalog or model,
        "priced": catalog is not None,
        "speed": speed,
        "tokens": t,
        "cost": pricer.cost(catalog, t, speed) if catalog else None,
        "subagent": subagent or bool(d.get("isSidechain")),
        "cwd": d.get("cwd"),
    }


# ---------------------------------------------------------------- formatting

def usd(value):
    if value is None:
        return "unpriced"
    if value >= 100:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.3f}"


def kt(tokens):
    if tokens >= 1_000_000:
        return f"{tokens / 1e6:.1f}M"
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}k"
    return str(int(tokens))


def pct(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "—"


def window(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def footer(pricer):
    return (f"_Costs at Anthropic API list rates, prices as of {pricer.as_of}. Subscription plans pay a flat fee; "
            "read these as the value of the usage. Local only: nothing left this machine._")


# ---------------------------------------------------------------- audit

def audit(days=30, out=sys.stdout):
    pricer = Pricer()
    since = window(days)
    requests, sessions = read_logs(since, pricer)
    w = out.write
    w(f"## Claude Code context audit, last {days} days\n\n")
    if not requests:
        w("No Claude Code usage found in this period.\n")
        return
    by_session = defaultdict(list)
    for r in requests:
        by_session[r["session"]].append(r)
    total = sum(r["cost"] or 0 for r in requests)
    w(f"{len(by_session)} sessions · {len(requests):,} requests · {usd(total)} at API rates\n\n")

    # Only sessions that started inside the window: a session that began earlier has no start here,
    # and its first request in the window carries the whole conversation so far.
    opened = {}
    for sid, rs in by_session.items():
        main = sorted((r for r in rs if not r["subagent"]), key=lambda r: r["time"])
        if main and sessions[sid].started is not None and sessions[sid].started >= since:
            prompt = (sessions[sid].first_prompt_chars or 0) / CHARS_PER_TOKEN
            opened[sid] = (main, max(0, prompt_tokens(main[0]["tokens"]) - prompt))
    starts = [base for _, base in opened.values()]
    # A resumed session's first request also carries history; cap each base for the cost estimate.
    cap = 1.5 * statistics.median(starts) if starts else 0
    carry_cost, weekly = 0.0, defaultdict(list)
    for sid, (main, base) in opened.items():
        first = main[0]
        weekly[(first["time"] - timedelta(days=first["time"].weekday())).date()].append(base)
        base = min(base, cap)
        if first["priced"]:
            # The base's share of the first request's input-side cost, at that request's actual cache mix.
            t = dict(first["tokens"], output=0)
            carry_cost += (pricer.cost(first["model"], t, first["speed"]) or 0) * base / max(1, prompt_tokens(t))
            carry_cost += sum(base * pricer.rates(r["model"], prompt_tokens(r["tokens"]), r["speed"])["cache_read"]
                              / 1e6 for r in main[1:] if r["priced"])
    if starts:
        med = statistics.median(starts)
        w("### Starting context (tokens loaded before your first prompt)\n\n")
        w("| Measure | Tokens |\n| --- | --- |\n")
        ordered = sorted(starts)
        low, high = ordered[len(ordered) // 10], ordered[min(len(ordered) - 1, (len(ordered) * 9) // 10)]
        w(f"| Median | {kt(med)} |\n| Typical range (10th–90th percentile) | {kt(low)} – {kt(high)} |\n")
        trend = " · ".join(f"{week:%b %d}: {kt(statistics.median(v))}" for week, v in sorted(weekly.items())[-5:])
        w(f"| By week (median) | {trend} |\n\n")
        carried = sum(1 for r in requests if r["session"] in opened and not r["subagent"])
        session_total = sum(r["cost"] or 0 for r in requests if r["session"] in opened)
        w(f"Carrying it: loading it at the start of each session and re-reading it from the cache on every later "
          f"request cost about **{usd(carry_cost)}** across {len(opened)} sessions started in this period "
          f"({carried:,} requests), {pct(carry_cost, session_total)} of their usage.\n\n")
        visible = [sum(s.mcp_tokens.values()) + sum(s.skill_tokens.values())
                   for s in sessions.values() if s.saw_loaded_record and s.id in by_session]
        if visible:
            v = statistics.median(visible)
            w(f"Of the median {kt(med)}, about **{kt(v)}** comes from MCP servers and skills you can switch off. "
              f"The other {kt(max(0, med - v))} is Claude Code's own system prompt and built-in tools, plus your "
              "CLAUDE.md and memory files.\n\n")

    rows, actions = loaded_vs_used(sessions, by_session, pricer)
    if rows:
        recorded = sum(1 for s in sessions.values() if s.saw_loaded_record and s.id in by_session)
        w(f"### Loaded vs used ({recorded} sessions recorded what was loaded)\n\n")
        w("| Source | Kind | ~Tokens per request | Loaded in | Used in |\n| --- | --- | --- | --- | --- |\n")
        for row in rows[:15]:
            w(f"| {row['name']} | {row['kind']} | {kt(row['tokens'])} | {row['loaded']} sessions | "
              f"{row['used']} ({pct(row['used'], row['loaded'])}) |\n")
        if len(rows) > 15:
            w(f"| … {len(rows) - 15} more | | | | |\n")
        w("\nToken sizes are estimates from what Claude Code recorded (instructions, tool names, skill "
          "descriptions). Run `/context` in a session for its exact breakdown.\n\n")
    else:
        w("### Loaded vs used\n\nThis Claude Code version did not record which MCP servers and skills were "
          "loaded. Run `/context` in a session to see its breakdown.\n\n")

    w("### Top actions\n\n")
    if actions:
        for i, a in enumerate(actions[:3], 1):
            w(f"{i}. **{a['label']}** — loaded in {a['loaded']} sessions, used in {a['used']}. About "
              f"{kt(a['tokens'])} tokens per request; roughly {usd(a['saving'])} over {days} days.\n")
    else:
        w("Nothing loaded in many sessions goes unused. Your setup is lean.\n")
    w("\n")
    if starts:
        unused = sum(a["tokens"] for a in actions[:3])
        line = (f"My Claude Code sessions start with {kt(statistics.median(starts))} tokens before I type. "
                f"Carrying that cost {usd(carry_cost)} in {days} days, {pct(carry_cost, session_total)} of my usage.")
        if unused >= 5000:
            line += f" {kt(unused)} of it is things I almost never use."
        w(f"### Share\n\n> {line} (ganivra audit)\n\n")
    w(footer(pricer) + "\n")


def loaded_vs_used(sessions, by_session, pricer):
    stats = {}

    def add(key, name, kind, tokens, used, sid):
        row = stats.setdefault(key, {"name": name, "kind": kind, "tokens": [], "loaded": 0, "used": 0,
                                     "sessions": []})
        row["tokens"].append(tokens)
        row["loaded"] += 1
        row["used"] += int(used)
        row["sessions"].append(sid)

    for s in sessions.values():
        if s.id not in by_session or not s.saw_loaded_record:
            continue
        for server, tokens in s.mcp_tokens.items():
            add(("mcp", server), server, "MCP server", tokens, server in s.used_servers, s.id)
        for skill, tokens in s.skill_tokens.items():
            used = skill in s.used_skills or skill.split(":")[-1] in s.used_skills
            add(("skill", skill), skill, "skill", tokens, used, s.id)

    rows = []
    for row in stats.values():
        row["tokens"] = statistics.median(row["tokens"])
        rows.append(row)
    rows.sort(key=lambda r: r["tokens"] * r["loaded"], reverse=True)

    # Group plugin skills and plugin MCP servers so an action can say "disable plugin X".
    groups = {}
    for (kind, key), row in stats.items():
        plugin = plugin_of_skill(key) if kind == "skill" else plugin_of_server(key)
        gkey = ("plugin", plugin) if plugin else (kind, key)
        g = groups.setdefault(gkey, {"tokens": 0, "sessions": set(), "used_sessions": set(), "members": 0})
        g["tokens"] += row["tokens"]
        g["members"] += 1
        g["sessions"].update(row["sessions"])
    for s in sessions.values():
        for gkey, g in groups.items():
            if s.id not in g["sessions"]:
                continue
            kind, key = gkey
            if kind == "plugin":
                hit = any(plugin_of_skill(k) == key for k in s.used_skills) or \
                    any(plugin_of_server(k) == key for k in s.used_servers)
            elif kind == "mcp":
                hit = key in s.used_servers
            else:
                hit = key in s.used_skills or key.split(":")[-1] in s.used_skills
            if hit:
                g["used_sessions"].add(s.id)

    actions = []
    for (kind, key), g in groups.items():
        loaded, used = len(g["sessions"]), len(g["used_sessions"])
        if loaded < 5 or used / loaded >= 0.05 or g["tokens"] < 200:
            continue
        saving = 0.0
        for sid in g["sessions"]:
            for r in by_session[sid]:
                if r["priced"] and not r["subagent"]:
                    saving += g["tokens"] * pricer.rates(r["model"], prompt_tokens(r["tokens"]))["cache_read"] / 1e6
        if kind == "plugin":
            label = f"Disable the `{key}` plugin when you are not using it"
        elif kind == "mcp":
            label = f"Turn off the `{key}` MCP server outside the projects that need it"
        else:
            label = f"Remove or disable the `{key}` skill"
        actions.append({"label": label, "loaded": loaded, "used": used, "tokens": g["tokens"], "saving": saving})
    actions.sort(key=lambda a: a["saving"], reverse=True)
    return rows, actions


# ---------------------------------------------------------------- report

def report(days=30, as_csv=False, out=sys.stdout):
    pricer = Pricer()
    requests, _ = read_logs(window(days), pricer)
    w = out.write
    if as_csv:
        w("date,project,model,requests,input_tokens,cache_read_tokens,cache_write_tokens,output_tokens,cost_usd\n")
        rows = defaultdict(lambda: [0, 0, 0, 0, 0, 0.0])
        for r in requests:
            t = r["tokens"]
            row = rows[(r["time"].astimezone().date().isoformat(), r["project"], r["model"])]
            for i, v in enumerate((1, t["input"], t["cache_read"], t["write_5m"] + t["write_1h"], t["output"])):
                row[i] += v
            row[5] += r["cost"] or 0
        for (day, project, model), v in sorted(rows.items()):
            w(f"{day},{_csv(project)},{_csv(model)},{v[0]},{v[1]},{v[2]},{v[3]},{v[4]},{v[5]:.4f}\n")
        return

    w(f"## Claude Code usage, last {days} days\n\n")
    if not requests:
        w("No Claude Code usage found in this period.\n")
        return
    total = sum(r["cost"] or 0 for r in requests)
    sessions = len({r["session"] for r in requests})
    sub = sum(r["cost"] or 0 for r in requests if r["subagent"])
    w(f"**{usd(total)}** at API rates · {sessions} sessions · {len(requests):,} requests · "
      f"subagents {usd(sub)} ({pct(sub, total)})\n\n")

    def table(title, key, limit=10):
        groups = defaultdict(lambda: [0.0, 0, set()])
        for r in requests:
            g = groups[key(r)]
            g[0] += r["cost"] or 0
            g[1] += 1
            g[2].add(r["session"])
        w(f"### {title}\n\n| {title.split()[-1].title()} | Cost | Share | Sessions | Requests |\n"
          "| --- | --- | --- | --- | --- |\n")
        ordered = sorted(groups.items(), key=lambda kv: kv[1][0], reverse=True)
        for name, (c, n, ss) in ordered[:limit]:
            w(f"| {name} | {usd(c)} | {pct(c, total)} | {len(ss)} | {n:,} |\n")
        if len(ordered) > limit:
            rest = sum(v[0] for _, v in ordered[limit:])
            w(f"| {len(ordered) - limit} others | {usd(rest)} | {pct(rest, total)} | | |\n")
        w("\n")

    table("By project", lambda r: r["project"])
    table("By model", lambda r: display_model(r["model"]) + (" (fast)" if r["speed"] == "fast" else ""))

    daily = defaultdict(float)
    for r in requests:
        daily[r["time"].astimezone().date()] += r["cost"] or 0
    w("### By day\n\n| Day | Cost |\n| --- | --- |\n")
    for day in sorted(daily)[-14:]:
        w(f"| {day:%a %b %d} | {usd(daily[day])} |\n")
    w("\n")

    priced = [r for r in requests if r["priced"]]
    lines = []
    for target in COMPARE:
        if target not in pricer.models or all(r["model"] == target for r in priced):
            continue
        same = sum(pricer.cost(target, r["tokens"]) or 0 for r in priced)
        change = f"+{pct(same - total, total)}" if same >= total else f"−{pct(total - same, total)}"
        lines.append(f"| {display_model(target)} | {usd(same)} | {change} |")
    if lines:
        w("### The same tokens on other models\n\n| Model | Cost | vs actual |\n| --- | --- | --- |\n")
        w("\n".join(lines) + "\n\n")
        w("Same token counts priced on each model. A different model may need more or fewer turns for the "
          "same work, so treat this as a ceiling on savings, not a forecast.\n\n")
    unpriced = [r for r in requests if not r["priced"]]
    if unpriced:
        models = ", ".join(sorted({r["model"] for r in unpriced}))
        w(f"{len(unpriced):,} requests on models without a bundled price are excluded: {models}.\n\n")
    w(footer(pricer) + "\n")


def _csv(value):
    value = str(value)
    return '"' + value.replace('"', '""') + '"' if any(c in value for c in ',"\n') else value


# ---------------------------------------------------------------- status line support

def today_cost(cache_path=None, pricer=None):
    """Today's cost (local date) across all sessions, read incrementally so the status line stays fast."""
    pricer = pricer or Pricer()
    cache_path = cache_path or os.path.join(INSTALL_DIR, "cache", "today.json")
    today = datetime.now().astimezone().date().isoformat()
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    if cache.get("date") != today:
        cache = {"date": today, "files": {}}
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    for path in log_files(midnight):
        entry = cache["files"].setdefault(path, {"offset": 0, "costs": {}})
        try:
            size = os.path.getsize(path)
            if size < entry["offset"]:
                entry.update(offset=0, costs={})
            if size == entry["offset"]:
                continue
            with open(path, "rb") as fh:
                fh.seek(entry["offset"])
                chunk = fh.read()
        except OSError:
            continue
        end = chunk.rfind(b"\n") + 1
        entry["offset"] += end
        for raw in chunk[:end].splitlines():
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            when = parse_time(d.get("timestamp"))
            if d.get("type") != "assistant" or not isinstance(msg.get("usage"), dict) or when is None:
                continue
            if when.astimezone().date().isoformat() != today:
                continue
            model = pricer.resolve(msg.get("model"))
            if model:
                key = msg.get("id") or d.get("requestId") or d.get("uuid")
                entry["costs"][key] = pricer.cost(model, token_parts(msg["usage"]), msg["usage"].get("speed"))
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return sum(c or 0 for f in cache["files"].values() for c in f["costs"].values())


def install_statusline(out=sys.stdout):
    """Copy the status line and its pricing into ~/.ganivra and print the settings change to make."""
    os.makedirs(INSTALL_DIR, exist_ok=True)
    for name in ("statusline.py", "ganivra_usage.py"):
        shutil.copy2(os.path.join(HERE, name), os.path.join(INSTALL_DIR, name))
    for path in PRICE_PATHS:
        if os.path.exists(path):
            shutil.copy2(path, os.path.join(INSTALL_DIR, "prices.json"))
            break
    settings_path = os.path.join(config_dir(), "settings.json")
    current = None
    try:
        with open(settings_path, encoding="utf-8") as fh:
            current = json.load(fh).get("statusLine")
    except (OSError, ValueError):
        pass
    command = f'python3 -I "{os.path.join(INSTALL_DIR, "statusline.py")}"'
    proposed = {"type": "command", "command": command}
    out.write(f"Installed status line files in {INSTALL_DIR}\n\n")
    out.write(f"Settings file: {settings_path}\n")
    out.write("Current statusLine: " + (json.dumps(current) if current else "none") + "\n")
    out.write("Proposed statusLine: " + json.dumps(proposed) + "\n")
    if current and current != proposed:
        out.write("\nAn existing status line is set. Replacing it removes it; to keep it, leave settings as they "
                  "are.\n")


# ---------------------------------------------------------------- CLI

def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    days = 30
    if "--days" in argv:
        try:
            days = max(1, min(365, int(argv[argv.index("--days") + 1])))
        except (IndexError, ValueError):
            print("--days needs a number of days, e.g. --days 7")
            return 2
    command = argv[0]
    if command == "audit":
        audit(days)
    elif command == "report":
        report(days, as_csv="--csv" in argv)
    elif command == "install-statusline":
        install_statusline()
    else:
        print(f"Unknown command: {command}\n{__doc__}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
