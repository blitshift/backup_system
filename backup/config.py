"""Configuration loading and management."""

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


CONFIG_DIR = Path("/etc/backup")
COMMON_CONFIG = CONFIG_DIR / "config.toml"
MACHINE_CONFIG = CONFIG_DIR / "machine.toml"


@dataclass
class StorageBoxConfig:
    host: str
    user: str
    path: str
    ssh_key: Path | None = None


@dataclass
class ResticConfig:
    storage_box: StorageBoxConfig
    retention: dict = field(default_factory=lambda: {
        "hourly": 24,
        "daily": 7,
        "weekly": 4,
        "monthly": 12,
    })
    exclude: list[str] = field(default_factory=list)


@dataclass
class ColdStorageConfig:
    storage_box: StorageBoxConfig
    redundant_storage_box: StorageBoxConfig | None = None
    base_path_strip: str = "/home"


@dataclass
class KumaEndpoints:
    backup: str | None = None
    verify: str | None = None


@dataclass
class MachineConfig:
    name: str
    databases: list[str] = field(default_factory=list)
    extra_backup_paths: list[str] = field(default_factory=list)
    scan_paths: list[str] = field(default_factory=lambda: ["/home"])
    kuma: KumaEndpoints = field(default_factory=KumaEndpoints)


@dataclass
class Config:
    restic: ResticConfig
    cold_storage: ColdStorageConfig
    machine: MachineConfig


def _parse_storage_box(data: dict) -> StorageBoxConfig:
    return StorageBoxConfig(
        host=data["host"],
        user=data["user"],
        path=data["path"],
        ssh_key=Path(data["ssh_key"]) if data.get("ssh_key") else None,
    )


def load_config(
    common_path: Path = COMMON_CONFIG,
    machine_path: Path = MACHINE_CONFIG,
) -> Config:
    """Load configuration from common and machine-specific files."""
    with open(common_path, "rb") as f:
        common = tomllib.load(f)

    with open(machine_path, "rb") as f:
        machine = tomllib.load(f)

    # Parse restic config
    restic_data = common.get("restic", {})
    default_retention = {"hourly": 24, "daily": 7, "weekly": 4, "monthly": 12}
    restic = ResticConfig(
        storage_box=_parse_storage_box(restic_data["storage_box"]),
        retention=restic_data.get("retention", default_retention),
        exclude=restic_data.get("exclude", []),
    )

    # Parse cold storage config
    cold_data = common.get("cold_storage", {})
    redundant_box = None
    if "redundant_storage_box" in cold_data:
        redundant_box = _parse_storage_box(cold_data["redundant_storage_box"])

    cold_storage = ColdStorageConfig(
        storage_box=_parse_storage_box(cold_data["storage_box"]),
        redundant_storage_box=redundant_box,
        base_path_strip=cold_data.get("base_path_strip", "/home"),
    )

    # Parse machine config
    kuma_data = machine.get("kuma", {})
    kuma = KumaEndpoints(
        backup=kuma_data.get("backup"),
        verify=kuma_data.get("verify"),
    )
    # The weekly deep verify (and its own Kuma monitor) was retired: the nightly
    # verify now reads a 1/7 subset and pushes to `verify`. Deployed configs may
    # still carry the old key -- ignore it, just point it out.
    if "deep_verify" in kuma_data:
        print(f"Note: [kuma] deep_verify in {machine_path} is no longer used "
              f"(deep verify retired); remove it.", file=sys.stderr)

    machine_config = MachineConfig(
        name=machine["name"],
        databases=machine.get("databases", []),
        extra_backup_paths=machine.get("extra_backup_paths", []),
        scan_paths=machine.get("scan_paths", ["/home"]),
        kuma=kuma,
    )

    return Config(
        restic=restic,
        cold_storage=cold_storage,
        machine=machine_config,
    )
