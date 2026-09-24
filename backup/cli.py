"""Command-line interface for the backup system."""

import shutil
import sys
import tempfile
from pathlib import Path

import click

from . import __version__
from .config import load_config, COMMON_CONFIG, MACHINE_CONFIG, CONFIG_DIR
from .scanner import scan_markers, print_scan_result, get_effective_backup_paths
from .secrets import get_restic_password, set_restic_password, get_mariadb_password, set_mariadb_password
from .restic import run_backup, run_forget_and_prune, run_check, daily_read_data_subset, init_repo, check_repo_exists, backup_database_dump, get_repo_info, unlock_stale, ResticError
from .mariadb import dump_all_databases, MariaDBError
from .kuma import push_backup_success, push_backup_failure, push_verify_success, push_verify_failure
from .cold import upload_to_cold_storage, get_cold_storage_status, verify_cold_storage, ColdStorageError


def _require_config(ctx):
    """Get config or raise error."""
    config = ctx.obj.get("config")
    if not config:
        raise click.ClickException("Config not found. Run 'backup init' first.")
    return config


def _get_password(machine_name: str) -> str:
    """Get restic password or raise error."""
    password = get_restic_password(machine_name)
    if not password:
        raise click.ClickException(
            f"Restic password not found for {machine_name}. Run 'backup init' first."
        )
    return password


@click.group()
@click.version_option(version=__version__)
@click.option(
    "--config",
    "common_config",
    type=click.Path(path_type=Path),
    default=COMMON_CONFIG,
    help="Path to common config file",
)
@click.option(
    "--machine-config",
    type=click.Path(path_type=Path),
    default=MACHINE_CONFIG,
    help="Path to machine-specific config file",
)
@click.pass_context
def cli(ctx, common_config: Path, machine_config: Path):
    """Backup system for Linux machines.

    Supports restic backups, MariaDB dumps, and cold storage with verification.
    """
    ctx.ensure_object(dict)
    ctx.obj["common_config"] = common_config
    ctx.obj["machine_config"] = machine_config
    try:
        ctx.obj["config"] = load_config(common_config, machine_config)
    except FileNotFoundError:
        ctx.obj["config"] = None


@cli.command()
@click.pass_context
def init(ctx):
    """Initialize the backup system on this machine.

    Creates restic repository, sets up keyring entries, and validates config.
    """
    common_config = ctx.obj["common_config"]
    machine_config = ctx.obj["machine_config"]

    # Check if config files exist
    if not common_config.exists():
        raise click.ClickException(
            f"Common config not found at {common_config}\n"
            f"Copy config/config.example.toml to {common_config} and edit it."
        )
    if not machine_config.exists():
        raise click.ClickException(
            f"Machine config not found at {machine_config}\n"
            f"Copy config/machine.example.toml to {machine_config} and edit it."
        )

    config = load_config(common_config, machine_config)
    click.echo(f"Initializing backup system for {config.machine.name}...")

    # Create state directory
    state_dir = Path("/var/lib/backup")
    if not state_dir.exists():
        click.echo(f"Creating state directory {state_dir}...")
        state_dir.mkdir(parents=True, mode=0o700)

    # Set up restic password
    existing_password = get_restic_password(config.machine.name)
    if existing_password:
        click.echo("Restic password already set in keyring.")
        password = existing_password
    else:
        password = click.prompt("Enter restic repository password", hide_input=True)
        password_confirm = click.prompt("Confirm password", hide_input=True)
        if password != password_confirm:
            raise click.ClickException("Passwords do not match")
        set_restic_password(config.machine.name, password)
        click.echo("Restic password saved to keyring.")

    # Initialize restic repository if needed
    if check_repo_exists(config, password):
        click.echo("Restic repository already exists.")
    else:
        click.echo("Initializing restic repository...")
        try:
            init_repo(config, password)
            click.echo("Restic repository initialized.")
        except ResticError as e:
            raise click.ClickException(f"Failed to initialize restic: {e}")

    # Set up MariaDB password if databases are configured
    if config.machine.databases:
        existing_db_password = get_mariadb_password()
        if existing_db_password:
            click.echo("MariaDB password already set in keyring.")
        else:
            if click.confirm("Set up MariaDB backup password?"):
                db_password = click.prompt("Enter MariaDB backup user password", hide_input=True)
                set_mariadb_password(db_password)
                click.echo("MariaDB password saved to keyring.")

    click.echo("\nInitialization complete!")
    click.echo(f"Run 'backup scan' to see what will be backed up.")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be done without doing it")
@click.option("--no-prune", is_flag=True, help="Skip the retention forget+prune step")
@click.pass_context
def run(ctx, dry_run: bool, no_prune: bool):
    """Run backup (restic + databases)."""
    config = _require_config(ctx)
    password = _get_password(config.machine.name)

    click.echo(f"Running backup for {config.machine.name}...")

    try:
        # Clear stale locks from any previously interrupted run, so a single
        # hard-killed backup can't block this one (and every future one).
        unlock_stale(config, password)

        # Scan for marker files
        click.echo("Scanning for backup markers...")
        scan_result = scan_markers(config.machine.scan_paths, update_db=True)
        paths = get_effective_backup_paths(scan_result, config.machine.extra_backup_paths)

        if not paths and not config.machine.databases:
            click.echo("Nothing to backup!")
            return

        # Backup files
        if paths:
            click.echo(f"Backing up {len(paths)} paths...")
            for p in paths:
                click.echo(f"  {p}")
            run_backup(config, paths, password, dry_run=dry_run,
                       excludes=scan_result.nobackup_paths)

        # Backup databases
        if config.machine.databases:
            click.echo(f"Backing up {len(config.machine.databases)} databases...")
            db_password = get_mariadb_password()

            temp_dir = None
            try:
                dump_files = dump_all_databases(config.machine.databases, db_password)
                if dump_files:
                    temp_dir = dump_files[0].parent
                    for dump_file in dump_files:
                        click.echo(f"  {dump_file.name}")
                        if not dry_run:
                            backup_database_dump(config, dump_file, password)
            finally:
                if temp_dir and temp_dir.exists():
                    shutil.rmtree(temp_dir)

        # Apply retention policy
        if not dry_run and not no_prune:
            click.echo("Applying retention policy...")
            run_forget_and_prune(config, password)

        click.echo("Backup complete!")
        push_backup_success(config.machine.kuma.backup)

    except (ResticError, MariaDBError) as e:
        click.echo(f"Backup failed: {e}", err=True)
        push_backup_failure(config.machine.kuma.backup, str(e))
        sys.exit(1)


@cli.command()
@click.option("--deep", is_flag=True,
              help="Read ALL data instead of today's 1/7 subset (manual; takes hours "
                   "on a large repo and holds an exclusive lock that blocks backups)")
@click.pass_context
def verify(ctx, deep: bool):
    """Verify backup integrity.

    Default (nightly timer): structural check plus reading today's 1/7 of the
    data (group = ISO weekday), so all data is read once every 7 days.
    """
    config = _require_config(ctx)
    password = _get_password(config.machine.name)

    subset = None if deep else daily_read_data_subset()
    mode = "deep (all data)" if deep else f"read-data subset {subset}"
    click.echo(f"Running {mode} verification for {config.machine.name}...")

    try:
        # Clear stale locks left by a previously interrupted run before checking.
        unlock_stale(config, password)

        run_check(config, password, read_data=deep, read_data_subset=subset)
        click.echo("Verification passed!")
        push_verify_success(config.machine.kuma.verify, f"Verification passed ({mode})")

    except ResticError as e:
        click.echo(f"Verification failed: {e}", err=True)
        push_verify_failure(config.machine.kuma.verify, f"{mode}: {e}")
        sys.exit(1)


@cli.command()
@click.option("--redundant", is_flag=True, help="Also upload to redundant storage box")
@click.pass_context
def cold(ctx, redundant: bool):
    """Run cold storage backup."""
    config = _require_config(ctx)

    click.echo(f"Running cold storage backup for {config.machine.name}...")

    # Scan for cold storage markers
    scan_result = scan_markers(config.machine.scan_paths, update_db=True)

    paths = scan_result.cold_storage_paths.copy()
    if redundant:
        paths.extend(scan_result.cold_storage_redundant_paths)

    if not paths:
        click.echo("No cold storage paths found.")
        return

    click.echo(f"Found {len(paths)} paths to backup:")
    for p in paths:
        marker = "redundant" if p in scan_result.cold_storage_redundant_paths else "standard"
        click.echo(f"  {p} ({marker})")

    try:
        for path in paths:
            is_redundant = path in scan_result.cold_storage_redundant_paths
            uploaded = upload_to_cold_storage(path, config, redundant=is_redundant)
            click.echo(f"  Uploaded {len(uploaded)} files from {path}")

        click.echo("Cold storage backup complete!")

    except ColdStorageError as e:
        click.echo(f"Cold storage backup failed: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option("--no-update", is_flag=True, help="Skip updatedb (use cached plocate data)")
@click.pass_context
def scan(ctx, no_update: bool):
    """Scan for marker files and show what would be backed up."""
    config = _require_config(ctx)

    click.echo(f"Scanning for backup markers in {config.machine.scan_paths}...")
    if not no_update:
        click.echo("Updating locate database...")

    result = scan_markers(config.machine.scan_paths, update_db=not no_update)
    print_scan_result(result)

    # Show effective backup paths including extras
    effective = get_effective_backup_paths(result, config.machine.extra_backup_paths)
    click.echo("\n=== Effective Restic Backup Paths ===")
    for p in effective:
        marker = "(marker)" if p in result.backup_paths else "(config)"
        click.echo(f"  {p} {marker}")

    if config.machine.databases:
        click.echo("\n=== Databases to Backup ===")
        for db in config.machine.databases:
            click.echo(f"  {db}")


@cli.command()
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.pass_context
def status(ctx, path: Path):
    """Check cold storage status for a path."""
    config = _require_config(ctx)

    path = path.resolve()
    click.echo(f"Checking cold storage status for {path}...")

    result = get_cold_storage_status(path, config)

    if not result:
        click.echo("Not backed up to cold storage.")
        return

    if result.get("type") == "directory":
        click.echo(f"Directory backed up: {result['files']} files")
        click.echo(f"Total size: {result['total_size'] / 1024 / 1024:.2f} MB")
    else:
        click.echo(f"File backed up:")
        click.echo(f"  SHA256: {result['sha256']}")
        click.echo(f"  Size: {result['size']} bytes")
        click.echo(f"  Backed up: {result['backed_up']}")


@cli.command("verify-cold")
@click.pass_context
def verify_cold(ctx):
    """Verify cold storage checksums against local files."""
    config = _require_config(ctx)

    click.echo(f"Verifying cold storage checksums for {config.machine.name}...")

    passed, failed = verify_cold_storage(config)

    click.echo(f"\nPassed: {len(passed)}")
    click.echo(f"Failed: {len(failed)}")

    if failed:
        click.echo("\nFailed files:")
        for f in failed:
            click.echo(f"  {f}")
        sys.exit(1)


@cli.command()
@click.pass_context
def info(ctx):
    """Show current backup status and repository info."""
    config = _require_config(ctx)
    password = _get_password(config.machine.name)

    click.echo(f"Backup info for {config.machine.name}")
    click.echo("=" * 50)

    try:
        repo_info = get_repo_info(config, password)

        click.echo(f"\nRepository: {repo_info['repository']}")

        # Stats
        if repo_info.get("stats"):
            stats = repo_info["stats"]
            total_size = stats.get("total_size", 0)
            total_files = stats.get("total_file_count", 0)
            size_mb = total_size / 1024 / 1024
            size_gb = size_mb / 1024
            if size_gb >= 1:
                click.echo(f"Total size: {size_gb:.2f} GB ({total_files} files)")
            else:
                click.echo(f"Total size: {size_mb:.2f} MB ({total_files} files)")

        # Snapshots summary
        snapshots = repo_info.get("snapshots", [])
        click.echo(f"Snapshots: {len(snapshots)}")

        # Latest snapshot
        latest = repo_info.get("latest")
        if latest:
            click.echo(f"\n--- Latest Backup ---")
            click.echo(f"Time: {latest.get('time', 'unknown')}")
            click.echo(f"Hostname: {latest.get('hostname', 'unknown')}")

            paths = latest.get("paths", [])
            if paths:
                click.echo(f"Paths ({len(paths)}):")
                for p in paths:
                    click.echo(f"  {p}")

            tags = latest.get("tags", [])
            if tags:
                click.echo(f"Tags: {', '.join(tags)}")

            short_id = latest.get("short_id", latest.get("id", "")[:8])
            click.echo(f"Snapshot ID: {short_id}")
        else:
            click.echo("\nNo backups yet.")

        # Show configured paths for reference
        click.echo(f"\n--- Configured ---")
        click.echo(f"Scan paths: {', '.join(config.machine.scan_paths)}")
        if config.machine.extra_backup_paths:
            click.echo(f"Extra paths: {', '.join(config.machine.extra_backup_paths)}")
        if config.machine.databases:
            click.echo(f"Databases: {', '.join(config.machine.databases)}")

    except ResticError as e:
        click.echo(f"Failed to get repo info: {e}", err=True)
        sys.exit(1)


def main():
    cli()


if __name__ == "__main__":
    main()
