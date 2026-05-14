"""System healthcheck snapshot for the admin menu.

Lecture seule, cross-platform (psutil). Une seule fonction "impure" (`collect`)
qui lit le système ; tout le reste est pur et testable sans mock.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from html import escape as html_escape
from typing import Optional

import psutil


logger = logging.getLogger(__name__)


# ─── Dataclasses ─────────────────────────────────────────────────────────

@dataclass
class DiskUsage:
    mount: str
    used_bytes: int
    total_bytes: int
    percent: float


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cpu_percent: float
    rss_bytes: int


@dataclass
class HealthSnapshot:
    uptime_seconds: int
    cpu_percent: float
    load_avg: Optional[tuple[float, float, float]]  # None sur Windows
    ram_used_bytes: int
    ram_total_bytes: int
    ram_percent: float
    disks: list[DiskUsage] = field(default_factory=list)
    top_cpu: list[ProcessInfo] = field(default_factory=list)
    top_ram: list[ProcessInfo] = field(default_factory=list)


# ─── Collecte ────────────────────────────────────────────────────────────

_PROC_NAME_MAX = 24
_CPU_SAMPLE_INTERVAL = 0.1


def collect(mounts: list[str], *, top_n: int = 3) -> HealthSnapshot:
    """Snapshot système synchrone. Tolère silencieusement les erreurs par mount/process."""
    uptime = int(time.time() - psutil.boot_time())
    cpu = psutil.cpu_percent(interval=_CPU_SAMPLE_INTERVAL)

    try:
        load_avg = psutil.getloadavg()
    except (AttributeError, OSError):
        load_avg = None

    vm = psutil.virtual_memory()
    ram_used = int(vm.total - vm.available)

    disks: list[DiskUsage] = []
    for mount in mounts:
        try:
            du = psutil.disk_usage(mount)
        except OSError as e:
            logger.warning("disk_usage(%r) failed: %s", mount, e)
            continue
        disks.append(
            DiskUsage(
                mount=mount,
                used_bytes=int(du.used),
                total_bytes=int(du.total),
                percent=float(du.percent),
            )
        )

    top_cpu, top_ram = _collect_top_processes(top_n)

    return HealthSnapshot(
        uptime_seconds=uptime,
        cpu_percent=float(cpu),
        load_avg=load_avg,
        ram_used_bytes=ram_used,
        ram_total_bytes=int(vm.total),
        ram_percent=float(vm.percent),
        disks=disks,
        top_cpu=top_cpu,
        top_ram=top_ram,
    )


def _collect_top_processes(top_n: int) -> tuple[list[ProcessInfo], list[ProcessInfo]]:
    """Return (top_cpu, top_ram). Needs two passes: cpu_percent() first call is 0.0."""
    procs: list[psutil.Process] = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(interval=None)  # prime
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    time.sleep(_CPU_SAMPLE_INTERVAL)

    snapshots: list[ProcessInfo] = []
    for p in procs:
        try:
            with p.oneshot():
                snapshots.append(
                    ProcessInfo(
                        pid=p.pid,
                        name=p.name() or "?",
                        cpu_percent=float(p.cpu_percent(interval=None)),
                        rss_bytes=int(p.memory_info().rss),
                    )
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    top_cpu = sorted(snapshots, key=lambda x: x.cpu_percent, reverse=True)[:top_n]
    top_ram = sorted(snapshots, key=lambda x: x.rss_bytes, reverse=True)[:top_n]
    return top_cpu, top_ram


# ─── Formatage ───────────────────────────────────────────────────────────

def format_snapshot(snap: HealthSnapshot) -> str:
    """Render a Telegram-friendly HTML block. User-controlled values (process
    names, mount labels) are escaped inline; the assembled output is *not*
    re-escaped."""
    out: list[str] = ["🩺 <b>Healthcheck</b>", "", "<pre>"]

    out.append(f"⏱  Uptime  : {_format_duration(snap.uptime_seconds)}")

    if snap.load_avg is not None:
        la = snap.load_avg
        out.append(
            f"📊 CPU     : {snap.cpu_percent:.0f}% "
            f"(load {la[0]:.2f} / {la[1]:.2f} / {la[2]:.2f})"
        )
    else:
        out.append(f"📊 CPU     : {snap.cpu_percent:.0f}% (load n/a)")

    out.append(
        f"🧠 RAM     : {_format_bytes(snap.ram_used_bytes)} "
        f"/ {_format_bytes(snap.ram_total_bytes)} ({snap.ram_percent:.0f}%)"
    )

    for d in snap.disks:
        label = html_escape(_truncate(d.mount, 8), quote=False)
        out.append(
            f"💾 {label:<7} : {_format_bytes(d.used_bytes)} "
            f"/ {_format_bytes(d.total_bytes)} ({d.percent:.0f}%)"
        )

    out.append("")
    out.append("🔥 Top CPU")
    if snap.top_cpu:
        for i, p in enumerate(snap.top_cpu, 1):
            name = html_escape(_truncate(p.name, _PROC_NAME_MAX), quote=False)
            out.append(f"  {i}. {name:<{_PROC_NAME_MAX}} {p.cpu_percent:5.1f}%")
    else:
        out.append("  (aucun)")

    out.append("")
    out.append("🐘 Top RAM")
    if snap.top_ram:
        for i, p in enumerate(snap.top_ram, 1):
            name = html_escape(_truncate(p.name, _PROC_NAME_MAX), quote=False)
            out.append(
                f"  {i}. {name:<{_PROC_NAME_MAX}} {_format_bytes(p.rss_bytes):>10}"
            )
    else:
        out.append("  (aucun)")

    out.append("</pre>")
    return "\n".join(out)


# ─── Helpers purs ────────────────────────────────────────────────────────

def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}j {hours}h {minutes}m"


_UNITS = ["B", "KB", "MB", "GB", "TB"]


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value = float(n)
    unit_idx = 0
    while value >= 1024 and unit_idx < len(_UNITS) - 1:
        value /= 1024
        unit_idx += 1
    return f"{value:.1f} {_UNITS[unit_idx]}"


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"
