"""Retrieval benchmark for narya_search (after rocqet-search's mine_eval and CSLibPremiseBench).

Tasks, built from the project itself (leakage-free):

- premises: for a declaration X, the query is what an agent proving X knows (its comment and signature); the targets
  are the non-trivial constants X's body uses, searched among the declarations visible before X (import closure,
  earlier in the file).  Metrics per task: recall@10, hit@10, MRR (first target), nDCG@10 (binary relevance).
- describe: the query is the comment above X, removed from X's own index entry; hit@1 and hit@10 of X.
- agent pairs (--pairs, from scripts/mine_telemetry.py): real agent queries and the declarations they then used.

Files are split into dev and test halves by a hash of their name (module-disjoint): tune on dev, report test.
Each variant is compared with the default by a paired bootstrap over tasks (95% interval of the mean difference).

    uv run python scripts/search_eval.py CONFIG.toml [--sample 1000] [--split test] [--pairs pairs.jsonl]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from narya_mcp import search  # noqa: E402
from narya_mcp.config import load_config  # noqa: E402

K = 10


def split_of(file: str) -> str:
    return "dev" if int(hashlib.sha1(Path(file).name.encode()).hexdigest(), 16) % 2 == 0 else "test"


def premises_targets(idx: search.Index, common: set[str], d: search.Decl) -> set[str]:
    used = search.identifiers(d.body) & idx.by_name.keys()
    return {u for u in used if u != d.name and u not in common}


def ranking_metrics(names: list[str], targets: set[str]) -> dict:
    hits = [i for i, n in enumerate(names[:K]) if n in targets]
    dcg = sum(1 / math.log2(i + 2) for i in hits)
    idcg = sum(1 / math.log2(i + 2) for i in range(min(len(targets), K)))
    return {
        "recall@10": len({names[i] for i in hits}) / len(targets),
        "hit@10": float(bool(hits)),
        "mrr": 1 / (hits[0] + 1) if hits else 0.0,
        "ndcg@10": dcg / idcg if idcg else 0.0,
    }


def run_tasks(idx: search.Index, tasks: dict) -> dict[str, list[float]]:
    """Per-task metric values (aligned across variants, for paired comparisons)."""
    out: dict[str, list[float]] = {}

    def add(k, v):
        out.setdefault(k, []).append(v)

    for i, targets in tasks["premises"]:
        d = idx.decls[i]
        hits = idx.search(f"{d.doc} {d.signature}", scope_file=d.file, scope_line=d.line, exclude={i}, limit=K)
        for k, v in ranking_metrics([h.name for _, h in hits], targets).items():
            add(f"premises {k}", v)
    for i in tasks["describe"]:
        d = idx.decls[i]
        saved = idx.tf[i], idx.lengths[i]
        doc_less = search.Decl(**{**d.__dict__, "doc": ""}).tokens()
        idx.tf[i], idx.lengths[i] = doc_less, sum(doc_less.values())
        ranked = [h for _, h in idx.search(d.doc, limit=K)]
        idx.tf[i], idx.lengths[i] = saved
        add("describe hit@1", float(bool(ranked) and ranked[0] is d))
        add("describe hit@10", float(any(h is d for h in ranked)))
    for p in tasks["pairs"]:
        hits = idx.search(p["query"], scope_file=p.get("scope_file"), limit=K)
        for k, v in ranking_metrics([h.name for _, h in hits], set(p["used"])).items():
            add(f"agent {k}", v)
    return out


def bootstrap(a: list[float], b: list[float], n: int = 5000, seed: int = 0) -> tuple[float, float, float]:
    """Mean of b - a and its 95% paired-bootstrap interval."""
    diffs = [y - x for x, y in zip(a, b)]
    rnd = random.Random(seed)
    m = len(diffs)
    means = sorted(sum(diffs[rnd.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return sum(diffs) / m, means[int(0.025 * n)], means[int(0.975 * n)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--sample", type=int, default=1000)
    ap.add_argument("--split", choices=["dev", "test", "all"], default="test")
    ap.add_argument("--min-prefix", type=int, default=0,
                    help="only files named with a numeric prefix >= this (e.g. 400-groups.ny); 0: all files")
    ap.add_argument("--pairs", type=Path, help="agent query/used pairs from scripts/mine_telemetry.py")
    ap.add_argument("--variants", default="all", help="'all' or 'default' (just measure the current settings)")
    args = ap.parse_args()
    proj = load_config(Path(args.config).expanduser().resolve())
    idx = search.Index(proj.root, proj.search_paths, proj.search_annotations)
    t0 = time.time()
    idx.refresh()
    usage: Counter = Counter()
    for d in idx.decls:
        usage.update(search.identifiers(d.body) & idx.by_name.keys())
    common = {name for name, _ in usage.most_common(50)}

    def prefix(d):
        m = re.match(r"(\d+)-", Path(d.file).name)
        return int(m.group(1)) if m else (0 if args.min_prefix == 0 else -1)

    pool = [
        i
        for i, d in enumerate(idx.decls)
        if prefix(d) >= args.min_prefix and (args.split == "all" or split_of(d.file) == args.split)
    ]
    random.Random(0).shuffle(pool)
    sample = pool[: args.sample]
    tasks = {
        "premises": [(i, t) for i in sample if (t := premises_targets(idx, common, idx.decls[i]))],
        "describe": [i for i in sample if len(idx.decls[i].doc.split()) >= 5],
        "pairs": [json.loads(line) for line in args.pairs.open()] if args.pairs else [],
    }
    print(
        f"{len(idx.decls)} declarations (indexed in {time.time() - t0:.1f}s); split {args.split}: "
        f"{len(tasks['premises'])} premise tasks, {len(tasks['describe'])} describe tasks, "
        f"{len(tasks['pairs'])} agent pairs"
    )

    base_w, base_mode, base_pw = dict(search.WEIGHTS), search.PREFIX_MODE, search.PREFIX_WEIGHT

    def variant(weights=None, mode=None, pw=None):
        search.WEIGHTS.clear()
        search.WEIGHTS.update(base_w | (weights or {}))
        search.PREFIX_WEIGHT = base_pw if pw is None else pw
        idx._rebuild()
        idx.prefix = base_mode if mode is None else mode
        return run_tasks(idx, tasks)

    default = variant()
    off = {"label": 0, "doc": 0, "note": 0, "minor_note": 0}
    variants = {"default": default}
    if args.variants == "all":
        variants |= {
            "names only": variant(off | {"signature": 0}, "off"),
            "names + signatures": variant(off, "off"),
            "+ comments": variant(off | {"doc": base_w["doc"]}, "off"),
            "+ annotations (no abbreviations)": variant(None, "off"),
            "+ body 0.3": variant({"body": 0.3}),
            "all prefix matches": variant(None, "all", 0.25),
        }
    metrics = list(default)
    print(f"\n{'variant':34s} " + " ".join(f"{m:>18s}" for m in metrics))
    for name, res in variants.items():
        cells = []
        for m in metrics:
            mean = sum(res[m]) / len(res[m])
            if name == "default":
                cells.append(f"{mean:18.3f}")
            else:
                d, lo, hi = bootstrap(default[m], res[m])
                star = "*" if lo > 0 or hi < 0 else " "
                cells.append(f"{mean:6.3f} {d:+.3f}{star}".rjust(18))
        print(f"{name:34s} " + " ".join(cells))
    print("\n(difference from default; * = 95% paired-bootstrap interval excludes 0)")


if __name__ == "__main__":
    main()
