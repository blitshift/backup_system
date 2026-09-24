#!/usr/bin/env python3
"""Interactive setup script for the backup system.

Run as regular user - will request sudo for privileged operations.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


# Root-owned conda install (Miniforge) + dedicated env.
# We use conda rather than a venv because a venv borrows the system Python via a
# symlink; on a rolling distro an interpreter minor-version bump (e.g. Arch
# 3.13 -> 3.14) invalidates it. A conda env ships its own interpreter, so a
# system Python upgrade can't break it.
CONDA_ROOT = "/opt/miniforge"
ENV_PREFIX = f"{CONDA_ROOT}/envs/backup"
BACKUP_BIN = f"{ENV_PREFIX}/bin/backup"
PYTHON_VERSION = "3.14"  # pin the newest stable; the env owns its own interpreter
MINIFORGE_URL = (
    "https://github.com/conda-forge/miniforge/releases/latest/download/"
    "Miniforge3-Linux-x86_64.sh"
)
OLD_VENV_PATH = "/opt/backup-venv"  # legacy install to clean up
NOTIFY_SCRIPT = "/opt/backup-notify-failure.py"  # stdlib-only OnFailure handler
BIN_SYMLINK = "/usr/local/bin/backup"
# Units from earlier versions that a re-install removes: the weekly deep verify
# was replaced by the nightly `backup verify` (reads 1/7 of the data per night).
RETIRED_UNITS = ["backup-verify-deep.timer", "backup-verify-deep.service"]

CONFIG_DIR = "/etc/backup"
STATE_DIR = "/var/lib/backup"
SCRIPT_DIR = Path(__file__).parent.resolve()


def run_sudo(cmd: list[str], check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a command with sudo."""
    return subprocess.run(["sudo"] + cmd, check=check, **kwargs)


def run_sudo_sh(script: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell script with sudo."""
    return subprocess.run(["sudo", "sh", "-c", script], check=check)


def prompt(msg: str, default: str = "") -> str:
    """Prompt for input with optional default."""
    if default:
        result = input(f"{msg} [{default}]: ").strip()
        return result if result else default
    return input(f"{msg}: ").strip()


def prompt_list(msg: str) -> list[str]:
    """Prompt for a list of items, one per line."""
    print(f"{msg} (one per line, empty line to finish):")
    items = []
    while True:
        item = input("  > ").strip()
        if not item:
            break
        items.append(item)
    return items


def prompt_yn(msg: str, default: bool = True) -> bool:
    """Prompt for yes/no."""
    hint = "Y/n" if default else "y/N"
    result = input(f"{msg} [{hint}]: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def check_dependencies():
    """Check that required system tools are installed."""
    missing = []
    for cmd in ["restic", "plocate", "ssh"]:
        if subprocess.run(["which", cmd], capture_output=True).returncode != 0:
            missing.append(cmd)

    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print("Install them first:")
        print("  sudo pacman -S restic plocate openssh")
        sys.exit(1)


def fix_plocate_config():
    """Fix plocate config for btrfs systems.

    plocate incorrectly detects directories as bind mounts on btrfs,
    causing it to skip /home. This sets PRUNE_BIND_MOUNTS = "no".
    """
    print("\n=== Checking plocate configuration ===")

    updatedb_conf = Path("/etc/updatedb.conf")
    if not updatedb_conf.exists():
        print("  /etc/updatedb.conf not found, skipping")
        return

    content = updatedb_conf.read_text()

    if 'PRUNE_BIND_MOUNTS = "yes"' in content:
        print("  Fixing PRUNE_BIND_MOUNTS for btrfs compatibility...")
        new_content = content.replace(
            'PRUNE_BIND_MOUNTS = "yes"',
            'PRUNE_BIND_MOUNTS = "no"'
        )
        run_sudo_sh(f"cat > /etc/updatedb.conf << 'EOF'\n{new_content}EOF")
        print("  Set PRUNE_BIND_MOUNTS = \"no\"")
    else:
        print("  PRUNE_BIND_MOUNTS already set to \"no\" or not present")

    # Rebuild plocate database
    print("  Rebuilding plocate database...")
    run_sudo(["updatedb"])

    # Verify plocate can find files in /home
    home = os.environ.get("HOME", "/home")
    result = subprocess.run(
        ["plocate", "-c", home],
        capture_output=True,
        text=True,
    )
    count = int(result.stdout.strip()) if result.returncode == 0 else 0
    if count > 0:
        print(f"  Verified: plocate indexed {count} files under {home}")
    else:
        print(f"  WARNING: plocate found no files under {home}")
        print("  Marker file scanning may not work correctly.")


def create_conda_env():
    """Create the root-owned conda env and install the package into it.

    The package is installed NON-editable (the code is copied into the env), so
    root's backup service does not depend on the source tree under /home.
    """
    print(f"\n=== Setting up conda environment at {ENV_PREFIX} ===")

    conda = f"{CONDA_ROOT}/bin/conda"

    # 1. Install Miniforge to /opt if it isn't there yet.
    if not Path(conda).exists():
        print(f"Miniforge not found at {CONDA_ROOT}, installing it...")
        installer = "/tmp/miniforge-installer.sh"
        print(f"  Downloading {MINIFORGE_URL}")
        import urllib.request
        urllib.request.urlretrieve(MINIFORGE_URL, installer)
        run_sudo(["bash", installer, "-b", "-p", CONDA_ROOT])
        try:
            os.unlink(installer)
        except OSError:
            pass
    else:
        print(f"Using existing Miniforge at {CONDA_ROOT}")

    # 2. Create (or recreate) the env with a pinned Python.
    if Path(ENV_PREFIX).exists():
        if prompt_yn(f"Env {ENV_PREFIX} exists. Recreate from scratch?", default=False):
            run_sudo([conda, "env", "remove", "-y", "-p", ENV_PREFIX])
        else:
            print("Keeping existing env, will reinstall the package into it.")
    if not Path(ENV_PREFIX).exists():
        run_sudo([conda, "create", "-y", "-p", ENV_PREFIX, f"python={PYTHON_VERSION}"])

    # 3. Install the backup package (non-editable) and its deps.
    run_sudo([f"{ENV_PREFIX}/bin/pip", "install", "--upgrade", "pip"])
    run_sudo([f"{ENV_PREFIX}/bin/pip", "install", "--force-reinstall", "--no-deps", str(SCRIPT_DIR)])
    run_sudo([f"{ENV_PREFIX}/bin/pip", "install", str(SCRIPT_DIR)])
    print(f"Installed backup package to {ENV_PREFIX}")

    # 4. Convenience symlink so `sudo backup ...` works without the full path.
    run_sudo(["ln", "-sf", BACKUP_BIN, BIN_SYMLINK])
    print(f"Linked {BIN_SYMLINK} -> {BACKUP_BIN}")

    # 5. Remove the legacy venv if present (it's the broken one).
    if Path(OLD_VENV_PATH).exists():
        if prompt_yn(f"Remove old/broken venv at {OLD_VENV_PATH}?", default=True):
            run_sudo(["rm", "-rf", OLD_VENV_PATH])
            print(f"Removed {OLD_VENV_PATH}")


def install_notifier():
    """Install the stdlib-only OnFailure notifier (dead-man's-switch).

    Copied to /opt and run by the SYSTEM python, so it still fires when the
    backup conda env itself is broken.
    """
    print(f"\n=== Installing failure notifier ===")
    src = SCRIPT_DIR / "systemd" / "notify-failure.py"
    run_sudo(["cp", str(src), NOTIFY_SCRIPT])
    run_sudo(["chmod", "755", NOTIFY_SCRIPT])
    print(f"Installed {NOTIFY_SCRIPT}")


def create_config(machine_name: str, backup_paths: list[str], scan_paths: list[str],
                  storage_host: str, storage_user: str, storage_path: str,
                  databases: list[str], kuma_backup: str, kuma_verify: str,
                  base_path_strip: str):
    """Create config files."""
    print(f"\n=== Creating config in {CONFIG_DIR} ===")

    run_sudo(["mkdir", "-p", CONFIG_DIR])

    # Common config
    common_config = f'''# Backup system common configuration

[restic]
# Exclude patterns MUST stay under [restic], before any [restic.*] sub-table:
# in TOML a key after a sub-table header binds to that sub-table, so placing
# `exclude` after [restic.retention] makes it restic.retention.exclude and the
# loader (reading restic.exclude) silently gets no excludes.
exclude = [
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".git",
    ".cache",
    "*.tmp",
    "*.swp",
    ".thumbnails",
    "Cache",
    "CachedData",
]

[restic.retention]
hourly = 24
daily = 7
weekly = 4
monthly = 12

[restic.storage_box]
host = "{storage_host}"
user = "{storage_user}"
path = "{storage_path}"

[cold_storage]
base_path_strip = "{base_path_strip}"

[cold_storage.storage_box]
host = "{storage_host}"
user = "{storage_user}"
path = "/cold"
'''

    # Machine config
    scan_paths_toml = ",\n    ".join(f'"{p}"' for p in scan_paths)
    extra_paths_toml = ",\n    ".join(f'"{p}"' for p in backup_paths)
    databases_toml = ",\n    ".join(f'"{d}"' for d in databases) if databases else ""

    machine_config = f'''# Machine-specific configuration

name = "{machine_name}"

scan_paths = [
    {scan_paths_toml}
]

extra_backup_paths = [
    {extra_paths_toml}
]

databases = [
    {databases_toml}
]

[kuma]
backup = "{kuma_backup}"
verify = "{kuma_verify}"
'''

    # Write configs via sudo
    config_path = f"{CONFIG_DIR}/config.toml"
    machine_path = f"{CONFIG_DIR}/machine.toml"

    if Path(config_path).exists():
        if not prompt_yn(f"{config_path} exists. Overwrite?", default=False):
            print("Keeping existing config.toml")
        else:
            run_sudo_sh(f"cat > {config_path} << 'EOFCONFIG'\n{common_config}\nEOFCONFIG")
            print(f"Created {config_path}")
    else:
        run_sudo_sh(f"cat > {config_path} << 'EOFCONFIG'\n{common_config}\nEOFCONFIG")
        print(f"Created {config_path}")

    if Path(machine_path).exists():
        if not prompt_yn(f"{machine_path} exists. Overwrite?", default=False):
            print("Keeping existing machine.toml")
        else:
            run_sudo_sh(f"cat > {machine_path} << 'EOFCONFIG'\n{machine_config}\nEOFCONFIG")
            print(f"Created {machine_path}")
    else:
        run_sudo_sh(f"cat > {machine_path} << 'EOFCONFIG'\n{machine_config}\nEOFCONFIG")
        print(f"Created {machine_path}")


def update_systemd_units():
    """Point the backup service units at the conda env's `backup` binary.

    Only touches the two real backup services -- NOT backup-onfailure@.service,
    which intentionally runs the system python against the standalone notifier.
    """
    print(f"\n=== Updating systemd units ===")

    systemd_dir = SCRIPT_DIR / "systemd"
    exec_lines = {
        "backup.service": f"ExecStart={BACKUP_BIN} run",
        "backup-verify.service": f"ExecStart={BACKUP_BIN} verify",
    }

    for name, exec_line in exec_lines.items():
        service_file = systemd_dir / name
        lines = [
            exec_line if line.startswith("ExecStart=") else line
            for line in service_file.read_text().split("\n")
        ]
        service_file.write_text("\n".join(lines))

    print(f"Updated service files to use {BACKUP_BIN}")


def install_systemd():
    """Install and enable systemd timers."""
    print(f"\n=== Installing systemd timers ===")

    systemd_dir = SCRIPT_DIR / "systemd"

    # Remove retired units left by an earlier install. Idempotent; stop and
    # disable are separate calls with errors ignored because disable fails on
    # the dangling symlinks left once the unit files are gone from the repo
    # (same logic as systemd/install.sh).
    for unit in RETIRED_UNITS:
        dst = Path(f"/etc/systemd/system/{unit}")
        if dst.is_symlink() or dst.exists():
            print(f"  Removing retired {unit}")
        run_sudo(["systemctl", "stop", unit], check=False, capture_output=True)
        run_sudo(["systemctl", "disable", unit], check=False, capture_output=True)
        run_sudo(["rm", "-f", str(dst), f"/etc/systemd/system/timers.target.wants/{unit}"])

    for unit in ["backup.service", "backup.timer",
                 "backup-verify.service", "backup-verify.timer",
                 "backup-onfailure@.service"]:
        src = systemd_dir / unit
        dst = f"/etc/systemd/system/{unit}"
        run_sudo(["ln", "-sf", str(src), dst])
        print(f"  Linked {unit}")

    run_sudo(["systemctl", "daemon-reload"])
    run_sudo(["systemctl", "reset-failed", *RETIRED_UNITS], check=False, capture_output=True)

    for timer in ["backup.timer", "backup-verify.timer"]:
        result = subprocess.run(
            ["sudo", "systemctl", "enable", "--now", timer],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"  Enabled {timer}")
        else:
            print(f"  FAILED to enable {timer}: {result.stderr.strip()}")

    # Show timer status
    print("\nTimer status:")
    subprocess.run(["systemctl", "list-timers", "backup*", "--no-pager"])


def run_init():
    """Run backup init."""
    print(f"\n=== Initializing backup system ===")
    run_sudo([BACKUP_BIN, "init"])


def main():
    parser = argparse.ArgumentParser(description="Set up the backup system")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Use defaults, fail if required values missing")
    args = parser.parse_args()

    print("=" * 60)
    print("Backup System Setup")
    print("=" * 60)
    print("\nThis script will:")
    print(f"  1. Set up a root-owned conda env at {ENV_PREFIX}")
    print(f"  2. Create config files in {CONFIG_DIR}")
    print("  3. Install systemd timers + a failure-notifier (dead-man's-switch)")
    print("  4. Initialize the restic repository")
    print("\nYou will be prompted for sudo password.")
    print()

    if not prompt_yn("Continue?"):
        sys.exit(0)

    # Check dependencies
    check_dependencies()

    # Fix plocate for btrfs
    fix_plocate_config()

    # Gather configuration
    print("\n=== Configuration ===\n")

    hostname = os.uname().nodename
    machine_name = prompt("Machine name", hostname)

    print("\nStorage box (Hetzner or similar SFTP target):")
    storage_host = prompt("  Host", "uXXXXXX.your-storagebox.de")
    storage_user = prompt("  User", "uXXXXXX")
    storage_path = prompt("  Restic path", "/backups/restic")

    print("\nPaths to scan for .backup marker files:")
    scan_paths = prompt_list("Scan paths")
    if not scan_paths:
        scan_paths = [f"/home/{os.environ.get('USER', 'user')}"]
        print(f"  Using default: {scan_paths}")

    print("\nAdditional paths to always backup (no marker needed):")
    backup_paths = prompt_list("Extra backup paths")

    print("\nMariaDB databases to backup (leave empty if none):")
    databases = prompt_list("Databases")

    # Determine base_path_strip from scan_paths
    if scan_paths:
        # Use parent of first scan path
        first_scan = Path(scan_paths[0])
        base_path_strip = str(first_scan.parent) if first_scan.parent != first_scan else str(first_scan)
    else:
        base_path_strip = "/home"
    base_path_strip = prompt("Cold storage base path to strip", base_path_strip)

    print("\nUptime Kuma push URLs (leave empty to skip monitoring):")
    kuma_backup = prompt("  Backup success URL", "")
    kuma_verify = prompt("  Verify success URL", "")

    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"Machine name: {machine_name}")
    print(f"Storage: {storage_user}@{storage_host}:{storage_path}")
    print(f"Scan paths: {scan_paths}")
    print(f"Extra backup paths: {backup_paths}")
    print(f"Databases: {databases}")
    print(f"Kuma URLs configured: {bool(kuma_backup or kuma_verify)}")
    print()

    if not prompt_yn("Proceed with installation?"):
        sys.exit(0)

    # Do the installation
    create_conda_env()
    install_notifier()
    update_systemd_units()
    create_config(
        machine_name=machine_name,
        backup_paths=backup_paths,
        scan_paths=scan_paths,
        storage_host=storage_host,
        storage_user=storage_user,
        storage_path=storage_path,
        databases=databases,
        kuma_backup=kuma_backup,
        kuma_verify=kuma_verify,
        base_path_strip=base_path_strip,
    )
    install_systemd()
    run_init()

    print("\n" + "=" * 60)
    print("Setup complete!")
    print("=" * 60)
    print()
    print("Timers installed and running:")
    print("  - backup.timer        (hourly)")
    print("  - backup-verify.timer (nightly; reads 1/7 of the data, all of it every 7 days)")
    print()
    print("Next steps:")
    print("  1. Drop .backup files in folders you want backed up")
    print("  2. Run 'sudo backup scan' to verify")
    print("  3. Run 'sudo backup run --dry-run' to test")
    print("  4. Check 'systemctl list-timers backup*'")
    print()
    print("IMPORTANT - finish the dead-man's-switch in the Uptime Kuma UI:")
    print("  Set each push monitor's Heartbeat Interval so MISSING pings alert:")
    print("    backup       -> ~5400s (90 min; runs hourly)")
    print("    verify       -> ~108000s (30 h; runs nightly, up to ~2.5 h incl. lock wait)")
    print("  Without this, a silent failure (like the Jun 2026 outage) goes unnoticed.")
    print()


if __name__ == "__main__":
    main()
