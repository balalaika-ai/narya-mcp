"""Mine narya-mcp's call logs: where agents fail, what they never use, and whether search finds what they use.

    uv run python scripts/mine_telemetry.py [LOG_DIR ...] [--pairs pairs.jsonl] [--since 2026-10-01]

Reports, per tool, calls / ok rate / latency percentiles / failure reasons; the worst (tool, reason) cells; tools
never called; and search linkage: for each narya_search, whether a declaration it returned was later used by the
same server process (in narya_solve / narya_try / narya_exec / narya_check text), at which rank, and which used
constants no search had returned.  --pairs writes (query, scope, used declarations) records: a retrieval benchmark
made of real agent queries.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from narya_mcp import reasons  # noqa: E402
from narya_mcp.search import identifiers  # noqa: E402

USE_TOOLS = {"narya_solve": ("term",), "narya_try": ("terms",), "narya_exec": ("commands",), "narya_check": ("source",)}
LINK_WINDOW = 30 * 60  # seconds after a search during which a use counts


def load(dirs: list[Path], since: float) -> list[dict]:
    out = []
    for d in dirs:
        for f in sorted(d.glob("calls-*.jsonl")):
            for line in f.open(encoding="utf-8"):
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ts", 0) >= since:
                    out.append(e)
    out.sort(key=lambda e: (e["pid"], e["ts"]))
    return out


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def used_text(e: dict) -> str:
    args = e.get("args", {})
    parts = []
    for k in USE_TOOLS.get(e["tool"], ()):
        v = args.get(k)
        parts.extend(v if isinstance(v, list) else [v or ""])
    return " ".join(map(str, parts))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*", type=Path)
    ap.add_argument("--pairs", type=Path)
    ap.add_argument("--since", help="YYYY-MM-DD")
    ap.add_argument("--config", type=Path, help="narya-mcp.toml: count only real declarations as search misses")
    args = ap.parse_args()
    dirs = args.dirs or [Path(os.path.expanduser("~/.local/state/narya-mcp"))]
    since = time.mktime(time.strptime(args.since, "%Y-%m-%d")) if args.since else 0
    calls = load(dirs, since)
    if not calls:
        print("no calls logged")
        return
    print(f"{len(calls)} calls from {len({e['pid'] for e in calls})} server processes\n")

    by_tool: dict[str, list[dict]] = defaultdict(list)
    for e in calls:
        by_tool[e["tool"]].append(e)
    print(f"{'tool':18s} {'calls':>6s} {'ok%':>6s} {'p50 s':>7s} {'p90 s':>7s}  failure reasons")
    cells: Counter = Counter()
    for tool, es in sorted(by_tool.items(), key=lambda kv: -len(kv[1])):
        ok = sum(e["ok"] for e in es)
        ms = [e["ms"] / 1000 for e in es]
        rs = Counter(e["reason"] for e in es if not e["ok"])
        for r, n in rs.items():
            cells[(tool, r)] += n
        reasons_s = ", ".join(f"{r}:{n}" for r, n in rs.most_common(4))
        print(f"{tool:18s} {len(es):6d} {100 * ok / len(es):6.1f} {pct(ms, .5):7.2f} {pct(ms, .9):7.2f}  {reasons_s}")

    infra = [(c, n) for c, n in cells.most_common() if c[1] in reasons.INFRASTRUCTURE or c[1] == "validation"]
    print("\nworst cells (tool, reason): " + ", ".join(f"{t}/{r}:{n}" for (t, r), n in cells.most_common(8)))
    if infra:
        print("infrastructure/validation failures (fix the server or the tool description): "
              + ", ".join(f"{t}/{r}:{n}" for (t, r), n in infra[:8]))

    from narya_mcp import server  # registered tools

    registered = {t.name for t in __import__("asyncio").run(server.mcp.list_tools())}
    unused = sorted(registered - set(by_tool))
    if unused:
        print(f"never called: {', '.join(unused)}")

    # Search linkage.
    searches = [e for e in calls if e["tool"] == "narya_search" and e["ok"]]
    uses = [e for e in calls if e["tool"] in USE_TOOLS and e["ok"]]
    linked = 0
    ranks: list[int] = []
    pairs = []
    returned_by_pid: dict[int, set] = defaultdict(set)
    for s in searches:
        names = s["summary"].get("results", [])
        returned_by_pid[s["pid"]].update(names)
        later = [u for u in uses if u["pid"] == s["pid"] and 0 <= u["ts"] - s["ts"] <= LINK_WINDOW]
        used_ids = set().union(*(identifiers(used_text(u)) for u in later)) if later else set()
        hit = [(i + 1, n) for i, n in enumerate(names) if n in used_ids]
        if hit:
            linked += 1
            ranks.append(hit[0][0])
            pairs.append(
                {
                    "query": s["args"].get("query", ""),
                    "scope_file": s["args"].get("file"),
                    "session_id": s.get("session_id"),
                    "hole": s["args"].get("hole"),
                    "used": [n for _, n in hit],
                    "ranks": [r for r, _ in hit],
                }
            )
    if searches:
        print(f"\nsearch: {len(searches)} calls; a returned declaration was later used after {linked} "
              f"({100 * linked / len(searches):.0f}%)"
              + (f", first used rank median {statistics.median(ranks):.0f}" if ranks else ""))
    # Constants used in successful solves that no earlier search of the same process returned.
    declared = None
    if args.config:
        from narya_mcp import search
        from narya_mcp.config import load_config

        proj = load_config(args.config.expanduser().resolve())
        declared = set(search.get_index(proj.root, proj.search_paths, proj.search_annotations).by_name)
    misses: Counter = Counter()
    for u in uses:
        if u["tool"] == "narya_solve":
            for name in identifiers(used_text(u)):
                if (name in declared if declared is not None else name[:1].isalpha()) and name not in returned_by_pid[
                    u["pid"]
                ]:
                    misses[name] += 1
    if misses:
        print("most used in solves without having been found by search: "
              + ", ".join(f"{n}:{c}" for n, c in misses.most_common(12)))
    if args.pairs and pairs:
        with args.pairs.open("w", encoding="utf-8") as f:
            for p in pairs:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
        print(f"wrote {len(pairs)} query/used pairs to {args.pairs}")


if __name__ == "__main__":
    main()
