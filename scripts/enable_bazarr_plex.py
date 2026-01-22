#!/usr/bin/env python3
"""
Enable Plex integration in Bazarr configuration.

This script updates Bazarr's config.yaml to enable Plex integration,
allowing Bazarr to scan Plex library and download subtitles.
"""

import os
import sys
from pathlib import Path

import yaml


def update_bazarr_config(config_path: Path, plex_token: str, plex_url: str = "http://plex:32400") -> None:
    """
    Update Bazarr configuration to enable Plex integration.

    Args:
        config_path: Path to Bazarr's config.yaml
        plex_token: Plex authentication token
        plex_url: Plex server URL (default: http://plex:32400)
    """
    if not config_path.exists():
        print(f"Error: Config file not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    # Read existing config
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Update general settings to enable Plex
    if "general" not in config:
        config["general"] = {}
    config["general"]["use_plex"] = True

    # Update Plex settings
    if "plex" not in config:
        config["plex"] = {}

    config["plex"]["apikey"] = plex_token
    config["plex"]["auth_method"] = "apikey"
    config["plex"]["ip"] = plex_url.replace("http://", "").replace("https://", "").split(":")[0]
    config["plex"]["port"] = 32400
    config["plex"]["ssl"] = False
    config["plex"]["base_url"] = ""

    # Backup original config
    backup_path = config_path.with_suffix(".yaml.bak")
    print(f"Creating backup at {backup_path}")
    with open(backup_path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    # Write updated config
    print(f"Updating {config_path}")
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    print("✓ Plex integration enabled in Bazarr")
    print(f"  - Plex URL: {plex_url}")
    print(f"  - Token configured: {'*' * 10}")
    print("\nNext steps:")
    print("  1. Restart Bazarr: docker compose restart bazarr")
    print("  2. Access Bazarr at http://localhost:6767")
    print("  3. Verify Plex connection in Settings → Plex")


def main() -> None:
    """Main entry point."""
    # Get Plex token from environment
    plex_token = os.getenv("PLEX_TOKEN")
    if not plex_token:
        print("Error: PLEX_TOKEN environment variable not set", file=sys.stderr)
        print("Please add PLEX_TOKEN to your .env file", file=sys.stderr)
        sys.exit(1)

    # Get config directory
    config_dir = os.getenv("CONFIG_DIRECTORY", "/mnt/drive/.docker-config")
    config_path = Path(config_dir) / "bazarr" / "config" / "config.yaml"

    print("Bazarr Plex Integration Setup")
    print("=" * 50)
    print(f"Config directory: {config_dir}")
    print(f"Config file: {config_path}")
    print()

    update_bazarr_config(config_path, plex_token)


if __name__ == "__main__":
    main()
