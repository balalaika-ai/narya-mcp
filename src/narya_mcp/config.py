"""Project configuration: how to run Narya for files under a given root.

A project is configured by a ``narya-mcp.toml`` file at its root (found by walking up from the file being checked),
or by a TOML file named in ``NARYA_MCP_CONFIG`` (several may be given, separated by ``:``), whose ``root`` key says
which directory it covers.  Without either, Narya is run directly from ``NARYA_BIN`` or ``narya`` on ``PATH``, in the
directory of the file.

Keys (all optional)::

    root = "."                         # project root, relative to the config file
    narya = "path/to/narya.exe"        # Narya executable (direct mode)
    launcher = ["tools/launch.sh"]     # launcher wrapping Narya (see below); takes precedence over `narya`
    flags = ["-parametric"]            # extra Narya flags for every run
    path_aliases = ["/remote/root"]    # directories that Narya may report in place of the project root
    quiet_codes = ["W2305"]            # diagnostic codes to drop from reports (e.g. expected warnings)
    max_sessions = 4                   # live interactive sessions (each one is a Narya process)
    max_text = 8000                    # longest string returned to the client (longer ones are truncated)
    search_paths = ["src"]             # directories indexed by narya_search (default: the whole root; imports are
                                       # followed out of them, e.g. into vendored libraries)
    search_annotations = ["inventory.json"]  # JSON lists of {declarations|names|name, labels, note, ...} attaching
                                       # descriptions to declarations
    idle_timeout = 1800                # seconds before an idle session is closed
    command_timeout = 600              # per-command timeout for imports and file loading, seconds
    edit_timeout = 120                 # per-command timeout for definitions being worked on (exec, solve, try,
                                       # synth, and changed commands replayed by narya_sync): a term whose
                                       # normalization blows up is interrupted before it exhausts memory
    load_timeout = 1800                # default timeout for loading a file into a session / batch checks
    [env]                              # extra environment for Narya / the launcher ($VARS are expanded, ~ is not)
    NAME = "value"

A launcher is called as ``<launcher...> run <reldir> <narya args...>`` and must run Narya with those arguments in the
directory ``<reldir>`` of (its copy of) the project, passing stdin/stdout through.  It may print a line
``narya-mcp-pid <token>`` on stderr; signals (interrupts of long commands) are then delivered by
``<launcher...> signal <token> <SIGNAL>``.  It may print ``narya-mcp-maxrss <KB>`` on stderr at the end of a run.
This is how sources are synchronised to another machine, or how Narya is sandboxed.

Because `narya`, `launcher` and `[env]` decide what gets executed, they are honoured only in trusted configurations:
those named in ``NARYA_MCP_CONFIG``, and ``narya-mcp.toml`` files inside a directory listed in ``NARYA_MCP_TRUSTED``
(``:``-separated).  In any other ``narya-mcp.toml`` (say, in a cloned repository) these keys are ignored, with a
warning.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "narya-mcp.toml"


class ConfigError(Exception):
    pass


@dataclass
class Project:
    root: Path
    config_file: Path | None = None
    narya: str | None = None
    launcher: list[str] | None = None
    flags: list[str] = field(default_factory=list)
    path_aliases: list[str] = field(default_factory=list)
    quiet_codes: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    max_sessions: int = 4
    idle_timeout: float = 1800
    command_timeout: float = 600
    edit_timeout: float = 120
    load_timeout: float = 1800
    max_text: int = 8000  # longest string (type, context entry, message) returned to the client
    search_paths: list[Path] = field(default_factory=list)
    search_annotations: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.root.name

    def narya_executable(self) -> str:
        exe = self.narya or os.environ.get("NARYA_BIN") or shutil.which("narya")
        if not exe:
            raise ConfigError(
                "no Narya executable: set `narya` or `launcher` in narya-mcp.toml, NARYA_BIN, or put narya on PATH"
            )
        return exe

    def command(self, reldir: str, args: list[str]) -> tuple[list[str], str]:
        """argv and cwd for running Narya with ``args`` in directory ``reldir`` of the project."""
        if self.launcher:
            return [*self.launcher, "run", reldir or ".", *self.flags, *args], str(self.root)
        return [self.narya_executable(), *self.flags, *args], str(self.root / reldir)

    def signal_command(self, token: str, sig: str) -> list[str] | None:
        if not self.launcher:
            return None
        return [*self.launcher, "signal", token, sig]

    def environment(self) -> dict[str, str]:
        e = dict(os.environ)
        e.update(self.env)
        return e

    def relpath(self, path: Path) -> str:
        return os.path.relpath(path, self.root)

    def local_path(self, reported: str) -> str:
        """Map a path printed by Narya (possibly on another machine or in a mirror) to a local path."""
        p = reported
        for alias in [*self.path_aliases, str(self.root)]:
            for a in {alias.rstrip("/"), "/private" + alias.rstrip("/")}:
                if p == a or p.startswith(a + "/"):
                    return str(self.root) + p[len(a) :]
        return p

    def describe(self) -> dict:
        d: dict = {"root": str(self.root)}
        if self.config_file:
            d["config"] = str(self.config_file)
        if self.launcher:
            d["launcher"] = self.launcher
        else:
            try:
                d["narya"] = self.narya_executable()
            except ConfigError as e:
                d["narya_error"] = str(e)
        if self.flags:
            d["flags"] = self.flags
        if self.warnings:
            d["warnings"] = self.warnings
        return d


def _expand(base: Path, s: str) -> str:
    s = os.path.expanduser(os.path.expandvars(s))
    if s.startswith("/") or "/" not in s:
        # Bare names (e.g. "narya", "ssh") are looked up on PATH.
        return s
    return str((base / s).resolve())


UNSAFE_KEYS = ("narya", "launcher", "env")


def _trusted(path: Path) -> bool:
    for d in filter(None, os.environ.get("NARYA_MCP_TRUSTED", "").split(":")):
        root = Path(os.path.expanduser(d)).resolve()
        if path == root or root in path.parents:
            return True
    return False


def load_config(path: Path, trusted: bool = True) -> Project:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from None
    base = path.parent
    root = (base / os.path.expanduser(data.get("root", "."))).resolve()
    proj = Project(root=root, config_file=path)
    if not trusted:
        ignored = [k for k in UNSAFE_KEYS if k in data]
        for k in ignored:
            del data[k]
        if ignored:
            proj.warnings.append(
                f"{path}: ignored {', '.join(ignored)} (untrusted configuration; list its directory in "
                "NARYA_MCP_TRUSTED or name it in NARYA_MCP_CONFIG)"
            )
    if "narya" in data:
        proj.narya = _expand(base, data["narya"])
    if "launcher" in data:
        launcher = data["launcher"]
        if isinstance(launcher, str):
            launcher = [launcher]
        proj.launcher = [_expand(base, launcher[0]), *launcher[1:]]
    proj.flags = list(data.get("flags", []))
    proj.path_aliases = [os.path.expanduser(a) for a in data.get("path_aliases", [])]
    proj.quiet_codes = list(data.get("quiet_codes", []))
    proj.search_paths = [(root / os.path.expanduser(p)).resolve() for p in data.get("search_paths", [])]
    proj.search_annotations = [(root / os.path.expanduser(p)).resolve() for p in data.get("search_annotations", [])]
    # Only $VARS are expanded: a leading ~ may refer to a home directory on another machine.
    proj.env = {k: os.path.expandvars(str(v)) for k, v in data.get("env", {}).items()}
    for key in ("max_sessions", "idle_timeout", "command_timeout", "edit_timeout", "load_timeout", "max_text"):
        if key in data:
            setattr(proj, key, type(getattr(proj, key))(data[key]))
    return proj


def _env_configs() -> list[Project]:
    out = []
    for p in filter(None, os.environ.get("NARYA_MCP_CONFIG", "").split(":")):
        out.append(load_config(Path(os.path.expanduser(p)).resolve()))
    return out


def find_project(path: str | Path | None) -> Project:
    """The project covering ``path`` (a file or directory); defaults to the current directory."""
    p = Path(os.path.expanduser(str(path))).resolve() if path else Path.cwd()
    start = p if p.is_dir() else p.parent
    for d in [start, *start.parents]:
        cfg = d / CONFIG_NAME
        if cfg.is_file():
            env_cfgs = {Path(os.path.expanduser(c)).resolve() for c in os.environ.get("NARYA_MCP_CONFIG", "").split(":") if c}
            return load_config(cfg, trusted=cfg.resolve() in env_cfgs or _trusted(cfg.resolve()))
    best: Project | None = None
    for proj in _env_configs():
        if p == proj.root or proj.root in p.parents:
            if best is None or len(str(proj.root)) > len(str(best.root)):
                best = proj
    if best:
        return best
    return Project(root=start)


def default_project() -> Project:
    """The project for calls that name no file: the first NARYA_MCP_CONFIG entry, else the current directory."""
    envs = _env_configs()
    return envs[0] if envs else find_project(None)
