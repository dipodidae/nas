#!/usr/bin/env python3
"""
Prowlarr Priority Checker

This script analyzes indexer priorities in Prowlarr and identifies what needs
to be updated based on the configuration in prowlarr-config.yml.
Shows manual update instructions as a workaround for API issues.

Requirements:
- Prowlarr running and accessible
- .env file with API_KEY_PROWLARR set
- prowlarr-config.yml file with indexer priorities
- requests library (pip install requests)
- python-dotenv library (pip install python-dotenv)
- PyYAML library (pip install PyYAML)

Usage:
    python prowlarr-priority-checker.py
"""

import os
import sys

import requests

try:
  from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover

  def load_dotenv():  # fallback noop
    return False


import importlib.util
from difflib import SequenceMatcher

# Import from kebab-case module
spec = importlib.util.spec_from_file_location("prowlarr_config", "scripts/prowlarr-config.py")
prowlarr_config_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prowlarr_config_module)
load_prowlarr_config = prowlarr_config_module.load_prowlarr_config

# Load environment variables (best-effort)
load_dotenv()

# Load configuration from YAML file
config = load_prowlarr_config()
INDEXER_PRIORITIES = config.indexer_priorities
FUZZY_MATCH_THRESHOLD = config.fuzzy_match_threshold


def find_best_match(query, choices, threshold=0.8):
  """Find the best fuzzy match for a query in a list of choices."""
  best_match = None
  best_ratio = 0

  for choice in choices:
    ratio = SequenceMatcher(None, query.lower(), choice.lower()).ratio()
    if ratio > best_ratio and ratio >= threshold:
      best_ratio = ratio
      best_match = choice

  return best_match, best_ratio


def main():
  api_key = os.getenv("API_KEY_PROWLARR")
  if not api_key:
    print("❌ Error: API_KEY_PROWLARR not found in .env file")
    sys.exit(1)

  host = os.getenv("PROWLARR_HOST", "http://localhost")
  port = int(os.getenv("PROWLARR_PORT", "9696"))

  print("🔧 Prowlarr Configuration:")
  print(f"   Host: {host}")
  print(f"   Port: {port}")
  print(f"   API Key: {api_key[:10]}...{api_key[-4:]}")
  print(f"   Config File: {len(INDEXER_PRIORITIES)} indexers loaded")
  print(f"   Fuzzy Threshold: {FUZZY_MATCH_THRESHOLD}")

  # Get indexers
  try:
    url = f"{host}:{port}/api/v1/indexer"
    response = requests.get(url, headers={"X-Api-Key": api_key}, timeout=10)

    if response.status_code != 200:
      print(f"❌ Failed to connect to Prowlarr API: {response.status_code}")
      sys.exit(1)

    indexers = response.json()
    print(f"✅ Successfully retrieved {len(indexers)} indexers")

  except Exception as e:
    print(f"❌ Error connecting to Prowlarr: {e}")
    sys.exit(1)

  # Analyze what needs to be updated
  updates_needed = []
  already_correct = []
  not_in_list = []

  available_priority_names = list(INDEXER_PRIORITIES.keys())

  for indexer in indexers:
    name = indexer["name"]
    current_priority = indexer.get("priority", "N/A")
    indexer_id = indexer["id"]

    # Try to find a match
    matched_name, match_ratio = find_best_match(name, available_priority_names)

    if matched_name:
      new_priority = INDEXER_PRIORITIES[matched_name]

      if current_priority != new_priority:
        updates_needed.append(
          {
            "name": name,
            "id": indexer_id,
            "current": current_priority,
            "new": new_priority,
            "matched": matched_name,
            "confidence": match_ratio,
          }
        )
      else:
        already_correct.append(
          {
            "name": name,
            "priority": current_priority,
            "matched": matched_name,
            "confidence": match_ratio,
          }
        )
    else:
      not_in_list.append(name)

  # Display results
  print("\n" + "=" * 70)
  print("PROWLARR INDEXER PRIORITY ANALYSIS")
  print("=" * 70)

  if updates_needed:
    print(f"\n🔄 UPDATES NEEDED ({len(updates_needed)}):")
    print("-" * 50)
    for item in updates_needed:
      confidence_text = f" (fuzzy: {item['confidence']:.1%})" if item["confidence"] < 1.0 else ""
      print(f"  • {item['name']} (ID: {item['id']})")
      print(f"    Current: {item['current']} → New: {item['new']}")
      if confidence_text:
        print(f"    Matched: '{item['matched']}'{confidence_text}")
      print()

  if already_correct:
    print(f"\n✅ ALREADY CORRECT ({len(already_correct)}):")
    print("-" * 30)
    for item in already_correct:
      confidence_text = f" (fuzzy: {item['confidence']:.1%})" if item["confidence"] < 1.0 else ""
      print(f"  • {item['name']}: {item['priority']}{confidence_text}")

  if not_in_list:
    print(f"\n⏭️  NOT IN PRIORITY LIST ({len(not_in_list)}):")
    print("-" * 40)
    for name in not_in_list:
      print(f"  • {name}")

  # Manual update instructions
  if updates_needed:
    print("\n" + "=" * 70)
    print("MANUAL UPDATE INSTRUCTIONS")
    print("=" * 70)
    print("Due to API issues, please update these manually in the Prowlarr UI:")
    print(f"1. Open {host}:{port}/settings/indexers")
    print("2. Update the following indexers:")
    print()
    for item in updates_needed:
      print(f"   • {item['name']}: Set priority to {item['new']}")

    print(f"\n🎯 Summary: {len(updates_needed)} indexers need priority updates")
  else:
    print("\n🎉 All indexers already have correct priorities!")


if __name__ == "__main__":
  main()
