#!/usr/bin/env python3
"""
Configuration loader for Prowlarr scripts.

This module provides utilities to load indexer priorities and settings
from the prowlarr-config.yml file.
"""

import sys
from pathlib import Path
from typing import Any

try:
  import yaml
except ImportError:
  print("❌ Error: PyYAML is required. Install with: pip install PyYAML")
  sys.exit(1)


class ProwlarrConfig:
  """Loads and provides access to Prowlarr configuration."""

  def __init__(self, config_path: str | None = None):
    """Initialize with optional custom config path."""
    if config_path is None:
      # Default to prowlarr-config.yml in the same directory as this script
      script_dir = Path(__file__).parent
      config_path = script_dir / "prowlarr-config.yml"

    self.config_path = Path(config_path)
    self._config_data = self._load_config()

  def _load_config(self) -> dict[str, Any]:
    """Load configuration from YAML file."""
    if not self.config_path.exists():
      raise FileNotFoundError(
        f"Configuration file not found: {self.config_path}\n"
        f"Please ensure prowlarr-config.yml exists in the scripts directory.",
      )

    try:
      with open(self.config_path, encoding="utf-8") as file:
        config = yaml.safe_load(file)

      if not config:
        raise ValueError("Configuration file is empty")

      if "indexer_priorities" not in config:
        raise ValueError("Configuration file missing 'indexer_priorities' section")

      return config

    except yaml.YAMLError as e:  # noqa: PERF203
      raise ValueError(f"Invalid YAML in configuration file: {e}") from e
    except Exception as e:  # noqa: BLE001
      raise ValueError(f"Error reading configuration file: {e}") from e

  @property
  def indexer_priorities(self) -> dict[str, int]:
    """Get the indexer priorities mapping."""
    return self._config_data.get("indexer_priorities", {})

  @property
  def fuzzy_match_threshold(self) -> float:
    """Get the fuzzy match threshold setting."""
    return self._config_data.get("settings", {}).get("fuzzy_match_threshold", 0.8)

  @property
  def strict_fuzzy_threshold(self) -> float:
    """Get the strict fuzzy match threshold setting."""
    return self._config_data.get("settings", {}).get("strict_fuzzy_threshold", 0.9)

  def get_priority(self, indexer_name: str) -> int | None:
    """Get priority for a specific indexer name."""
    return self.indexer_priorities.get(indexer_name)

  def list_indexers_by_priority(self) -> dict[int, list]:
    """Get indexers grouped by priority level."""
    priority_groups = {}
    for indexer, priority in self.indexer_priorities.items():
      if priority not in priority_groups:
        priority_groups[priority] = []
      priority_groups[priority].append(indexer)
    return priority_groups

  def validate_config(self) -> None:
    """Validate the configuration file."""
    errors = []

    # Check indexer_priorities
    priorities = self.indexer_priorities
    if not priorities:
      errors.append("No indexer priorities defined")

    # Validate priority values
    for indexer, priority in priorities.items():
      if not isinstance(priority, int):
        errors.append(f"Priority for '{indexer}' must be an integer, got {type(priority).__name__}")
      elif not (1 <= priority <= 50):
        errors.append(f"Priority for '{indexer}' must be between 1-50, got {priority}")

    # Check settings
    settings = self._config_data.get("settings", {})
    fuzzy_threshold = settings.get("fuzzy_match_threshold", 0.8)
    strict_threshold = settings.get("strict_fuzzy_threshold", 0.9)

    if not isinstance(fuzzy_threshold, (int, float)) or not (0.0 <= fuzzy_threshold <= 1.0):
      errors.append(f"fuzzy_match_threshold must be between 0.0-1.0, got {fuzzy_threshold}")

    if not isinstance(strict_threshold, (int, float)) or not (0.0 <= strict_threshold <= 1.0):
      errors.append(f"strict_fuzzy_threshold must be between 0.0-1.0, got {strict_threshold}")

    if strict_threshold < fuzzy_threshold:
      errors.append("strict_fuzzy_threshold should be >= fuzzy_match_threshold")

    if errors:
      raise ValueError(
        "Configuration validation failed:\n" + "\n".join(f"  - {error}" for error in errors)
      )

  def display_summary(self) -> None:
    """Display a summary of the loaded configuration."""
    print(f"📄 Configuration loaded from: {self.config_path}")
    print(f"📊 Total indexers configured: {len(self.indexer_priorities)}")
    print(f"🎯 Fuzzy match threshold: {self.fuzzy_match_threshold}")
    print(f"🎯 Strict fuzzy threshold: {self.strict_fuzzy_threshold}")

    # Show priority distribution
    priority_groups = self.list_indexers_by_priority()
    priority_ranges = {
      "High Priority (1-10)": [p for p in priority_groups if 1 <= p <= 10],
      "Mid Priority (11-30)": [p for p in priority_groups if 11 <= p <= 30],
      "Standard Priority (31-40)": [p for p in priority_groups if 31 <= p <= 40],
      "Low Priority (41-50)": [p for p in priority_groups if 41 <= p <= 50],
    }

    print("\n📈 Priority Distribution:")
    for range_name, priorities in priority_ranges.items():
      if priorities:
        count = sum(len(priority_groups[p]) for p in priorities)
        print(f"  {range_name}: {count} indexers")


def load_prowlarr_config(config_path: str | None = None) -> ProwlarrConfig:
  """Convenience function to load and validate Prowlarr configuration."""
  try:
    config = ProwlarrConfig(config_path)
    config.validate_config()
    return config
  except Exception as e:
    print(f"❌ Configuration Error: {e}")
    sys.exit(1)


if __name__ == "__main__":
  """Test the configuration loader."""
  try:
    config = load_prowlarr_config()
    config.display_summary()
    print("\n✅ Configuration loaded and validated successfully!")
  except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)
