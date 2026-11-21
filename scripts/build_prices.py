#!/usr/bin/env python3
"""Build data/prices.json for the plugin from llm-prices/data/prices.csv (Anthropic rows only).

Usage: python3 -I scripts/build_prices.py PATH/TO/llm-prices/data/prices.csv [OUT]
"""
import csv
import json
import os
import sys
from datetime import date


def num(value):
    return float(value) if value not in (None, "") else None


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                             "..", "data", "prices.json")
    models = {}
    with open(src, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["provider"] != "anthropic":
                continue
            models[row["model"]] = {key: num(row[key]) for key in (
                "input", "cached_input", "output", "cache_write_5m",
                "long_prompt_over", "long_prompt_input", "long_prompt_output")}
    data = {
        "source": "https://github.com/ProdMoh/llm-prices",
        "as_of": date.today().isoformat(),
        "unit": "USD per million tokens",
        # Multipliers on every rate in fast mode, for models that publish a fast price.
        "fast_multiplier": {"claude-opus-5-5": 2.0},
        "models": dict(sorted(models.items())),
    }
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")
    print(f"{len(models)} Anthropic models -> {os.path.normpath(out)}")


if __name__ == "__main__":
    main()
