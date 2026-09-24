# Checkpoint

## Current Task / Goal

Hardened backup system running across the fleet (**baloo**, **rafiki**, **crush**),
all to the same Hetzner storage box (user `u312848`) under per-machine repo paths.
Original failure mode: old setups (venv on baloo, hand-rolled scripts on
rafiki/crush) died silently — no Kuma ping — and went unnoticed for months.

**baloo is stable and about to get a system update + reboot** (2026-09-24). Backup
status was checked green beforehand; several improvements are deliberately
**deferred to after the reboot** (see Next Steps).

> Storage-box access: SSH/SFTP works on port 23 (official) and 22.
> - **baloo**: box shows the full tree on both ports; restic uses default port 22,
>   repo `sftp:u312848@…:/backups/restic/baloo`.
> - **rafiki**: port 22 is chrooted to `/home`; config `path=/restic` (repo at
>   `/home/restic/rafiki` on port 23). crush likely similar.

## What's Been Completed

### baloo
- Core hardening: conda deployment (Python 3.14, immune to system Python bumps),
  dead-man's-switch (OnFailure notifier), self-healing locks, cache-dir fix,
  scanner fixes, retention fix (stable DB dump path + `forget --group-by host,tags`),
  `--no-prune`, one-time backlog prune. Git `0e14907` / `f4d0f46`.
- [x] Exclude-ordering bug fixed + verified (config `exclude` moved under
  `[restic]`; `/home/olli/.cache` 31 GB now excluded). `fb51914` deployed.
- [x] **git-lfs storage mount (2026-09-09)**: Hetzner sub-account `u312848-sub1`
  (jailed to `/git-lfs`, key auth via root's key, SSH+external only) mounted at
  `/mnt/git-lfs` via SSHFS fstab **automount** (boot-persistent, `reconnect`,
  `nofail`, exposed to olli). Isolated from the backups. LFS data under
  `/mnt/git-lfs/objects/`. See memory `git-lfs-storage-box-mount`.
- [x] **DR access from Mac verified (2026-09-24)**: Merlin (`olli@Merlin.local`
  key already authorized) opened the repo and listed all 105 snapshots — key +
  repo passphrase (in the password manager) both confirmed.
- [x] **Pre-reboot status check (2026-09-24)**: all 3 timers active/enabled, last
  runs green, newest snapshot current, no locks, conda env intact.

### rafiki (earlier session)
- [x] Diagnosed backups dead since 2024-09 (~21 months); cleared the stale lock;
  deployed the new system (conda, notifier, 3 timers); reused existing repo
  `sftp:.../restic/rafiki` (185 GB); 6 MariaDB DBs; timer-run verified green;
  legacy timer disabled.

### Bugs fixed in the repo (commit `fb51914`, both surfaced on rafiki)
- [x] Excludes were inert (`exclude` under `[restic.retention]`).
- [x] Multi-DB retention dropped all but one DB (shared tag) → per-DB tag.

## Open Questions / Blockers

- **baloo backups are getting heavy**: each hourly run now takes **~27–30 min**
  (3–5 GB RAM). Driver: `/home/olli` grew **347 GiB (Jun) → ~1.0 TiB (Sep)** and
  the `hyperliquid` DB dump **26 → 43 GiB**. Snapshot count stable ~105 (retention
  works). Needs investigation: what grew, and whether hourly is still sensible.
- **Weekly deep-verify vs hourly backup lock contention**: the Monday
  `restic check --read-data` on the ~1 TiB repo runs ~5 h and holds a
  (non-exclusive) lock; the hourly backup's **prune step needs an exclusive lock**
  → fails `exit 11` for that window (~5 runs, seen 2026-09-21 02:32–06:34).
  *Data is still backed up* (file/DB use non-exclusive locks); only prune is
  skipped — but it false-pings Kuma down. Fix: make a prune-lock failure
  non-fatal / skip-on-contention.
- **~29 orphaned old-tag DB snapshots** (Feb–Apr, `/tmp/backup_db_*`, tag
  `database` only) are stuck permanently — the old tag group orphaned after the
  Jun switch to `database,hyperliquid`. Remove explicitly by id/path.
- **Broad SSH-key surface** on the storage-box main account (~19 keys incl. old
  Pis/desktops/VPS/mobile) — all can read AND delete the backups. Prune stale keys.
- **rafiki Kuma**: 3 push monitors not yet created; `[kuma]` URLs empty → DMS has
  nowhere to ping. Create on `baloo.vogel-haus.de`, set heartbeats, fill URLs.
- **`backup info` cosmetic bug**: "latest" not sorted by time (low priority).

## Next Steps

### Post-reboot on baloo (deferred by user, 2026-09-24)
1. Verify after reboot: `systemctl list-timers backup*` active, git-lfs automount
   returns on access to `/mnt/git-lfs`, first backup run green.
2. Investigate the ~28-min runtime / what drove `/home/olli` to ~1 TiB.
3. Prune-lock fix (don't fail the whole backup when only prune can't lock).
4. Clean up the ~29 orphaned old-tag DB snapshots.
5. Prune stale SSH keys on the storage box.
6. git-lfs **migration** itself (mount is done): lfs-folderstore per repo → move
   objects off GitHub.

### Fleet
7. rafiki: create + wire the 3 Kuma monitors; set heartbeat intervals.
8. crush: deploy the new system (read legacy script for repo path/password, list
   DBs, check influx/docker/secondary-repo).
