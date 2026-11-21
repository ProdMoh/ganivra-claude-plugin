import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gu = load("ganivra_usage", "bin/ganivra_usage.py")
sl = load("statusline", "bin/statusline.py")

PRICES = {
    "as_of": "2026-11-21",
    "fast_multiplier": {"claude-opus-5-5": 2.0},
    "models": {
        "claude-opus-5-5": {"input": 4, "cached_input": 0.2, "output": 20, "cache_write_5m": 5},
        "claude-haiku-5-5": {"input": 0.1, "cached_input": 0.01, "output": 0.5, "cache_write_5m": 0.125,
                             "long_prompt_over": 100000, "long_prompt_input": 0.5, "long_prompt_output": 2.5},
        "claude-sonnet-5-5": {"input": 2, "cached_input": 0.1, "output": 10, "cache_write_5m": 2.5},
    },
}


def ts(minutes_ago=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def assistant(sid, mid, usage, model="claude-opus-5-5", minutes_ago=5, tools=(), cwd="/tmp/proj", **extra):
    content = [{"type": "tool_use", "name": n, "input": i} for n, i in tools]
    return {"type": "assistant", "sessionId": sid, "timestamp": ts(minutes_ago), "cwd": cwd,
            "message": {"id": mid, "model": model, "usage": usage, "content": content}, **extra}


def usage(inp=0, read=0, w5=0, w1=0, out=0, speed="standard"):
    return {"input_tokens": inp, "cache_read_input_tokens": read, "cache_creation_input_tokens": w5 + w1,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1},
            "output_tokens": out, "speed": speed}


class PricingTest(unittest.TestCase):
    def setUp(self):
        self.p = gu.Pricer(PRICES)

    def test_resolve_variants(self):
        for name in ("claude-opus-5-5", "claude-opus-5-5[1m]", "us.anthropic.claude-opus-5-5-v1:0",
                     "claude-opus-5-5@20261101", "claude-opus-5-5-20261101"):
            self.assertEqual(self.p.resolve(name), "claude-opus-5-5", name)
        self.assertIsNone(self.p.resolve("gpt-6-luna"))
        self.assertIsNone(self.p.resolve("<synthetic>"))

    def test_cache_writes_and_reads(self):
        t = gu.token_parts(usage(inp=1_000_000, read=1_000_000, w5=1_000_000, w1=1_000_000, out=1_000_000))
        self.assertAlmostEqual(self.p.cost("claude-opus-5-5", t), 4 + 0.2 + 5 + 8 + 20)

    def test_haiku_long_prompt_raises_every_rate(self):
        short = gu.token_parts(usage(inp=100_000, out=1000))
        long = gu.token_parts(usage(inp=100_001, out=1000))
        self.assertAlmostEqual(self.p.cost("claude-haiku-5-5", short), (100_000 * 0.1 + 1000 * 0.5) / 1e6)
        self.assertAlmostEqual(self.p.cost("claude-haiku-5-5", long), (100_001 * 0.5 + 1000 * 2.5) / 1e6)

    def test_fast_mode_only_where_published(self):
        t = gu.token_parts(usage(inp=1_000_000))
        self.assertAlmostEqual(self.p.cost("claude-opus-5-5", t, "fast"), 8)
        self.assertAlmostEqual(self.p.cost("claude-sonnet-5-5", t, "fast"), 2)

    def test_old_logs_without_split_count_as_5m_writes(self):
        t = gu.token_parts({"input_tokens": 0, "cache_creation_input_tokens": 1_000_000})
        self.assertEqual((t["write_5m"], t["write_1h"]), (1_000_000, 0))


class LogsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.dir
        self.pricer = gu.Pricer(PRICES)
        gu.Pricer.__init__.__defaults__ = (PRICES,)
        self.project = os.path.join(self.dir, "projects", "-tmp-proj")
        os.makedirs(self.project)

    def tearDown(self):
        gu.Pricer.__init__.__defaults__ = (None,)
        if self.old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.old
        shutil.rmtree(self.dir)

    def write(self, name, rows):
        path = os.path.join(self.project, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        return path

    def session(self, sid, servers_used=(), minutes_ago=30):
        rows = [
            {"type": "attachment", "sessionId": sid, "attachment": {
                "type": "deferred_tools_delta", "addedNames": ["mcp__crm__search", "Read"],
                "addedLines": ["mcp__crm__search" + "x" * 400, "Read"]}},
            {"type": "attachment", "sessionId": sid, "attachment": {
                "type": "skill_listing", "names": ["deck:make", "notes"],
                "content": "- deck:make: " + "d" * 4000 + "\n- notes: short"}},
            {"type": "user", "sessionId": sid, "cwd": "/tmp/proj", "message": {"content": "p" * 400}},
            assistant(sid, "m1", usage(inp=10, read=40_000, w1=30_000, out=50), minutes_ago=minutes_ago,
                      tools=[("mcp__crm__search", {})] if "crm" in servers_used else []),
            assistant(sid, "m1", usage(inp=10, read=40_000, w1=30_000, out=200), minutes_ago=minutes_ago),
            assistant(sid, "m2", usage(inp=5, read=70_100, out=100), minutes_ago=minutes_ago - 1),
        ]
        return rows

    def test_dedupes_streamed_blocks_and_keeps_final_usage(self):
        self.write("s1.jsonl", self.session("s1"))
        requests, _ = gu.read_logs(gu.window(1), self.pricer)
        self.assertEqual(len(requests), 2)
        first = next(r for r in requests if r["tokens"]["write_1h"])
        self.assertEqual(first["tokens"]["output"], 200)

    def test_subagents_cost_but_do_not_set_starting_context(self):
        self.write("s1.jsonl", self.session("s1"))
        self.write("s1/subagents/agent-a.jsonl", [assistant("s1", "sub1", usage(inp=999_999, out=10),
                                                            model="claude-haiku-5-5", isSidechain=True)])
        out = io.StringIO()
        gu.audit(1, out)
        text = out.getvalue()
        self.assertIn("1 sessions · 3 requests", text)
        self.assertIn("| Median | 69.9k |", text)  # 70,010 prompt tokens − 400 chars / 4

    def test_session_started_before_window_has_no_starting_context(self):
        # Began 2 days ago; its in-window request carries 300k of history and must not count as a start.
        self.write("old.jsonl", [assistant("old", "o1", usage(read=50_000, w5=20_000), minutes_ago=2 * 24 * 60),
                                 assistant("old", "o2", usage(read=300_000), minutes_ago=10)])
        self.write("s1.jsonl", self.session("s1"))
        out = io.StringIO()
        gu.audit(1, out)
        text = out.getvalue()
        self.assertIn("2 sessions · 3 requests", text)
        self.assertIn("| Median | 69.9k |", text)
        self.assertIn("across 1 sessions started in this period", text)

    def test_loaded_vs_used_and_actions(self):
        for i in range(6):
            self.write(f"s{i}.jsonl", self.session(f"s{i}", servers_used=("crm",) if i == 0 else ()))
        out = io.StringIO()
        gu.audit(1, out)
        text = out.getvalue()
        self.assertIn("| crm | MCP server |", text)
        self.assertIn("6 sessions | 1 (17%)", text)
        self.assertIn("Disable the `deck` plugin", text)  # 1k-token plugin skill, never used
        self.assertNotIn("`crm` MCP server outside", text)  # used in 17% of sessions

    def test_typed_slash_command_counts_as_use(self):
        rows = self.session("s0")
        rows.insert(3, {"type": "user", "sessionId": "s0", "message": {
            "content": "<command-name>/deck:make</command-name>"}})
        self.write("s0.jsonl", rows)
        _, sessions = gu.read_logs(gu.window(1), self.pricer)
        self.assertIn("deck:make", sessions["s0"].used_skills)

    def test_report_and_csv(self):
        self.write("s1.jsonl", self.session("s1"))
        out = io.StringIO()
        gu.report(1, out=out)
        self.assertIn("| Sonnet 5.5 |", out.getvalue())
        self.assertIn("| proj |", out.getvalue())
        out = io.StringIO()
        gu.report(1, as_csv=True, out=out)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("date,project,model"))
        self.assertEqual(lines[1].split(",")[3], "2")

    def test_today_cost_is_incremental(self):
        cache = os.path.join(self.dir, "cache", "today.json")
        path = self.write("s1.jsonl", [assistant("s1", "m1", usage(inp=1_000_000), minutes_ago=0)])
        self.assertAlmostEqual(gu.today_cost(cache, self.pricer), 4)
        self.write("s1.jsonl", [assistant("s1", "m1", usage(inp=1_000_000, out=1_000_000), minutes_ago=0),
                                assistant("s1", "m2", usage(inp=500_000), minutes_ago=0)])
        self.assertAlmostEqual(gu.today_cost(cache, self.pricer), 24 + 2)
        with open(path, "a") as fh:
            fh.write('{"type": "assistant", "partial')  # a line still being written is skipped
        self.assertAlmostEqual(gu.today_cost(cache, self.pricer), 26)


class StatusLineTest(unittest.TestCase):
    def setUp(self):
        os.environ["NO_COLOR"] = "1"

    def test_subscription_line(self):
        data = {"model": {"display_name": "Opus 5.5"}, "cost": {"total_cost_usd": 1.8432},
                "context_window": {"used_percentage": 71.2},
                "rate_limits": {"five_hour": {"used_percentage": 20}, "seven_day": {"used_percentage": 30}},
                "workspace": {"project_dir": "/x/metering"}}
        self.assertEqual(sl.build(data, 6.2), "Opus 5.5 · ≈$1.84 · ctx 71% · 5h 20% · today ≈$6.20 · metering")

    def test_api_line_and_missing_fields(self):
        self.assertEqual(sl.build({"model": {"id": "claude-sonnet-5-5"}, "cost": {"total_cost_usd": 0.5}}),
                         "claude-sonnet-5-5 · $0.50")
        self.assertEqual(sl.build({}), "")


if __name__ == "__main__":
    unittest.main()
