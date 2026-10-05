# narya-mcp

An [MCP](https://modelcontextprotocol.io/) server for the [Narya](https://github.com/gwaithimirdain/narya) proof
assistant. It lets LLM agents typecheck Narya files and develop proofs interactively: load a file into a live Narya
process, inspect the holes (`?`) with their contexts and types, split holes, check candidate terms, solve holes,
search the project for lemmas, and replay the file after edits.

The interactive tools drive Narya's own ProofGeneral mode (`narya -proofgeneral`). Its protocol reports goals, hole
positions, error offsets and the current undo instant for every command, so no separate prover backend is needed.
The design follows [rocq-mcp](https://github.com/LLM4Rocq/rocq-mcp).

## Install

Requirements: Narya, Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/balalaika-ai/narya-mcp && cd narya-mcp
uv tool install --editable .        # installs the narya-mcp command
uv run pytest                       # unit tests; with NARYA_BIN=/path/to/narya also the end-to-end tests
```

Register the server with an MCP client. For Claude Code, a project `.mcp.json`:

```json
{
  "mcpServers": {
    "narya": {
      "type": "stdio",
      "command": "${HOME}/.local/bin/narya-mcp",
      "env": { "NARYA_BIN": "/path/to/narya" }
    }
  }
}
```

Without configuration the server runs `NARYA_BIN` (or `narya` on `PATH`) in the directory of the file. See
[Configuration](#configuration) for projects that need more.

## Tools

Positions are 1-based lines and columns; end columns are exclusive. Every response has `ok`; a failure also has
`reason`, from a fixed set (`src/narya_mcp/reasons.py`), and `error`.

| Tool | What it does |
|---|---|
| `narya_check_file` | Typecheck a file in a fresh Narya run: the authoritative check. Errors with file, line and column; warnings; assumed axioms; open holes with their contexts. `ok`: Narya accepted the file. `clean`: also no axioms and no holes. |
| `narya_check` | Typecheck a snippet. With `file`, in the context of that file (through a session, then undone). |
| `narya_audit` | Find `axiom` commands and holes in a file and its whole import closure, and the axioms Narya assumes. |
| `narya_toc` | Outline of a file: commands, names, line ranges, definitions with holes. |
| `narya_search` | Find declarations by words, identifiers or exact name. Narya has no search command; see [Search](#search). |
| `narya_lookup` | Full source of a declaration by name, with its location and comment. |
| `narya_start` | Start a session: a Narya process that loads the file command by command, up to `line`. Returns the errors and the open holes, with their positions, contexts and types. |
| `narya_sync` | After an edit, retract the session from the first changed command (Narya `undo`) and replay the rest. Unchanged commands and imports stay loaded. |
| `narya_holes` | Open holes of a session. |
| `narya_split` | Narya's proposed shape for a hole (abstraction, tuple, comatch, constructor, or match), not committed. |
| `narya_try` | Check up to 20 candidate terms against a hole, without solving it. |
| `narya_synth` | Type (or normal form) of a term, globally or in the context of a hole. |
| `narya_solve` | Solve a hole with a term, which may contain new holes. Returns the edit to apply to the file. |
| `narya_exec` | Run any Narya commands in a session. |
| `narya_close`, `narya_sessions` | Session management. |
| `narya_health` | Configuration in use, Narya version, live sessions. |

A typical loop: `narya_start(file)`; for each hole, `narya_search(session_id, hole)`, `narya_split`, `narya_try`
and `narya_synth`; `narya_solve` and apply the edit; `narya_sync`; at the end, `narya_check_file`.

Notes:

- `narya_solve` does not change the file. Apply the returned edit, and `narya_sync` replays the edited command.
- `narya_try` checks `((term) : TYPE)` with the printed type of the hole, so it fails if that type mentions other
  holes.
- A session command that runs longer than `edit_timeout` (120 s) is interrupted with SIGINT: terms whose
  normalization blows up are stopped before they use all the memory. The session usually survives (see
  [Known issues](#known-issues)).
- Narya stops at the first error of a file; sessions stop loading at the first failing command.

## Search

The index holds every `def`, `axiom` and mutual `and` clause of the project, with its signature and the comment just
above it. Optional JSON files (`search_annotations`) attach labels and descriptions to declarations. Ranking is BM25
over these fields, with identifiers split at `_`, `.` and case changes, and with abbreviations learned from the
project (a comment that says "associativity" above a name that contains `assoc`). The index is cached on disk and
refreshed by file modification times.

With `file` and `line`, results are limited to what is visible there (the import closure and earlier declarations).
With `session_id` and `hole`, the constants of the goal are added to the query. Narya prints goals in normal form,
where defined constants are unfolded, so the source signature of the definition that contains the hole is used too.

## Configuration

A project is configured by a `narya-mcp.toml` at its root (found by walking up from the file), or by TOML files
listed in `NARYA_MCP_CONFIG` (separated by `:`) whose `root` contains the file.

```toml
root = "."                          # project root, relative to this file
narya = "path/to/narya"             # or a launcher, see below
launcher = ["launchers/auto.sh"]
flags = []                          # extra Narya flags, for example ["-parametric"]
path_aliases = ["/remote/root"]     # paths that Narya may report instead of the project root
quiet_codes = ["W2305"]             # diagnostic codes to drop
max_text = 8000                     # longer strings in responses are truncated
search_paths = ["src"]              # indexed directories (default: the root); imports are followed
search_annotations = ["notes.json"] # JSON lists of {declarations, labels, note, ...}
max_sessions = 4
idle_timeout = 1800                 # seconds
command_timeout = 600               # imports and first loads
edit_timeout = 120                  # commands under work
load_timeout = 1800

[env]                               # environment for Narya or the launcher ($VARS expanded, ~ kept)
NAME = "value"
```

`narya`, `launcher` and `[env]` decide what runs, so they are used only from trusted files: those named in
`NARYA_MCP_CONFIG`, or under a directory listed in `NARYA_MCP_TRUSTED`. In other files (a cloned repository, for
example) they are ignored, and responses carry `config_warnings`. `NARYA_MCP_JAIL=<dir>` refuses files outside
`<dir>`.

### Launchers

A launcher runs Narya somewhere else, for example on a larger machine. It is called as
`launcher run <reldir> <narya args...>` and must run Narya in `<reldir>` of its copy of the project, with stdin and
stdout passed through. It may print `narya-mcp-pid <token>` on stderr; signals are then sent with
`launcher signal <token> <SIGNAL>`. Lines that start with `narya-mcp: ` on stderr appear as notes in responses.

Included launchers (each script documents its environment variables):

- `launchers/local-mirror.sh` runs Narya in a local mirror of the project, so compiled `.nyo` files stay out of the
  source tree.
- `launchers/ssh-workspace.sh` runs Narya on another machine, in a workspace copied from a precompiled base. A private
  mount namespace puts the workspace at the path of the base, because `.nyo` files store absolute paths. It limits
  memory and stops Narya when the connection is lost.
- `launchers/auto.sh` uses `ssh-workspace.sh` when the host answers, and `local-mirror.sh` otherwise.

## Measurement

- **Telemetry.** Each call is appended to `$NARYA_MCP_LOG_DIR` (default `~/.local/state/narya-mcp`), one JSONL file
  per server process and day. `NARYA_MCP_LOG=0` disables it. `scripts/mine_telemetry.py` reports success and
  latency per tool, the most frequent failure reasons, tools that are never called, and whether search results were
  used later.
- **Retrieval benchmark.** `scripts/search_eval.py` builds tasks from a project: for a declaration, the query is its
  comment and signature, and the targets are the constants that its proof uses, among the declarations visible
  before it. It uses a split by module and a paired bootstrap against the default settings.
- **End-to-end benchmark.** `scripts/e2e_bench.py` replaces definitions of a project with holes, runs headless Claude
  Code agents in an isolated copy with a budget, and checks the results independently. Its arms differ only in the
  tools offered (`NARYA_MCP_TOOLS`).

## Known issues

- Narya can fail with `anomaly: failure: Meta.Map.find_opt` when it uses a compiled file whose file number changed
  ([gwaithimirdain/narya#142](https://github.com/gwaithimirdain/narya/issues/142)). `narya_check_file` and
  `narya_start` then retry from source.
- A SIGINT during a computation sometimes stops Narya with `Fatal error: exception Stdlib.Sys.Break` instead of a
  `user interrupt` error. This also occurs without narya-mcp. The session is then lost; start a new one.

## License

Apache-2.0
