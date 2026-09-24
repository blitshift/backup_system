"""Restic backup operations."""

import os
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from .config import Config, ResticConfig
from .secrets import get_restic_password


class ResticError(Exception):
    """Error during restic operation."""
    pass


# Fixed cache location. Under systemd hardening $HOME is unset, so restic's
# default ($HOME/.cache/restic) is unavailable and it would run cacheless
# (slow + heavier on the storage box). /var/lib/backup is in every unit's
# ReadWritePaths, so this is writable under ProtectSystem=strict.
RESTIC_CACHE_DIR = "/var/lib/backup/restic-cache"

# How long a restic command waits for a conflicting lock before giving up.
# `restic check` holds an EXCLUSIVE lock for its whole run (~1 h for the nightly
# 1/7 subset on a ~1 TiB repo), so an hourly backup colliding with it must be
# able to outwait it -- a backup that waits is delayed, one that times out fails.
RETRY_LOCK = "90m"

# The nightly verify reads 1/READ_DATA_GROUPS of the pack files, one group per
# ISO weekday, so all data is read once every 7 days (see daily_read_data_subset).
READ_DATA_GROUPS = 7


def _get_repo_url(config: Config) -> str:
    """Build the restic repository URL for sftp."""
    sb = config.restic.storage_box
    return f"sftp:{sb.user}@{sb.host}:{sb.path}/{config.machine.name}"


def _run_restic(
    config: Config,
    args: list[str],
    password: str,
    capture_output: bool = False,
    retry_lock: bool = True,
) -> subprocess.CompletedProcess:
    """Run a restic command with proper environment."""
    repo_url = _get_repo_url(config)

    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = repo_url
    env["RESTIC_PASSWORD"] = password

    # Ensure restic has a writable cache dir (see RESTIC_CACHE_DIR comment).
    if "RESTIC_CACHE_DIR" not in env:
        try:
            Path(RESTIC_CACHE_DIR).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        env["RESTIC_CACHE_DIR"] = RESTIC_CACHE_DIR

    # Under systemd, stdout isn't a TTY, so restic prints nothing until the end.
    # A low progress FPS makes it emit a status line periodically (~once/minute)
    # into the journal. An explicit env var still wins.
    env.setdefault("RESTIC_PROGRESS_FPS", "0.0166")

    # Add SSH key if specified
    if config.restic.storage_box.ssh_key:
        env["RESTIC_SFTP_ARGS"] = f"-i {config.restic.storage_box.ssh_key}"

    # Build command with retry-lock to wait if repo is locked
    cmd = ["restic"]
    if retry_lock:
        cmd.extend(["--retry-lock", RETRY_LOCK])
    cmd.extend(args)

    result = subprocess.run(
        cmd,
        env=env,
        capture_output=capture_output,
        text=True,
    )

    if result.returncode != 0:
        error_msg = result.stderr if capture_output else f"Exit code {result.returncode}"
        raise ResticError(f"Restic command failed: {error_msg}")

    return result


def unlock_stale(config: Config, password: str) -> None:
    """Remove stale locks from the repository (self-healing).

    `restic unlock` (without --remove-all) removes ONLY stale locks: ones whose
    creating process is dead, or ones that have gone unrefreshed. It never
    removes a lock that a live, actively-refreshing restic process holds, so
    this is safe to call before every operation even when a legitimate
    concurrent backup/verify is running. Because each machine has its own repo
    path, a lock left by a hard-killed local run (same host + dead PID) is
    detected as stale immediately -- no 30-minute wait.

    Without this, a single interrupted run (OOM, power loss, SIGKILL, a
    timed-out unit) leaves a lock that would block this run and every run after
    it on --retry-lock until a human intervened -- turning one dead backup into
    an indefinite silent outage.

    Best effort: a failure here (e.g. transient network) is swallowed so the
    real operation runs and surfaces any genuine lock/connection error itself.
    """
    try:
        _run_restic(config, ["unlock"], password, capture_output=True, retry_lock=False)
    except ResticError:
        pass


def init_repo(config: Config, password: str) -> None:
    """Initialize a new restic repository."""
    _run_restic(config, ["init"], password)


def check_repo_exists(config: Config, password: str) -> bool:
    """Check if the restic repository exists and is accessible."""
    try:
        _run_restic(config, ["snapshots", "--latest", "1"], password, capture_output=True)
        return True
    except ResticError:
        return False


def run_backup(
    config: Config,
    paths: list[Path],
    password: str,
    dry_run: bool = False,
    excludes: list[Path] | None = None,
) -> None:
    """Run a restic backup of the given paths.

    `excludes` are absolute paths (typically the .nobackup directories found by
    the scanner) to skip even though they sit inside a backed-up tree.
    """
    if not paths:
        raise ResticError("No paths to backup")

    args = ["backup"]

    # Add global exclude patterns
    for pattern in config.restic.exclude:
        args.extend(["--exclude", pattern])

    # Add per-path excludes (.nobackup directories inside backed-up trees)
    for ex in (excludes or []):
        args.extend(["--exclude", str(ex)])

    if dry_run:
        args.append("--dry-run")

    # Add paths
    args.extend(str(p) for p in paths)

    _run_restic(config, args, password)


def run_forget_and_prune(config: Config, password: str) -> None:
    """Apply retention policy and prune old snapshots."""
    retention = config.restic.retention

    args = [
        "forget",
        "--prune",
        # Group by host+tags, NOT paths. With restic's default (host,paths) the
        # old per-run dump paths made every DB snapshot its own group of one, so
        # retention never pruned them (this is how 1200+ snapshots piled up).
        # Grouping by tags fixes that. Each database dump carries a distinct
        # per-database tag (see backup_database_dump), so every database forms
        # its own retention group and keep-* applies per database; file backups
        # (no tags) share a single group.
        "--group-by", "host,tags",
        "--keep-hourly", str(retention.get("hourly", 24)),
        "--keep-daily", str(retention.get("daily", 7)),
        "--keep-weekly", str(retention.get("weekly", 4)),
        "--keep-monthly", str(retention.get("monthly", 12)),
    ]

    _run_restic(config, args, password)


def daily_read_data_subset(day: date | None = None) -> str:
    """Return today's `--read-data-subset` value, e.g. "3/7" on a Wednesday.

    The group is the ISO weekday (Mon=1 .. Sun=7), so seven consecutive nightly
    runs read every pack file once. restic assigns packs to groups by pack ID,
    so the split is stable across runs (new packs land in some group and get
    read within a week).
    """
    day = day or date.today()
    return f"{day.isoweekday()}/{READ_DATA_GROUPS}"


def run_check(
    config: Config,
    password: str,
    read_data: bool = False,
    read_data_subset: str | None = None,
) -> None:
    """Run restic check (verification).

    The structural check (index, snapshots, trees) is part of every run; the
    flags below add reading pack data on top.

    Args:
        config: Backup configuration
        password: Repository password
        read_data: If True, read and verify ALL data (slow: hours on a large repo)
        read_data_subset: Read and verify only this subset ("n/t"), e.g. the
            nightly group from daily_read_data_subset(). Ignored if read_data.
    """
    args = ["check"]
    if read_data:
        args.append("--read-data")
    elif read_data_subset:
        args.append(f"--read-data-subset={read_data_subset}")

    _run_restic(config, args, password)


def list_snapshots(config: Config, password: str) -> str:
    """List all snapshots in the repository."""
    result = _run_restic(config, ["snapshots"], password, capture_output=True)
    return result.stdout


def backup_database_dump(
    config: Config,
    dump_path: Path,
    password: str,
    tag: str = "database",
) -> None:
    """Backup a database dump file.

    Tags the snapshot with both the generic `tag` ("database") and the database
    name (the dump file stem). The per-database tag is REQUIRED for correct
    retention: forget groups by (host, tags), so if every dump carried only the
    shared "database" tag they would all fall in one group and keep-hourly/daily
    /... would collapse them to a single surviving snapshot per run -- silently
    dropping every database but the last one dumped (on a multi-DB host this
    means most databases have no retained backup). A distinct per-database tag
    puts each database in its own retention group, and also makes restores
    selectable with `restic snapshots --tag <db>`.
    """
    args = [
        "backup",
        "--tag", tag,
        "--tag", dump_path.stem,
        str(dump_path),
    ]
    _run_restic(config, args, password)


def get_snapshots_json(config: Config, password: str, latest: int | None = None) -> list[dict]:
    """Get snapshots as parsed JSON."""
    import json
    args = ["snapshots", "--json"]
    if latest:
        args.extend(["--latest", str(latest)])
    result = _run_restic(config, args, password, capture_output=True)
    if not result.stdout.strip():
        return []
    return json.loads(result.stdout)


def get_stats(config: Config, password: str) -> dict:
    """Get repository statistics."""
    import json
    result = _run_restic(config, ["stats", "--json"], password, capture_output=True)
    return json.loads(result.stdout)


def get_repo_info(config: Config, password: str) -> dict:
    """Get comprehensive repository info for display."""
    info = {
        "repository": _get_repo_url(config),
        "snapshots": [],
        "stats": None,
        "latest": None,
    }

    # Get all snapshots
    snapshots = get_snapshots_json(config, password)
    info["snapshots"] = snapshots

    # Get latest snapshot details
    if snapshots:
        latest = get_snapshots_json(config, password, latest=1)
        if latest:
            info["latest"] = latest[0]

    # Get stats
    try:
        info["stats"] = get_stats(config, password)
    except ResticError:
        pass

    return info
