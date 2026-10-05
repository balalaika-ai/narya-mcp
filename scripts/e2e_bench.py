"""End-to-end benchmark: can an agent with narya-mcp fill masked holes in a real Narya project?

Tasks are definitions of the project whose body is replaced by `?`, with the rest of their module dropped.  For each
task and arm an isolated copy of the sources is made and a headless Claude Code agent (--restricted: no shell, file
tools confined to the copy) is asked to fill the hole using the narya MCP server, with a budget.  The result is
verified independently: the file must typecheck clean (no holes, no axioms), the statement and everything before it
must be unchanged (helper definitions may be inserted before the target).

Assumptions: the sources live in `src/` of the project, and Narya runs through launchers/ssh-workspace.sh (the
configuration's [env] gives NMCP_HOST, NMCP_REMOTE_BASE and NMCP_REMOTE_NARYA); `prepare` builds the remote base.

Arms differ only in the narya tools offered (NARYA_MCP_TOOLS):
    full   every tool
    batch  narya_check_file only (what agents had with a plain check script)

    uv run python scripts/e2e_bench.py make-tasks CONFIG.toml --n 40 --out tasks.json
    uv run python scripts/e2e_bench.py prepare tasks.json --config CONFIG.toml --run-dir ~/nmcp-bench/r1
    uv run python scripts/e2e_bench.py run tasks.json --config CONFIG.toml --arms full,batch --run-dir ~/nmcp-bench/r1
    uv run python scripts/e2e_bench.py report ~/nmcp-bench/r1
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
import tomllib
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from narya_mcp import batch, reasons, search  # noqa: E402
from narya_mcp.config import load_config  # noqa: E402
from narya_mcp.splitter import split_commands  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ARMS = {
    "full": "",
    "batch": "narya_check_file",
}
PROMPT = """You are working in a Narya project (Narya is a proof assistant for higher observational type theory).
The file {file} ends with the definition `{name}`, whose body is a hole `?`.

Task: replace the hole with a complete term so that the file typechecks with no holes and no axioms.
Rules: do not change the statement of `{name}` or anything before it in the file; you may add helper
definitions (`def`) just before it. Do not use `axiom`.

Use the narya MCP tools{tools_hint}. You are done when narya_check_file reports the file ok and clean;
then stop and reply DONE (or GIVE UP if you cannot do it)."""
MODULE_PROMPT = """You are working in a Narya project (Narya is a proof assistant for higher observational type theory).
In the file {file}, the bodies of {n} definitions have been replaced by holes `?`: {names}.

Task: fill every hole with a complete term so that the whole file typechecks with no holes and no axioms.
Rules: do not change any statement or any other part of the file; you may add helper definitions (`def`) before
the definitions that use them. Do not use `axiom`.

Use the narya MCP tools{tools_hint}. You are done when narya_check_file reports the file ok and clean;
then stop and reply DONE (or GIVE UP if you cannot do it)."""
TOOLS_HINT = {
    "full": " (interactive sessions with holes, search, split/try/solve, batch checks)",
    "batch": " (narya_check_file typechecks a file)",
}


# ------------------------------------------------------------------------------------------------ tasks


def split_of(file: str) -> str:
    return "dev" if int(hashlib.sha1(Path(file).name.encode()).hexdigest(), 16) % 2 == 0 else "test"


def maskable_defs(text: str) -> list[str]:
    """Definitions of a module whose bodies can be hidden: proofs and constructions that no statement in the module
    mentions (hiding a body that a statement depends on would change what the statement means)."""
    cmds = [c for c in split_commands(text) if c.keyword == "def" and c.name]
    if any("\nand " in c.text for c in cmds):
        return []
    names = {c.name for c in cmds}
    in_statements: set[str] = set()
    for c in cmds:
        in_statements |= (search.identifiers(search._split_top_coloneq(c.text[3:])[0]) - {c.name}) & names
    out = []
    for c in cmds:
        sig, body = search._split_top_coloneq(c.text[3:])
        b = body.strip()
        if not b or b.startswith(("data", "codata", "sig")) or c.name in in_statements or re.search(r"[?¿]", body):
            continue
        out.append(c.name)
    return out


def make_module_tasks(args, proj) -> None:
    cands = []
    for f in sorted((proj.root / "src").glob("*.ny")):
        m = re.match(r"(\d+)-", f.name)
        if (args.min_prefix and (not m or int(m.group(1)) < args.min_prefix)) or split_of(str(f)) != args.split:
            continue
        text = f.read_text(encoding="utf-8")
        masked = maskable_defs(text)
        if args.min_holes <= len(masked) <= args.max_holes and len(text) <= args.max_module_chars:
            cands.append((f, masked))
    random.Random(args.seed).shuffle(cands)
    picked = cands[: args.n]
    tasks = []
    for f, masked in picked:
        rel = os.path.relpath(f, proj.root)
        tasks.append({"id": hashlib.sha1(rel.encode()).hexdigest()[:8], "mode": "module", "file": rel,
                      "name": masked[-1], "masked": masked})
    Path(args.out).write_text(json.dumps(tasks, ensure_ascii=False, indent=1))
    print(f"{len(cands)} candidate modules, wrote {len(tasks)} tasks to {args.out}")


def make_tasks(args) -> None:
    proj = load_config(Path(args.config).expanduser().resolve())
    if args.mode == "module":
        return make_module_tasks(args, proj)
    idx = search.Index(proj.root, proj.search_paths, proj.search_annotations)
    idx.refresh()
    cands = []
    for d in idx.decls:
        m = re.match(r"(\d+)-", Path(d.file).name)
        if (args.min_prefix and (not m or int(m.group(1)) < args.min_prefix)) or split_of(d.file) != args.split:
            continue
        if d.kind != "def" or not (args.min_body <= len(d.body) <= args.max_body):
            continue
        text = Path(d.file).read_text(encoding="utf-8")
        cmd = next((c for c in split_commands(text) if c.keyword == "def" and c.name == d.name), None)
        if cmd is None or "\nand " in cmd.text or re.search(r"[?¿]", search._split_top_coloneq(cmd.text)[1]):
            continue
        removed: list[str] = []
        if args.mode == "hard":
            removed = in_module_support(text, d.name)
            if not removed:
                continue
        cands.append((d, cmd, removed))
    random.Random(args.seed).shuffle(cands)
    # Stratify by body length so short and long proofs are both represented.
    cands.sort(key=lambda dc: len(dc[0].body) + 200 * len(dc[2]))
    k = max(1, len(cands) // args.n)
    picked = [cands[i] for i in range(0, len(cands), k)][: args.n]
    tasks = []
    for d, cmd, removed in picked:
        rel = os.path.relpath(d.file, proj.root)
        tasks.append(
            {
                "id": hashlib.sha1(f"{rel}:{d.name}".encode()).hexdigest()[:8],
                "file": rel,
                "name": d.name,
                "line": cmd.line,
                "signature": search._split_top_coloneq(cmd.text)[0].strip(),
                "body": search._split_top_coloneq(cmd.text)[1].strip(),
                "body_chars": len(d.body),
                "removed": removed,
            }
        )
    Path(args.out).write_text(json.dumps(tasks, ensure_ascii=False, indent=1))
    print(f"{len(cands)} candidates, wrote {len(tasks)} tasks to {args.out}")


def in_module_support(text: str, name: str) -> list[str]:
    """Definitions of the module before `name` that its proof depends on (transitively), and every earlier
    definition depending on those, so that the remaining prefix still typechecks.  Nothing the statement of `name`
    depends on is removed: otherwise an agent could redefine it and change the meaning of the statement."""
    cmds = [c for c in split_commands(text) if c.keyword in ("def", "axiom") and c.name]
    pos = next(i for i, c in enumerate(cmds) if c.name == name)
    earlier = cmds[:pos]
    names = {c.name for c in earlier}
    uses = {c.name: (search.identifiers(c.text[len(c.keyword) :]) - {c.name}) & names for c in cmds[: pos + 1]}

    def closure(start: set[str]) -> set[str]:
        seen, todo = set(), list(start)
        while todo:
            for u in uses.get(todo.pop(), set()):
                if u not in seen:
                    seen.add(u)
                    todo.append(u)
        return seen | start

    target = cmds[pos]
    sig = search._split_top_coloneq(target.text[len(target.keyword) :])[0]
    protected = closure(search.identifiers(sig) & names)
    body_deps = closure(uses[name]) - {name}
    removed = body_deps - protected
    while True:
        dependents = {c.name for c in earlier if c.name not in removed and uses[c.name] & removed}
        blocked = dependents & protected
        if blocked:
            # Keep what a protected definition needs.
            removed -= closure(blocked)
            continue
        if not dependents:
            break
        removed |= dependents
    return [c.name for c in earlier if c.name in removed]


def mask_module(text: str, task: dict) -> str:
    masked = set(task["masked"])
    out, pos = [], 0
    for c in split_commands(text):
        if c.keyword == "def" and c.name in masked:
            sig = search._split_top_coloneq(c.text)[0].rstrip()
            out.append(text[pos : c.start] + sig + "\n  ≔ ?")
            pos = c.end
    out.append(text[pos:])
    return "".join(out)


def mask(text: str, task: dict) -> str:
    if task.get("mode") == "module":
        return mask_module(text, task)
    cmds = split_commands(text)
    cmd = next(c for c in cmds if c.keyword == "def" and c.name == task["name"])
    drop = set(task.get("removed", []))
    out, pos = [], 0
    for c in cmds:
        if c.end > cmd.start:
            break
        if c.name in drop and c.keyword in ("def", "axiom"):
            out.append(text[pos : c.start])
            pos = c.end
    out.append(text[pos : cmd.start])
    sig = search._split_top_coloneq(cmd.text)[0].rstrip()
    return "".join(out) + sig + "\n  ≔ ?\n"


def norm(s: str) -> list[str]:
    """Tokens of a statement: whitespace differences (def f(x) vs def f (x)) do not count."""
    return re.findall(r"[^\s()\[\]{},:]+|[()\[\]{},:]", s)


def module_structure_ok(original: str, new: str, task: dict) -> tuple[bool, str]:
    masked = set(task["masked"])
    orig = [c for c in split_commands(original)]
    new_cmds = split_commands(new)
    by_name = {c.name: c for c in new_cmds if c.keyword == "def" and c.name}
    it = iter(new_cmds)
    for c in orig:
        if c.keyword == "def" and c.name in masked:
            t = by_name.get(c.name)
            if t is None:
                return False, f"{c.name} missing"
            if norm(search._split_top_coloneq(t.text)[0]) != norm(search._split_top_coloneq(c.text)[0]):
                return False, f"statement of {c.name} changed"
            want = t.text
        else:
            want = c.text
        # Every original command (or its filled version) must appear, in order; extra commands must be defs.
        for x in it:
            if x.text == want:
                break
            if x.keyword != "def" or (x.name in {o.name for o in orig if o.keyword == "def"}):
                return False, f"unexpected change near {c.name or c.keyword}"
        else:
            return False, f"{c.name or c.keyword} missing or reordered"
    if any(c.keyword == "axiom" for c in new_cmds if c.text not in {o.text for o in orig}):
        return False, "axiom added"
    return True, ""


def structure_ok(original: str, new: str, task: dict) -> tuple[bool, str]:
    if task.get("mode") == "module":
        return module_structure_ok(original, new, task)
    orig_cmds = split_commands(original)
    tgt = next(c for c in orig_cmds if c.keyword == "def" and c.name == task["name"])
    drop = set(task.get("removed", []))
    before = [c.text for c in orig_cmds if c.end <= tgt.start and not (c.name in drop and c.keyword in ("def", "axiom"))]
    new_cmds = split_commands(new)
    t = next((c for c in new_cmds if c.keyword == "def" and c.name == task["name"]), None)
    if t is None:
        return False, "target definition missing"
    if norm(search._split_top_coloneq(t.text)[0]) != norm(task["signature"]):
        return False, "statement changed"
    prefix = [c for c in new_cmds if c.end <= t.start]
    it = iter(c.text for c in prefix)
    if not all(any(x == b for x in it) for b in before):
        return False, "text before the target changed"
    extra = [c for c in prefix if c.text not in before]
    if any(c.keyword != "def" for c in extra):
        return False, "non-def command added before the target"
    if any(c.keyword == "axiom" for c in new_cmds):
        return False, "axiom added"
    return True, ""


# ------------------------------------------------------------------------------------------------ prepare


def remote_cfg(cfg_path: Path) -> dict:
    return tomllib.loads(cfg_path.read_text()).get("env", {})


def prepare(args) -> None:
    """Snapshot the sources (keeping modification times: Narya trusts a .nyo not older than its sources) and build,
    on the remote host, a base workspace in which every import of every task module is compiled from source, so
    that a task only recompiles its own masked module."""
    run_dir = Path(args.run_dir).expanduser()
    cfg = Path(args.config).expanduser().resolve()
    proj = load_config(cfg)
    env = remote_cfg(cfg)
    host, base, narya = env["NMCP_HOST"], env["NMCP_REMOTE_BASE"], env["NMCP_REMOTE_NARYA"]
    snap = run_dir / "_snapshot"
    (snap / "src").mkdir(parents=True, exist_ok=True)
    subprocess.run(["rsync", "-a", "--delete", "--exclude", "*.nyo", f"{proj.root}/src/", f"{snap}/src/"], check=True)
    for ann in proj.search_annotations:
        if ann.is_file():
            shutil.copy2(ann, snap / ann.name)
    tasks = json.loads(Path(args.tasks).read_text())
    imports = set()
    for t in tasks:
        text = (snap / t["file"]).read_text(encoding="utf-8")
        for c in split_commands(text):
            if c.keyword in ("import", "export") and (m := re.match(r'\s*(?:import|export)\s+"([^"]+)"', c.text)):
                target = os.path.normpath(os.path.join(os.path.dirname(t["file"]), m.group(1)))
                imports.add(os.path.relpath(target, "src"))
    digest = hashlib.sha1(
        "".join(sorted(f"{p}:{p.stat().st_mtime}" for p in (snap / "src").glob("*.ny"))).encode()
        + "".join(sorted(imports)).encode()
    ).hexdigest()[:8]
    bench_base = f"~/nmcp-bench-base-{digest}"
    (run_dir / "_meta").mkdir(exist_ok=True)
    (run_dir / "_meta" / "base.json").write_text(json.dumps({"base": bench_base, "imports": sorted(imports)}))
    lines = "".join(f'import "{i}"\n' for i in sorted(imports))
    print(f"{len(imports)} distinct imports; building {bench_base} on {host} (compiling from source)...", flush=True)
    script = f"""
set -e
b={bench_base}
if [ -e $b/.frozen-stamp ] && [ -e $b/.bench-complete ]; then echo "base exists"; exit 0; fi
rm -rf $b; cp -a {base} $b
"""
    subprocess.run(["ssh", host, script], check=True)
    subprocess.run(
        ["rsync", "-a", "--exclude", "*.nyo", f"{snap}/src/", f"{host}:{bench_base.replace('~/', '')}/src/"], check=True
    )
    build = f"""
set -e
b={bench_base}; cd $b/src
[ -e ../.bench-complete ] && exit 0
cat > nmcp-bench-imports.ny <<'NY'
{lines}NY
start=$(date +%s)
/usr/bin/time -f "maxrss_kb=%M" nice -n 10 {narya} -source-only -no-reformat nmcp-bench-imports.ny 2>&1 | tail -5
rm -f nmcp-bench-imports.ny nmcp-bench-imports.nyo
touch ../.frozen-stamp ../.bench-complete
echo "built in $(( $(date +%s) - start ))s; $(ls *.nyo | wc -l) compiled modules; $(du -sh .. | cut -f1)"
"""
    subprocess.run(["ssh", "-o", "ServerAliveInterval=30", host, build], check=True)


# ------------------------------------------------------------------------------------------------ running


def bench_config(src_cfg: Path, root: Path, out: Path, bench_base: str) -> Path:
    """The project's configuration with the root moved to the task copy, the remote launcher forced (the local
    mirror is shared with the real project and must not receive masked sources) and the prepared base."""
    data = tomllib.loads(src_cfg.read_text())
    env = dict(data.get("env", {}))
    env["NMCP_REMOTE_BASE"] = bench_base
    env["NMCP_FROZEN_STAMP"] = ".frozen-stamp"
    env["NMCP_SYNC"] = "src"
    aliases = [*data.get("path_aliases", []), "/home/ubuntu/" + bench_base.removeprefix("~/")]
    lines = [
        f'root = "{root}"',
        f'launcher = ["{REPO / "launchers" / "ssh-workspace.sh"}"]',
        f"path_aliases = {json.dumps(aliases)}",
        f"quiet_codes = {json.dumps(data.get('quiet_codes', []))}",
        "max_sessions = 3",
        f"search_paths = {json.dumps(data.get('search_paths', []))}",
        f"search_annotations = {json.dumps(data.get('search_annotations', []))}",
        "[env]",
        *[f"{k} = {json.dumps(v)}" for k, v in env.items()],
    ]
    out.write_text("\n".join(lines) + "\n")
    return out


async def run_one(task: dict, arm: str, args, proj, sem: asyncio.Semaphore) -> dict:
    run_dir = Path(args.run_dir).expanduser()
    tag = f"{task['id']}-{arm}"
    work = run_dir / tag
    meta = run_dir / "_meta" / tag
    result_file = meta / "result.json"
    if result_file.exists() and not args.force:
        return json.loads(result_file.read_text())
    async with sem:
        if work.exists():
            shutil.rmtree(work)
        meta.mkdir(parents=True, exist_ok=True)
        (work / "src").mkdir(parents=True)
        snap = run_dir / "_snapshot"
        subprocess.run(["rsync", "-a", "--exclude", "*.nyo", f"{snap}/src/", f"{work}/src/"], check=True)
        for ann in proj.search_annotations:
            if (snap / ann.name).is_file():
                shutil.copy2(snap / ann.name, work / ann.name)
        target = work / task["file"]
        original = (snap / task["file"]).read_text(encoding="utf-8")
        target.write_text(mask(original, task), encoding="utf-8")
        bench_base = json.loads((run_dir / "_meta" / "base.json").read_text())["base"]
        cfg = bench_config(Path(args.config).expanduser().resolve(), work, meta / "narya-mcp.toml", bench_base)
        mcp = {
            "mcpServers": {
                "narya": {
                    "type": "stdio",
                    "command": str(Path("~/.local/bin/narya-mcp").expanduser()),
                    "args": [],
                    "env": {
                        "NARYA_MCP_CONFIG": str(cfg),
                        "NARYA_MCP_LOG_DIR": str(meta / "logs"),
                        "NARYA_MCP_TOOLS": ARMS[arm],
                        "NARYA_MCP_JAIL": str(work),
                    },
                }
            }
        }
        (meta / "mcp.json").write_text(json.dumps(mcp, indent=1))
        settings = {"permissions": {"allow": ["mcp__narya", "Read", "Edit", "Write", "Grep", "Glob"]}}
        (meta / "settings.json").write_text(json.dumps(settings))
        if task.get("mode") == "module":
            prompt = MODULE_PROMPT.format(
                file=task["file"], n=len(task["masked"]), names=", ".join(f"`{n}`" for n in task["masked"]),
                tools_hint=TOOLS_HINT[arm],
            )
        else:
            prompt = PROMPT.format(file=task["file"], name=task["name"], tools_hint=TOOLS_HINT[arm])
        argv = [
            "claude", "-p", prompt,
            "--model", args.model,
            "--restricted",
            "--tools", "Read,Edit,Write,Grep,Glob",
            "--strict-mcp-config", "--mcp-config", str(meta / "mcp.json"),
            "--settings", str(meta / "settings.json"),
            "--permission-mode", "acceptEdits",
            "--output-format", "stream-json", "--verbose",
            "--max-budget-usd", str(args.budget),
            "--no-session-persistence",
        ]  # fmt: skip
        t0 = time.time()
        with (meta / "transcript.jsonl").open("wb") as out:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=work, stdout=out, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=args.wall)
                timed_out = False
            except TimeoutError:
                proc.kill()
                await proc.wait()
                err, timed_out = b"", True
        wall = time.time() - t0
        res = summarize_transcript(meta / "transcript.jsonl")
        new = target.read_text(encoding="utf-8")
        struct, why = structure_ok(original, new, task)
        check = await batch.check_file(load_config(cfg), target, False, 1800)
        solved = struct and check.get("clean", False)
        result = {
            "task": task["id"],
            "name": task["name"],
            "arm": arm,
            "solved": solved,
            "structure_ok": struct,
            "structure_problem": why or None,
            "check_ok": check.get("ok"),
            "check_clean": check.get("clean"),
            "check_reason": check.get("reason") or (reasons.derive(check) if not check.get("ok") else None),
            "check_retried_source_only": bool(check.get("retried_source_only")),
            "wall_s": round(wall, 1),
            "agent_timed_out": timed_out,
            "stderr_tail": err.decode(errors="replace")[-500:] if err else "",
            **res,
            "tools": tool_log_summary(meta / "logs"),
        }
        result_file.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        print(
            f"{tag:16s} {'SOLVED' if solved else 'failed':7s} {wall / 60:5.1f} min  "
            f"${result.get('cost_usd') or 0:.2f}  turns={result.get('turns')}  "
            f"calls={sum(result['tool_uses'].values())}  {why or check.get('reason') or ''}",
            flush=True,
        )
        return result


def summarize_transcript(path: Path) -> dict:
    uses: Counter = Counter()
    final: dict = {}
    for line in path.open(encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "assistant":
            for c in e.get("message", {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    uses[c.get("name", "?")] += 1
        elif e.get("type") == "result":
            final = e
    usage = final.get("usage") or {}
    return {
        "tool_uses": dict(uses),
        "turns": final.get("num_turns"),
        "cost_usd": final.get("total_cost_usd"),
        "agent_result": (final.get("result") or "")[-300:],
        "agent_subtype": final.get("subtype"),
        "output_tokens": usage.get("output_tokens"),
        "input_tokens": (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0),
    }


def tool_log_summary(logdir: Path) -> dict:
    calls: Counter = Counter()
    fails: Counter = Counter()
    for f in logdir.glob("calls-*.jsonl"):
        for line in f.open(encoding="utf-8"):
            e = json.loads(line)
            calls[e["tool"]] += 1
            if not e["ok"]:
                fails[f"{e['tool']}/{e['reason']}"] += 1
    return {"calls": dict(calls), "failures": dict(fails)}


async def run(args) -> None:
    tasks = json.loads(Path(args.tasks).read_text())
    if args.spread:
        step = len(tasks) / args.spread
        tasks = [tasks[int(i * step + step / 2)] for i in range(args.spread)]
    if args.limit:
        tasks = tasks[: args.limit]
    proj = load_config(Path(args.config).expanduser().resolve())
    sem = asyncio.Semaphore(args.parallel)
    arms = args.arms.split(",")
    jobs = [run_one(t, a, args, proj, sem) for t in tasks for a in arms]
    await asyncio.gather(*jobs)
    report(argparse.Namespace(run_dir=args.run_dir))


# ------------------------------------------------------------------------------------------------ report


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def report(args) -> None:
    run_dir = Path(args.run_dir).expanduser()
    results = [json.loads(p.read_text()) for p in (run_dir / "_meta").glob("*/result.json")]
    if not results:
        print("no results")
        return
    by_arm: dict[str, list[dict]] = {}
    for r in results:
        by_arm.setdefault(r["arm"], []).append(r)
    print(f"\n{'arm':6s} {'n':>3s} {'solved':>7s} {'95% CI':>13s} {'$/task':>7s} {'turns':>6s} {'min':>5s} "
          f"{'narya calls':>11s}  failures")
    for arm, rs in sorted(by_arm.items()):
        k = sum(r["solved"] for r in rs)
        lo, hi = wilson(k, len(rs))
        med = lambda xs: statistics.median(xs) if xs else 0  # noqa: E731
        cost = med([r.get("cost_usd") or 0 for r in rs])
        turns = med([r.get("turns") or 0 for r in rs])
        mins = med([r["wall_s"] / 60 for r in rs])
        calls = med([sum(r["tools"]["calls"].values()) for r in rs])
        fails = Counter()
        for r in rs:
            fails.update(r["tools"]["failures"])
        print(f"{arm:6s} {len(rs):3d} {k:3d}/{len(rs):<3d} [{lo:.2f}, {hi:.2f}] {cost:7.2f} {turns:6.0f} {mins:5.1f} "
              f"{calls:11.0f}  " + ", ".join(f"{f}:{n}" for f, n in fails.most_common(5)))
    arms = sorted(by_arm)
    if len(arms) == 2:
        a, b = arms
        sa = {r["task"]: r["solved"] for r in by_arm[a]}
        sb = {r["task"]: r["solved"] for r in by_arm[b]}
        both = set(sa) & set(sb)
        only_a = sum(sa[t] and not sb[t] for t in both)
        only_b = sum(sb[t] and not sa[t] for t in both)
        print(f"\npaired over {len(both)} tasks: solved only by {a}: {only_a}, only by {b}: {only_b}")
    usage: dict[str, Counter] = {}
    for r in results:
        usage.setdefault(r["arm"], Counter()).update(r["tools"]["calls"])
    for arm, c in sorted(usage.items()):
        print(f"{arm} tool calls: " + ", ".join(f"{t.removeprefix('narya_')}:{n}" for t, n in c.most_common()))
    unsolved = [r for r in results if not r["solved"]]
    if unsolved:
        why = Counter(r.get("structure_problem") or r.get("check_reason") or r.get("agent_subtype") for r in unsolved)
        print("unsolved because: " + ", ".join(f"{w}:{n}" for w, n in why.most_common()))


def reverify(args) -> None:
    """Recompute the structure verdict of finished runs (after fixing the checker); the recorded check stands."""
    run_dir = Path(args.run_dir).expanduser()
    tasks = {t["id"]: t for t in json.loads(Path(args.tasks).read_text())}
    changed = 0
    for rf in (run_dir / "_meta").glob("*/result.json"):
        r = json.loads(rf.read_text())
        t = tasks.get(r["task"])
        if t is None:
            continue
        orig = (run_dir / "_snapshot" / t["file"]).read_text(encoding="utf-8")
        new = (run_dir / f"{t['id']}-{r['arm']}" / t["file"]).read_text(encoding="utf-8")
        ok, why = structure_ok(orig, new, t)
        solved = ok and bool(r.get("check_clean"))
        if solved != r["solved"] or ok != r["structure_ok"]:
            changed += 1
            print(f"{t['id']}-{r['arm']}: solved {r['solved']} -> {solved} ({why or 'structure ok'})")
        r.update(structure_ok=ok, structure_problem=why or None, solved=solved)
        rf.write_text(json.dumps(r, ensure_ascii=False, indent=1))
    print(f"{changed} verdicts changed")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make-tasks")
    m.add_argument("config")
    m.add_argument("--n", type=int, default=40)
    m.add_argument("--out", default="tasks.json")
    m.add_argument("--seed", type=int, default=0)
    m.add_argument("--split", default="test")
    m.add_argument("--min-prefix", type=int, default=0,
                   help="only files named with a numeric prefix >= this (e.g. 400-groups.ny); 0: all files")
    m.add_argument("--min-body", type=int, default=20)
    m.add_argument("--max-body", type=int, default=400)
    m.add_argument("--mode", choices=["easy", "hard", "module"], default="easy",
                   help="hard: also remove the definitions of the module the target depends on; "
                        "module: hide the bodies of all definitions of a module that no statement mentions")
    m.add_argument("--min-holes", type=int, default=3)
    m.add_argument("--max-holes", type=int, default=10)
    m.add_argument("--max-module-chars", type=int, default=12000)
    r = sub.add_parser("run")
    r.add_argument("tasks")
    r.add_argument("--config", required=True)
    r.add_argument("--arms", default="full,batch")
    r.add_argument("--run-dir", required=True)
    r.add_argument("--model", default="sonnet")
    r.add_argument("--budget", type=float, default=3.0, help="max USD per task (claude --max-budget-usd)")
    r.add_argument("--wall", type=float, default=2400, help="max seconds per task")
    r.add_argument("--parallel", type=int, default=4)
    r.add_argument("--limit", type=int)
    r.add_argument("--spread", type=int, help="run N tasks evenly spread over the list (sorted by proof length)")
    r.add_argument("--force", action="store_true")
    q = sub.add_parser("prepare")
    q.add_argument("tasks")
    q.add_argument("--config", required=True)
    q.add_argument("--run-dir", required=True)
    p = sub.add_parser("report")
    p.add_argument("run_dir")
    v = sub.add_parser("reverify")
    v.add_argument("tasks")
    v.add_argument("run_dir")
    args = ap.parse_args()
    if args.cmd == "make-tasks":
        make_tasks(args)
    elif args.cmd == "prepare":
        prepare(args)
    elif args.cmd == "run":
        asyncio.run(run(args))
    elif args.cmd == "reverify":
        reverify(args)
    else:
        report(args)


if __name__ == "__main__":
    main()
