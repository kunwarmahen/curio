"""Who is using the GPU right now: the cards, the programs on them, and Ollama's models.

Read-only and cheap (one `nvidia-smi` query, one `pmon` sample, `/proc`, Ollama's
`/api/ps`), for the GPU page. Inside the container, `/proc` shows the host's programs
only when it shares the host's process namespace (`--pid=host`, which `curio.sh` adds
with the GPU check); without it the cards and Ollama are still reported.

Commands are shown, but any argument that looks like a secret (a flag or `key=value`
named token, key, secret, password, auth or credential) is hidden: the page can be
opened from the LAN.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

log = logging.getLogger(__name__)


class GpuCard(BaseModel):
    index: int
    name: str
    utilization: float | None = None  # percent
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    temperature_c: float | None = None
    power_w: float | None = None
    power_limit_w: float | None = None
    fan_percent: float | None = None


class GpuProcess(BaseModel):
    gpu: int
    pid: int
    program: str  # the executable's name
    command: str = ""  # its command line, secrets hidden ("" when /proc can't be read)
    app: str = ""  # what started it: a service, a container, an app ("" when unknown)
    kind: Literal["compute", "graphics", "both"] = "compute"
    memory_mb: float | None = None
    utilization: float | None = None  # percent of the GPU's compute, when nvidia-smi says
    running_s: float | None = None
    curio: bool = False  # Curio's own container


class OllamaModel(BaseModel):
    name: str
    size_mb: float
    vram_mb: float
    expires_at: datetime | None = None  # when Ollama unloads it if nothing uses it
    curio_model: bool = False  # one of the models Curio is set to use


class GpuReport(BaseModel):
    checked_at: datetime
    nvidia_smi: bool  # whether nvidia-smi could be run at all
    cards: list[GpuCard] = []
    processes: list[GpuProcess] = []
    # False when the GPU has memory in use but none of its programs can be seen (the
    # container doesn't share the host's process namespace).
    processes_visible: bool = True
    ollama_reachable: bool = False
    ollama: list[OllamaModel] = []


Runner = Callable[[list[str]], str]


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=15, check=True).stdout


def _num(text: str) -> float | None:
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        return None  # "[N/A]", "-", ""


CARD_FIELDS = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed"


def parse_cards(out: str) -> list[GpuCard]:
    cards = []
    for line in out.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 9 or not f[0].isdigit():
            continue
        util, used, total, temp, power, limit, fan = (_num(x) for x in f[2:9])
        cards.append(
            GpuCard(
                index=int(f[0]),
                name=f[1],
                utilization=util,
                memory_used_mb=used,
                memory_total_mb=total,
                temperature_c=temp,
                power_w=power,
                power_limit_w=limit,
                fan_percent=fan,
            )
        )
    return cards


def parse_pmon(out: str) -> list[dict]:
    """`nvidia-smi pmon -c 1 -s um`: columns differ between drivers, so read the header."""
    header: list[str] = []
    rows = []
    for line in out.splitlines():
        if line.startswith("#"):
            if not header:
                header = line.lstrip("#").split()
            continue
        f = line.split()
        if not header or len(f) < len(header) - 1:
            continue
        # The command name is last and may hold spaces ("Code --g").
        cells = dict(zip(header[:-1], f[: len(header) - 1], strict=False))
        cells[header[-1]] = " ".join(f[len(header) - 1 :])
        pid = cells.get("pid", "-")
        if not pid.isdigit():
            continue
        kind = cells.get("type", "C")
        rows.append(
            {
                "gpu": int(cells.get("gpu", "0")) if cells.get("gpu", "0").isdigit() else 0,
                "pid": int(pid),
                "kind": "both" if "+" in kind else ("graphics" if kind == "G" else "compute"),
                "utilization": _num(cells.get("sm", "-")),
                "memory_mb": _num(cells.get("fb", "-")),
                "program": cells.get("command", "") or "",
            }
        )
    return rows


_SECRET = re.compile(r"token|key|secret|pass|auth|cred", re.I)


def tidy_command(argv: list[str]) -> str:
    """The command line with secrets hidden and long arguments shortened."""
    if len(argv) == 1 and " " in argv[0]:
        argv = argv[0].split()  # Chromium-style programs rewrite their argv as one string
    out: list[str] = []
    hide_next = False
    for i, arg in enumerate(argv):
        if hide_next:
            out.append("…")
            hide_next = False
            continue
        if i and arg.startswith("-") and _SECRET.search(arg.split("=", 1)[0]):
            if "=" in arg:
                out.append(arg.split("=", 1)[0] + "=…")
            else:
                out.append(arg)
                hide_next = True
            continue
        if "=" in arg and not arg.startswith("-") and _SECRET.search(arg.split("=", 1)[0]):
            out.append(arg.split("=", 1)[0] + "=…")
            continue
        if i == 0:
            arg = arg.rsplit("/", 1)[-1] if arg.startswith("/") else arg
        elif len(arg) > 48:
            if arg.startswith("-") and "=" in arg:
                arg = arg.split("=", 1)[0] + "=…"
            elif "/" in arg:
                arg = "…/" + arg.rsplit("/", 1)[-1][:32]
            else:
                arg = arg[:32] + "…"
        out.append(arg)
    text = " ".join(out)
    return text if len(text) <= 200 else text[:199] + "…"


_SNAP = re.compile(r"^snap\.([^.]+)\.")
_APP = re.compile(r"^app-(?:gnome-|kde-|flatpak-)?(.+?)(?:-\d+)?\.scope$")
_CONTAINER = re.compile(r"(?:libpod|docker|crio)-(?:conmon-)?([0-9a-f]{12,64})\.scope")
FRIENDLY = {
    "code": "VS Code",
    "brave": "Brave",
    "firefox": "Firefox",
    "google-chrome": "Chrome",
    "org.gnome.Shell@ubuntu": "GNOME desktop",
    "org.gnome.Shell@wayland": "GNOME desktop",
    "org.gnome.Shell@x11": "GNOME desktop",
}


def app_from_cgroup(text: str, own_container: str = "") -> tuple[str, bool]:
    """What started a process, from its cgroup path; and whether it is Curio's container."""
    path = text.strip().splitlines()[-1].split("::", 1)[-1] if text.strip() else ""
    if m := _CONTAINER.search(path):
        cid = m.group(1)
        if own_container and cid.startswith(own_container[:12]):
            return "Curio (this app's container)", True
        return f"Container {cid[:12]}", False
    parts = [p for p in path.split("/") if p and p not in ("..", "container")]
    for part in reversed(parts):
        if part.endswith(".service") and not part.startswith("user@"):
            name = part.removesuffix(".service")
            if m := _SNAP.match(name):
                name = m.group(1)
            return FRIENDLY.get(name, f"{name} service"), False
        if part.endswith(".scope"):
            if part.startswith("session-"):
                return "Login session", False
            if m := _SNAP.match(part) or _APP.match(part):
                return FRIENDLY.get(m.group(1), m.group(1)), False
            return part.removesuffix(".scope"), False
    return "", False


def started_from(app: str, program: str) -> str:
    """An app's own process keeps the app's name; anything else in its scope (a script
    run from VS Code's terminal) was started from it."""
    own = {k for k, v in FRIENDLY.items() if v == app} | {app.lower()}
    if app in FRIENDLY.values() and app != "GNOME desktop" and program.lower() not in own:
        return f"Started from {app}"
    return app


class ProcReader:
    """Reads /proc for a pid; every read can fail (gone, or not visible here)."""

    def __init__(self, root: str = "/proc"):
        self.root = Path(root)

    def cmdline(self, pid: int) -> list[str] | None:
        try:
            raw = (self.root / str(pid) / "cmdline").read_bytes()
        except OSError:
            return None
        return [a for a in raw.decode(errors="replace").split("\0") if a] or None

    def cgroup(self, pid: int) -> str:
        try:
            return (self.root / str(pid) / "cgroup").read_text()
        except OSError:
            return ""

    def running_s(self, pid: int) -> float | None:
        try:
            stat = (self.root / str(pid) / "stat").read_text()
            uptime = float((self.root / "uptime").read_text().split()[0])
            start_ticks = float(stat.rsplit(")", 1)[1].split()[19])
            return max(0.0, uptime - start_ticks / os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError, IndexError):
            return None


def gpu_report(
    *,
    own_models: set[str],
    ollama_client=None,
    nvidia_smi: str | None = None,
    run: Runner = _run,
    proc: ProcReader | None = None,
    own_container: str | None = None,
) -> GpuReport:
    smi = nvidia_smi if nvidia_smi is not None else (shutil.which("nvidia-smi") or "")
    proc = proc or ProcReader()
    own_container = own_container if own_container is not None else socket.gethostname()
    report = GpuReport(checked_at=datetime.now(UTC), nvidia_smi=bool(smi))
    if smi:
        try:
            report.cards = parse_cards(run([smi, f"--query-gpu={CARD_FIELDS}", "--format=csv,noheader,nounits"]))
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("nvidia-smi failed (%s)", exc)
            report.nvidia_smi = False
        rows: list[dict] = []
        if report.nvidia_smi:
            try:
                rows = parse_pmon(run([smi, "pmon", "-c", "1", "-s", "um"]))
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("nvidia-smi pmon failed (%s)", exc)
        for row in rows:
            argv = proc.cmdline(row["pid"])
            app, curio = app_from_cgroup(proc.cgroup(row["pid"]), own_container)
            program = row["program"]
            if argv:
                program = argv[0].split(" ", 1)[0].rsplit("/", 1)[-1]
            app = started_from(app, program)
            report.processes.append(
                GpuProcess(
                    **{**row, "program": program},
                    command=tidy_command(argv) if argv else "",
                    app=app,
                    running_s=proc.running_s(row["pid"]),
                    curio=curio,
                )
            )
        report.processes.sort(key=lambda p: -(p.memory_mb or 0))
        used = sum(c.memory_used_mb or 0 for c in report.cards)
        report.processes_visible = bool(report.processes) or used < 100
    if ollama_client is not None:
        own = {m.removesuffix(":latest") for m in own_models}
        try:
            for m in ollama_client.ps().models:
                expires = getattr(m, "expires_at", None)
                report.ollama.append(
                    OllamaModel(
                        name=m.model,
                        size_mb=(m.size or 0) / 2**20,
                        vram_mb=(getattr(m, "size_vram", 0) or 0) / 2**20,
                        expires_at=expires if isinstance(expires, datetime) else None,
                        curio_model=m.model.removesuffix(":latest") in own,
                    )
                )
            report.ollama_reachable = True
        except Exception as exc:
            log.warning("Couldn't ask Ollama which models are loaded (%s)", exc)
    return report
