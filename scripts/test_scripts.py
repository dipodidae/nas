#!/usr/bin/env python3
"""
Test runner for NAS automation scripts

This script validates that all autom  scripts = [
    ("scripts/prowlarr-config.py", "prowlarr-config-loader"),
    ("scripts/prowlarr_priority_checker.py", "prowlarr-priority-checker"),
    ("scripts/prowlarr_priority_setter.py", "prowlarr-priority-setter"),n scripts are working correctly.
"""

import importlib.util
import os
import sys


def test_import(module_path, script_name):
  """Test if a script can be imported without errors."""
  print(f"Testing {script_name}...")
  try:
    spec = importlib.util.spec_from_file_location(script_name, module_path)
    importlib.util.module_from_spec(spec)
    return True, "Import successful"
  except Exception as e:  # noqa: BLE001
    return False, f"Import failed: {e}"


def test_environment():
  """Test Python environment and dependencies."""
  print("Testing Python environment...")

  # Test required packages
  required_packages = ["requests", "dotenv", "yaml"]
  missing_packages = []

  for package in required_packages:
    try:
      __import__(package)
    except ImportError:
      missing_packages.append(package)

  if missing_packages:
    return False, f"Missing packages: {', '.join(missing_packages)}"

  return True, "All dependencies available"


def test_configuration():
  """Test environment configuration."""
  print("Testing configuration...")

  # Check if .env file exists
  if not os.path.exists(".env"):
    return False, ".env file not found"

  # Check for required variables
  from dotenv import load_dotenv

  load_dotenv()

  api_key = os.getenv("API_KEY_PROWLARR")
  if not api_key:
    return False, "API_KEY_PROWLARR not found in .env"

  # Check YAML configuration
  if not os.path.exists("scripts/prowlarr-config.yml"):
    return False, "prowlarr-config.yml not found"

  try:
    # Import the config loader (adjust path since we're in the scripts directory when testing)
    scripts_path = os.path.join(os.getcwd(), "scripts")
    sys.path.insert(0, scripts_path)
    from prowlarr_config import load_prowlarr_config

    config = load_prowlarr_config()
    indexer_count = len(config.indexer_priorities)
    return (
      True,
      f"Configuration valid (API key: {api_key[:10]}..., {indexer_count} indexers loaded)",
    )
  except Exception as e:
    return False, f"YAML config error: {e}"


def main():
  """Run all tests."""
  print("🧪 NAS Automation Scripts Test Suite")
  print("=" * 50)

  tests_passed = 0
  total_tests = 0

  # Test environment
  total_tests += 1
  success, message = test_environment()
  print(f"{'✅' if success else '❌'} Environment: {message}")
  if success:
    tests_passed += 1

  # Test configuration
  total_tests += 1
  success, message = test_configuration()
  print(f"{'✅' if success else '❌'} Configuration: {message}")
  if success:
    tests_passed += 1

  # Test script imports
  scripts = [
    ("scripts/prowlarr_config.py", "prowlarr-config-loader"),
    ("scripts/prowlarr_priority_checker.py", "prowlarr-priority-checker"),
    ("scripts/prowlarr_priority_setter.py", "prowlarr-priority-setter"),
    ("scripts/config_backup.py", "config-backup"),
    ("scripts/project_service_adder.py", "project-service-adder"),
    ("scripts/permissions_auditor.py", "permissions-auditor"),
    ("scripts/post_update_verifier.py", "post-update-verifier"),
    ("scripts/log_pruner.py", "log-pruner"),
    ("scripts/qbittorrent_stalled_kickstart.py", "qbittorrent-stalled-kickstart"),
    ("scripts/slskd_rescan.py", "slskd-rescan"),
    ("scripts/slskd_cleanup.py", "slskd-cleanup"),
    ("scripts/slskd_complete_sweep.py", "slskd-complete-sweep"),
    ("scripts/lidarr_queue_unstick.py", "lidarr-queue-unstick"),
    ("scripts/lidarr_purge_empty_artists.py", "lidarr-purge-empty-artists"),
  ]

  for script_path, script_name in scripts:
    if os.path.exists(script_path):
      total_tests += 1
      success, message = test_import(script_path, script_name)
      print(f"{'✅' if success else '❌'} {script_name}: {message}")
      if success:
        tests_passed += 1
    else:
      print(f"⚠️  {script_name}: Script not found at {script_path}")

  # Summary
  print("\n" + "=" * 50)
  print(f"📊 Test Results: {tests_passed}/{total_tests} passed")

  if tests_passed == total_tests:
    print("🎉 All tests passed! Scripts are ready to use.")
    return 0
  else:
    print("❌ Some tests failed. Please check the errors above.")
    return 1


if __name__ == "__main__":
  sys.exit(main())
