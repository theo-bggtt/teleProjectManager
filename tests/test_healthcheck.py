"""Tests for tgbot.healthcheck."""
from tgbot.healthcheck import (
    DiskUsage,
    HealthSnapshot,
    ProcessInfo,
    _format_bytes,
    _format_duration,
    collect,
    format_snapshot,
)


# ─── _format_duration ────────────────────────────────────────────────────

def test_format_duration_zero():
    assert _format_duration(0) == "0s"


def test_format_duration_seconds():
    assert _format_duration(45) == "45s"


def test_format_duration_minutes():
    assert _format_duration(90) == "1m 30s"


def test_format_duration_hours():
    assert _format_duration(3700) == "1h 1m"


def test_format_duration_days():
    assert _format_duration(90061) == "1j 1h 1m"


# ─── _format_bytes ───────────────────────────────────────────────────────

def test_format_bytes_zero():
    assert _format_bytes(0) == "0 B"


def test_format_bytes_under_kb():
    assert _format_bytes(512) == "512 B"


def test_format_bytes_kb():
    assert _format_bytes(2048) == "2.0 KB"


def test_format_bytes_mb():
    assert _format_bytes(1_572_864) == "1.5 MB"


def test_format_bytes_gb():
    assert _format_bytes(1_610_612_736) == "1.5 GB"


# ─── format_snapshot ─────────────────────────────────────────────────────

def _sample(load_avg=(0.4, 0.6, 0.5), top_cpu=None, top_ram=None):
    return HealthSnapshot(
        uptime_seconds=90061,
        cpu_percent=23.0,
        load_avg=load_avg,
        ram_used_bytes=1_288_490_188,
        ram_total_bytes=4_294_967_296,
        ram_percent=30.0,
        disks=[
            DiskUsage(
                mount="/",
                used_bytes=13_207_960_780,
                total_bytes=68_719_476_736,
                percent=19.0,
            ),
        ],
        top_cpu=top_cpu if top_cpu is not None else [
            ProcessInfo(pid=1, name="python3", cpu_percent=18.0, rss_bytes=257_000_000),
            ProcessInfo(pid=2, name="node", cpu_percent=4.0, rss_bytes=80_000_000),
        ],
        top_ram=top_ram if top_ram is not None else [
            ProcessInfo(pid=1, name="python3", cpu_percent=18.0, rss_bytes=257_000_000),
            ProcessInfo(pid=3, name="postgres", cpu_percent=1.0, rss_bytes=80_000_000),
        ],
    )


def test_format_snapshot_contains_essentials():
    text = format_snapshot(_sample())
    assert "Uptime" in text
    assert "CPU" in text
    assert "RAM" in text
    assert "1j 1h 1m" in text  # uptime
    assert "/" in text  # mount label
    assert "python3" in text


def test_format_snapshot_no_load_avg():
    text = format_snapshot(_sample(load_avg=None))
    assert "n/a" in text.lower()


def test_format_snapshot_empty_top():
    text = format_snapshot(_sample(top_cpu=[], top_ram=[]))
    # sections still present, no crash
    assert "Top CPU" in text
    assert "Top RAM" in text


def test_format_snapshot_long_process_name_truncated():
    very_long = "a" * 64
    snap = _sample(
        top_cpu=[ProcessInfo(pid=1, name=very_long, cpu_percent=1.0, rss_bytes=1)],
    )
    text = format_snapshot(snap)
    # truncation kicks in (max 24 chars)
    assert very_long not in text


# ─── collect (smoke) ─────────────────────────────────────────────────────

def test_collect_returns_snapshot(tmp_path):
    """Smoke test: collect() works on the current platform with a real mount."""
    snap = collect([str(tmp_path)])
    assert snap.uptime_seconds > 0
    assert snap.ram_total_bytes > 0
    assert isinstance(snap.cpu_percent, float)
    assert isinstance(snap.top_cpu, list)
    assert isinstance(snap.top_ram, list)
    # tmp_path is a valid mount (lives under some real fs)
    assert len(snap.disks) == 1
    assert snap.disks[0].total_bytes > 0


def test_collect_skips_invalid_mount(tmp_path):
    bogus = str(tmp_path / "definitely-does-not-exist-xyz")
    snap = collect([bogus, str(tmp_path)])
    # invalid mount silently dropped, valid one kept
    assert len(snap.disks) == 1
    assert snap.disks[0].mount == str(tmp_path)


def test_collect_top_n_limit():
    snap = collect(["."], top_n=2)
    assert len(snap.top_cpu) <= 2
    assert len(snap.top_ram) <= 2
