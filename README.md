# Backup System

A backup system for Linux machines with Restic, MariaDB dumps, and cold storage support.

## Features

- **Restic backups** to Hetzner storage box (or any SFTP target)
- **Marker file scanning** - drop `.backup` files in folders to include them
- **MariaDB database dumps** - configurable per machine
- **Cold storage** - copy files with SHA256 checksum verification
- **Uptime Kuma integration** - push notifications for monitoring
- **Secure password storage** - uses `keyrings.cryptfile`
- **systemd timers** - hourly backups, nightly verification (1/7 of the data per night)

## Quick Start

### 1. Install system dependencies

```bash
# Arch Linux
sudo pacman -S restic plocate python

# Enable plocate (for marker file scanning)
sudo mkdir -p /etc/systemd/system/timers.target.wants
sudo ln -s /usr/lib/systemd/system/plocate-updatedb.timer /etc/systemd/system/timers.target.wants/
sudo systemctl daemon-reload
sudo systemctl start plocate-updatedb.timer
sudo updatedb
```

### 2. Set up SSH access for root

Backups run as root, so root needs SSH access to the storage box.

```bash
# Generate SSH key for root (if not exists)
sudo ls /root/.ssh/id_ed25519.pub || sudo ssh-keygen -t ed25519

# Show the public key
sudo cat /root/.ssh/id_ed25519.pub
```

Add the public key to your storage box (e.g., via Hetzner Robot panel).

```bash
# Test connection (Hetzner uses port 23 for SFTP)
sudo ssh -p 23 uXXXXXX@uXXXXXX.your-storagebox.de ls
```

### 3. Run the setup script

```bash
./install.py
```

The interactive setup will:
- Set up a root-owned conda env at `/opt/miniforge/envs/backup` (installs Miniforge
  to `/opt/miniforge` if needed) and install the package into it
- Prompt for storage box details
- Ask which paths to scan for `.backup` markers
- Ask for Uptime Kuma push URLs (optional)
- Create config files in `/etc/backup/`
- Install and enable systemd timers
- Install a stdlib-only failure notifier as a dead-man's-switch (see below)
- Initialize the restic repository (prompts for password)

> **Why conda, not a venv?** A venv borrows the system Python via a symlink.
> On a rolling distro, a Python minor-version bump (e.g. Arch 3.13 → 3.14)
> invalidates the venv and every backup run dies with `ModuleNotFoundError`.
> A conda env ships its own interpreter, so a system Python upgrade can't break it.

### Dead-man's-switch (important)

A crash at *import time* (exactly what the Python upgrade caused) means the
backup script never runs and never pushes any status to Kuma — so the failure is
**silent**. Two layers guard against this:

1. **`OnFailure=` notifier.** Each backup unit triggers `backup-onfailure@.service`
   on failure, which runs `/opt/backup-notify-failure.py` with the **system**
   Python (stdlib only, independent of the conda env) and pushes a `down` status
   to the matching Kuma monitor.
2. **Kuma heartbeat interval.** Set each push monitor's *Heartbeat Interval* in
   the Kuma UI so that **missing** pings alert on their own (covers a hung run,
   a disabled timer, or a powered-off machine):
   - `backup` → ~5400 s (90 min; runs hourly)
   - `verify` → ~108000 s (30 h; runs nightly, up to ~2.5 h incl. lock wait)

### 4. Mark folders for backup

```bash
# Include a folder in backups
touch ~/documents/.backup
touch ~/projects/.backup

# Exclude a subfolder
touch ~/projects/temp/.nobackup

# Verify what will be backed up
sudo backup scan
```

## Marker Files

Drop these empty files in directories to control backup behavior:

| File | Effect |
|------|--------|
| `.backup` | Include this directory in restic backups |
| `.nobackup` | Exclude this directory (overrides parent `.backup`) |
| `.coldstorage` | Include in cold storage backup |
| `.coldstorage_redundant` | Include in cold storage + redundant copy |

The system uses `plocate` to efficiently find these markers before each backup.

## Commands

All commands require root (backups run as root):

```bash
# Show what would be backed up
sudo backup scan

# Show current backup status and repo info
sudo backup info

# Run backup manually
sudo backup run
sudo backup run --dry-run

# Verify backup integrity
sudo backup verify          # Structure + today's 1/7 of the data (nightly timer)
sudo backup verify --deep   # Structure + ALL data (manual only, takes hours)

# Cold storage
sudo backup cold                    # Backup cold storage paths
sudo backup cold --redundant        # Include redundant paths
sudo backup status /path/to/folder  # Check if path is backed up
sudo backup verify-cold             # Verify checksums
```

## Scheduled Backups

The setup script installs two systemd timers:

| Timer | Schedule | What it does |
|-------|----------|--------------|
| `backup.timer` | **Hourly** | Runs `backup run` |
| `backup-verify.timer` | **Nightly** | Runs `backup verify` |

The nightly `backup verify` runs `restic check --read-data-subset=N/7`, where
N is the ISO weekday (Mon=1 … Sun=7): the structural check plus reading 1/7 of
the pack files, so every pack is read once a week. This replaces the former
weekly `--deep` timer, whose ~6 h full read blocked the hourly backups.

`restic check` holds an **exclusive** repo lock while it runs. Every restic call
waits up to 90 min for a lock (`--retry-lock`), so a backup that collides with
the nightly check (~1 h) is delayed, not failed. A manual `verify --deep` holds
the lock for hours; backups in that window fail after the 90 min wait.

Check timer status:
```bash
systemctl list-timers 'backup*'
journalctl -u backup.service -f  # Watch backup logs
```

## Uptime Kuma Integration

Create two push monitors in Uptime Kuma with different heartbeat expectations:

| Monitor | Expected Heartbeat |
|---------|-------------------|
| `backup-{machine}` | Every 1-2 hours |
| `verify-{machine}` | Every 1-2 days (nightly check; a manual `--deep` run pushes here too) |

If any check fails, it pushes a DOWN status. If a timer doesn't fire, Kuma will alert on missing heartbeat.

## Configuration Files

After setup, config lives in `/etc/backup/`:

### `config.toml` (shared settings)

```toml
[restic]
[restic.retention]
hourly = 24
daily = 7
weekly = 4
monthly = 12

exclude = [
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".git",
    ".cache",
]

[restic.storage_box]
host = "uXXXXXX.your-storagebox.de"
user = "uXXXXXX"
path = "/backups/restic"

[cold_storage]
base_path_strip = "/home/olli"

[cold_storage.storage_box]
host = "uXXXXXX.your-storagebox.de"
user = "uXXXXXX"
path = "/cold"
```

### `machine.toml` (per-machine settings)

```toml
name = "baloo"

scan_paths = ["/home/olli", "/data"]
extra_backup_paths = ["/etc"]
databases = ["wordpress", "nextcloud"]

[kuma]
backup = "https://kuma.example.com/api/push/xxxxx"
verify = "https://kuma.example.com/api/push/yyyyy"
```

A leftover `deep_verify` key from older installs is ignored (with a note on
stderr); remove it and delete its Kuma monitor.

## Restoring

Use restic directly for restores:

```bash
# Set up environment
export RESTIC_REPOSITORY="sftp:uXXXXXX@storagebox.de:/backups/restic/machinename"
export RESTIC_PASSWORD="your-password"

# List snapshots
restic snapshots

# Browse a snapshot
restic ls latest

# Restore specific files
restic restore latest --target /tmp/restore --include /path/to/file

# Restore everything
restic restore latest --target /tmp/restore
```

## Manual Installation

If you prefer not to use the setup script:

```bash
# Install Miniforge to /opt (once), create the env, install the package
sudo bash Miniforge3-Linux-x86_64.sh -b -p /opt/miniforge
sudo /opt/miniforge/bin/conda create -y -p /opt/miniforge/envs/backup python=3.14
sudo /opt/miniforge/envs/backup/bin/pip install /path/to/backup_system   # non-editable
sudo ln -sf /opt/miniforge/envs/backup/bin/backup /usr/local/bin/backup

# Install the dead-man's-switch notifier (runs under the SYSTEM python)
sudo cp systemd/notify-failure.py /opt/backup-notify-failure.py
sudo chmod 755 /opt/backup-notify-failure.py

# Create configs manually
sudo mkdir -p /etc/backup
sudo cp config/config.example.toml /etc/backup/config.toml
sudo cp config/machine.example.toml /etc/backup/machine.toml
# Edit both files...

# Initialize
sudo backup init

# Install systemd timers
sudo systemd/install.sh
```

## Directory Structure

```
/opt/miniforge/                    # Root-owned conda (Miniforge)
└── envs/backup/                   # Backup env (own Python interpreter)
/opt/backup-notify-failure.py      # Dead-man's-switch notifier (system python)
/usr/local/bin/backup              # Symlink -> conda env's backup binary
/etc/backup/
├── config.toml                    # Shared config
├── machine.toml                   # Machine-specific config
└── secrets/                       # Password files (mode 600, root-only dir)
/var/lib/backup/
└── {machine}_cold_checksums.json  # Cold storage tracking
```

## License

MIT
