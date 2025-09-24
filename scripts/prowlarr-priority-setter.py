#!/usr/bin/env python3
"""
Prowlarr Priority Setter

This script automatically updates indexer priorities in Prowlarr via the API.
It connects to your Prowlarr instance and sets priorities for specified indexers
based on the configuration in prowlarr-config.yml.

Requirements:
- Prowlarr running and accessible
- .env file with API_KEY_PROWLARR set
- prowlarr-config.yml file with indexer priorities
- requests library (pip install requests)
- python-dotenv library (pip install python-dotenv)
- PyYAML library (pip install PyYAML)

Usage:
    python prowlarr-priority-setter.py

Author: GitHub Copilot
"""

import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

import requests

try:
  from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover

  def load_dotenv():
    return False


from prowlarr_config import load_prowlarr_config

# Load environment variables from .env file (best-effort)
load_dotenv()

# Load configuration from YAML file
CONFIG = load_prowlarr_config()
INDEXER_PRIORITIES: dict[str, int] = CONFIG.indexer_priorities

# Constants
MIN_PRIORITY = 1
MAX_PRIORITY = 50
DEFAULT_PORT = 9696
DEFAULT_HOST = "http://localhost"
FUZZY_MATCH_THRESHOLD = CONFIG.strict_fuzzy_threshold  # Use strict threshold for setter
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
REQUEST_TIMEOUT_SECONDS = 10


@dataclass
class ProwlarrConfiguration:
  """Configuration for connecting to Prowlarr API."""

  api_key: str
  host: str = DEFAULT_HOST
  port: int = DEFAULT_PORT

  @property
  def base_url(self) -> str:
    """Get the base API URL."""
    return f"{self.host}:{self.port}/api"

  def display_info(self) -> None:
    """Display configuration information."""
    print("🔧 Configuration loaded:")
    print(f"   Host: {self.host}")
    print(f"   Port: {self.port}")
    print(f"   API Key: {self.api_key[:10]}...{self.api_key[-4:]}")
    print(f"   Config File: {len(INDEXER_PRIORITIES)} indexers loaded")
    print(f"   Fuzzy Threshold: {FUZZY_MATCH_THRESHOLD}")
    print()


@dataclass
class IndexerMatchResult:
  """Result of fuzzy matching an indexer name."""

  matched_name: str | None
  confidence: float

  @property
  def is_match(self) -> bool:
    """Check if a match was found."""
    return self.matched_name is not None


@dataclass
class UpdateResult:
  """Result of an indexer update operation."""

  indexer_name: str
  success: bool
  message: str
  error_type: str | None = None


@dataclass
class ProcessingSummary:
  """Summary of all processing results."""

  results: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
  total_attempted: int = 0
  successful_updates: int = 0

  def add_result(self, category: str, item: str) -> None:
    """Add a result to the specified category."""
    self.results[category].append(item)

  def increment_attempted(self) -> None:
    """Increment the total attempted counter."""
    self.total_attempted += 1

  def increment_successful(self) -> None:
    """Increment the successful updates counter."""
    self.successful_updates += 1

  @property
  def success_rate(self) -> float:
    """Calculate the success rate as a percentage."""
    if self.total_attempted == 0:
      return 0.0
    return (self.successful_updates / self.total_attempted) * 100


class IndexerMatcher:
  """Handles fuzzy matching of indexer names."""

  @staticmethod
  def find_best_match(
    target_name: str, available_names: list[str], threshold: float = FUZZY_MATCH_THRESHOLD
  ) -> IndexerMatchResult:
    """Find the best matching indexer name using fuzzy matching."""
    best_match = None
    best_ratio = 0.0

    for name in available_names:
      # Try exact match first
      if target_name == name:
        return IndexerMatchResult(matched_name=name, confidence=1.0)

      # Try case-insensitive match
      if target_name.lower() == name.lower():
        return IndexerMatchResult(matched_name=name, confidence=0.95)

      # Try fuzzy matching
      ratio = SequenceMatcher(None, target_name.lower(), name.lower()).ratio()
      if ratio > best_ratio and ratio >= threshold:
        best_ratio = ratio
        best_match = name

    return IndexerMatchResult(matched_name=best_match, confidence=best_ratio)


class IndexerValidator:
  """Validates indexer state and update requirements."""

  @staticmethod
  def is_updateable(indexer: dict[str, Any]) -> tuple[bool, str]:
    """Check if an indexer is in a state where it can be updated."""
    if not indexer.get("enable", True):
      return False, "Indexer is disabled"

    if not indexer.get("id"):
      return False, "Missing indexer ID"

    if not indexer.get("supportsSearch", True) and not indexer.get("supportsRss", True):
      return False, "Indexer doesn't support search or RSS"

    return True, "OK"

  @staticmethod
  def should_skip_update(indexer: dict[str, Any], new_priority: int) -> tuple[bool, str]:
    """Determine if we should skip the update (not count as failure)."""
    current_priority = indexer.get("priority", MAX_PRIORITY)

    if current_priority == new_priority:
      return True, f"Priority already set to {new_priority}"

    return False, "Update needed"


class ErrorClassifier:
  """Classifies API errors to distinguish real failures from expected issues."""

  @staticmethod
  def classify_error(status_code: int, response_text: str, indexer_name: str) -> tuple[str, str]:
    """Classify errors to distinguish real failures from expected issues."""
    response_lower = response_text.lower()

    if status_code == 400:
      if "priority" in response_lower and (
        "between" in response_lower or "range" in response_lower
      ):
        return "invalid_priority", "Priority value out of range"
      elif "no results" in response_lower or "indexer category" in response_lower:
        return "indexer_config", "Indexer configuration issue (not a script failure)"
      elif "unable to connect" in response_lower:
        return "indexer_offline", "Indexer appears to be offline/unreachable"
      else:
        return "validation_error", "Validation error from Prowlarr"

    elif status_code == 500:
      if "database is locked" in response_lower:
        return "database_lock", "Temporary database lock (retryable)"
      else:
        return "server_error", "Server error in Prowlarr"

    elif status_code == 404:
      return "not_found", "Indexer not found (may have been deleted)"

    elif status_code == 401 or status_code == 403:
      return "auth_error", "Authentication/authorization error"

    else:
      return "unknown_error", f"Unexpected HTTP {status_code} error"


class ProwlarrApiClient:
  """Client for interacting with Prowlarr API."""

  def __init__(self, config: ProwlarrConfiguration):
    self.config = config
    self.session = requests.Session()
    self.session.headers.update({"X-Api-Key": config.api_key})

  def _discover_api_endpoint(self) -> str:
    """Discover the working API endpoint."""
    print("Testing different Prowlarr API endpoints...")

    api_paths = [
      f"{self.config.base_url}/v1/indexer",
      f"{self.config.base_url}/indexer",
      f"{self.config.base_url}/v1/indexers",
      f"{self.config.base_url}/indexers",
      f"{self.config.base_url}/v1/system/status",
    ]

    for test_url in api_paths:
      print(f"Testing: {test_url}")
      try:
        response = self.session.get(test_url, timeout=5)
        content_type = response.headers.get("content-type", "unknown")
        print(f"  Status: {response.status_code}, Content-Type: {content_type}")

        if response.status_code == 200 and "json" in content_type:
          print("  SUCCESS: Found working API endpoint!")
          if "indexer" in test_url:
            return test_url.rsplit("/", 1)[0]  # Remove the last part to get base URL
          elif "status" in test_url:
            print(f"  System status response: {response.json()}")
        else:
          print(f"  Failed: {response.text[:100]}")
      except Exception as e:
        print(f"  Error: {e}")
      print()

    raise ConnectionError(
      "Could not find a working API endpoint. Please check:\n"
      "1. Prowlarr is running and accessible\n"
      "2. API key is correct\n"
      "3. API is enabled in Prowlarr settings",
    )

  def fetch_indexers(self) -> list[dict[str, Any]]:
    """Fetch all indexers from Prowlarr."""
    base_url = self._discover_api_endpoint()
    print(f"Using base URL: {base_url}")

    indexer_endpoints = ["indexer", "indexers"]

    for endpoint in indexer_endpoints:
      try:
        url = f"{base_url}/{endpoint}"
        print(f"Trying endpoint: {url}")

        response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        print(f"Response status code: {response.status_code}")
        print(f"Response headers: {dict(response.headers)}")
        print(f"Response content: {response.text[:500]}")  # First 500 chars

        if response.status_code != 200:
          print(f"Failed to fetch from {endpoint}. Status: {response.status_code}")
          print(f"Response: {response.text}")
          continue

        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
          print(f"Warning: Response doesn't appear to be JSON. Content-Type: {content_type}")
          continue

        indexers = response.json()
        print(f"Found {len(indexers)} items from {endpoint} endpoint.")
        return indexers

      except requests.exceptions.ConnectionError as e:
        print(f"Connection error for {endpoint}: Could not connect to Prowlarr at {base_url}")
        print(f"Error details: {e}")
        continue
      except requests.exceptions.Timeout as e:
        print(
          f"Timeout error for {endpoint}: Request timed out after {REQUEST_TIMEOUT_SECONDS} seconds"
        )
        print(f"Error details: {e}")
        continue
      except requests.exceptions.JSONDecodeError as e:
        print(f"JSON decode error for {endpoint}: Response is not valid JSON")
        print(f"Error details: {e}")
        print(f"Raw response: {response.text}")
        continue
      except Exception as e:
        print(f"Unexpected error for {endpoint}: {e}")
        continue

    raise ConnectionError(
      "Could not fetch indexers from any endpoint.\n"
      "Please check if Prowlarr is running and accessible at the specified address.",
    )

  def update_indexer(self, indexer: dict[str, Any], base_url: str) -> UpdateResult:
    """Update a single indexer's priority."""
    indexer_id = indexer["id"]
    indexer_name = indexer["name"]

    update_endpoints = [
      f"{base_url}/indexer/{indexer_id}",
      f"{base_url}/indexers/{indexer_id}",
    ]

    for endpoint in update_endpoints:
      print(f"Trying to update via: {endpoint}")

      for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
          response = self.session.put(endpoint, json=indexer, timeout=REQUEST_TIMEOUT_SECONDS)
          print(f"Response status: {response.status_code}")

          if response.status_code in [200, 202]:
            priority = indexer.get("priority", "N/A")
            print(f"✓ Successfully updated {indexer_name} -> priority {priority}")
            return UpdateResult(
              indexer_name=indexer_name,
              success=True,
              message=f"Priority updated to {priority}",
            )
          elif response.status_code == 500 and "database is locked" in response.text:
            if attempt < MAX_RETRY_ATTEMPTS - 1:
              print(
                f"Database locked, retrying in {RETRY_DELAY_SECONDS} seconds... (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS})"
              )
              time.sleep(RETRY_DELAY_SECONDS)
              continue
            else:
              return UpdateResult(
                indexer_name=indexer_name,
                success=False,
                message="Failed after 3 attempts due to database lock",
                error_type="database_lock",
              )
          else:
            error_type, error_description = ErrorClassifier.classify_error(
              response.status_code,
              response.text,
              indexer_name,
            )
            print(f"✗ Failed with status {response.status_code}: {error_description}")

            return UpdateResult(
              indexer_name=indexer_name,
              success=False,
              message=error_description,
              error_type=error_type,
            )

        except Exception as e:
          print(f"✗ Exception during update: {e}")
          return UpdateResult(
            indexer_name=indexer_name,
            success=False,
            message=f"Exception: {e}",
            error_type="exception",
          )

      break  # Only try the first working endpoint

    return UpdateResult(
      indexer_name=indexer_name,
      success=False,
      message="Failed to update via any endpoint",
      error_type="connection_error",
    )


class IndexerPriorityUpdater:
  """Main class for updating indexer priorities."""

  def __init__(self, config: ProwlarrConfiguration, dry_run: bool = False):
    self.config = config
    self.dry_run = dry_run
    self.api_client = ProwlarrApiClient(config)
    self.matcher = IndexerMatcher()
    self.validator = IndexerValidator()
    self.summary = ProcessingSummary()

  def run(self) -> None:
    """Execute the priority update process."""
    self.config.display_info()

    if self.dry_run:
      print("🔍 DRY RUN MODE: No changes will be made to Prowlarr")
      print("=" * 60)

    try:
      indexers = self.api_client.fetch_indexers()
      self._process_indexers(indexers)
      self._display_summary(len(indexers))
      self._display_usage_info()

    except (ConnectionError, requests.RequestException) as e:
      print(f"❌ Error: {e}")
      sys.exit(1)

  def _process_indexers(self, indexers: list[dict[str, Any]]) -> None:
    """Process all indexers and update their priorities."""
    print("\n" + "=" * 50)
    print("UPDATING INDEXER PRIORITIES")
    print("=" * 50)

    available_priority_names = list(INDEXER_PRIORITIES.keys())
    # Discover the base URL once at the beginning if we're not in dry-run mode
    base_url = None
    if not self.dry_run:
      base_url = self.api_client._discover_api_endpoint()

    for indexer in indexers:
      indexer_name = indexer["name"]

      # Try to find a match in our priorities (with fuzzy matching)
      match_result = self.matcher.find_best_match(
        indexer_name,
        available_priority_names,
      )

      if match_result.is_match:
        self._process_matched_indexer(indexer, match_result, base_url)
      else:
        print(f"{indexer_name} not in priority list, skipping.")
        self.summary.add_result("skipped", indexer_name)

  def _process_matched_indexer(
    self, indexer: dict[str, Any], match_result: IndexerMatchResult, base_url: str | None
  ) -> None:
    """Process an indexer that matches our priority list."""
    indexer_name = indexer["name"]
    matched_name = match_result.matched_name
    new_priority = INDEXER_PRIORITIES[matched_name]
    indexer_id = indexer["id"]

    print(f"\nProcessing {indexer_name} (ID: {indexer_id})")

    if match_result.confidence < 1.0:
      print(
        f"📍 Fuzzy matched '{indexer_name}' to '{matched_name}' "
        f"(confidence: {match_result.confidence:.1%})"
      )

    print(f"Current priority: {indexer.get('priority', 'N/A')}")
    print(f"New priority: {new_priority}")

    # Pre-flight checks
    can_update, update_reason = self.validator.is_updateable(indexer)
    if not can_update:
      print(f"⏭️  Skipping {indexer_name}: {update_reason}")
      self.summary.add_result("skipped_invalid", f"{indexer_name}: {update_reason}")
      return

    should_skip, skip_reason = self.validator.should_skip_update(indexer, new_priority)
    if should_skip:
      print(f"⏭️  Skipping {indexer_name}: {skip_reason}")
      self.summary.add_result("skipped_unchanged", f"{indexer_name}: {skip_reason}")
      self.summary.increment_successful()  # Count as success since it's already correct
      self.summary.increment_attempted()
      return

    # Update the indexer
    self._update_indexer_priority(indexer, new_priority, base_url)

  def _update_indexer_priority(
    self, indexer: dict[str, Any], new_priority: int, base_url: str | None
  ) -> None:
    """Update an indexer's priority."""
    indexer_name = indexer["name"]
    current_priority = indexer.get("priority", "N/A")

    # For Prowlarr, we need to send the full indexer object back with updated priority
    # Make a deep copy to avoid any reference issues
    import copy

    updated_indexer = copy.deepcopy(indexer)
    updated_indexer["priority"] = new_priority

    self.summary.increment_attempted()

    if self.dry_run:
      print(f"🔍 DRY RUN: Would update {indexer_name} -> priority {new_priority}")
      self.summary.increment_successful()
      self.summary.add_result("success", f"{indexer_name}: {current_priority} → {new_priority}")
      return

    # Perform the actual update
    result = self.api_client.update_indexer(updated_indexer, base_url)

    if result.success:
      self.summary.increment_successful()
      self.summary.add_result("success", f"{indexer_name}: {current_priority} → {new_priority}")
    else:
      self._handle_update_failure(result)

    if not result.success:
      print(f"Failed to update {indexer_name} via any endpoint")

  def _handle_update_failure(self, result: UpdateResult) -> None:
    """Handle update failure by categorizing the error."""
    error_type = result.error_type
    indexer_name = result.indexer_name
    message = result.message

    if error_type in ["indexer_config", "indexer_offline"]:
      self.summary.add_result("indexer_issues", f"{indexer_name}: {message}")
      print("💡 This is likely an indexer configuration issue, not a script problem")
    elif error_type == "invalid_priority":
      self.summary.add_result("script_errors", f"{indexer_name}: {message}")
    elif error_type == "database_lock":
      self.summary.add_result("database_locked", indexer_name)
    elif error_type == "exception":
      self.summary.add_result("exception", indexer_name)
    else:
      self.summary.add_result("other_error", f"{indexer_name}: {message}")

  def _display_summary(self, total_indexers: int) -> None:
    """Display a comprehensive summary of the processing results."""
    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    print(f"Total indexers found: {total_indexers}")
    print(f"Total indexers in priority list: {self.summary.total_attempted}")
    print(f"Successfully updated: {self.summary.successful_updates}")
    print(f"Skipped (not in priority list): {len(self.summary.results['skipped'])}")

    if self.summary.results["success"]:
      print(f"\n✅ Successfully updated ({len(self.summary.results['success'])}):")
      for update in self.summary.results["success"]:
        print(f"  - {update}")

    if self.summary.results["skipped_unchanged"]:
      print(
        f"\n⏭️  Skipped - already correct priority ({len(self.summary.results['skipped_unchanged'])}):"
      )
      for item in self.summary.results["skipped_unchanged"]:
        print(f"  - {item}")

    if self.summary.results["skipped_invalid"]:
      print(
        f"\n⏭️  Skipped - indexer not updateable ({len(self.summary.results['skipped_invalid'])}):"
      )
      for item in self.summary.results["skipped_invalid"]:
        print(f"  - {item}")

    if self.summary.results["database_locked"]:
      print(f"\n🔄 Temporary database locks ({len(self.summary.results['database_locked'])}):")
      for name in self.summary.results["database_locked"]:
        print(f"  - {name}")
      print("  💡 These are temporary issues - try running the script again.")

    if self.summary.results["indexer_issues"]:
      print(f"\n🔧 Indexer configuration issues ({len(self.summary.results['indexer_issues'])}):")
      for item in self.summary.results["indexer_issues"]:
        print(f"  - {item}")
      print(
        "  💡 These are indexer-specific problems, not script failures. Check indexer settings in Prowlarr UI."
      )

    if self.summary.results["script_errors"]:
      print(f"\n❌ Script configuration errors ({len(self.summary.results['script_errors'])}):")
      for item in self.summary.results["script_errors"]:
        print(f"  - {item}")
      print("  💡 These need to be fixed in the script configuration.")

    if self.summary.results["other_error"]:
      print(f"\n❓ Other errors ({len(self.summary.results['other_error'])}):")
      for item in self.summary.results["other_error"]:
        print(f"  - {item}")

    if self.summary.results["exception"]:
      print(f"\n💥 Exceptions ({len(self.summary.results['exception'])}):")
      for name in self.summary.results["exception"]:
        print(f"  - {name}")

    # Calculate real failures (excluding indexer issues which aren't script failures)
    real_failures = (
      len(self.summary.results["database_locked"])
      + len(self.summary.results["script_errors"])
      + len(self.summary.results["other_error"])
      + len(self.summary.results["exception"])
    )
    indexer_issues = len(self.summary.results["indexer_issues"])

    print(
      f"\n🎯 Success rate: {self.summary.successful_updates}/{self.summary.total_attempted} "
      f"({self.summary.success_rate:.1f}%)"
    )

    if indexer_issues > 0:
      print(
        f"📊 Real script failures: {real_failures} "
        f"(indexer config issues: {indexer_issues} - not counted as failures)"
      )
    else:
      print(f"📊 Real script failures: {real_failures}")

  def _display_usage_info(self) -> None:
    """Display usage information."""
    if self.dry_run:
      print("\n💡 This was a dry run. To actually make changes, run without --dry-run or -n")
    else:
      print(
        "\n💡 To preview changes before applying, use: python prowlarr-priority-setter.py --dry-run"
      )

    print("\n📚 Usage:")
    print("  python prowlarr-priority-setter.py           # Apply changes")
    print("  python prowlarr-priority-setter.py --dry-run # Preview changes only")
    print("  python prowlarr-priority-setter.py -n        # Preview changes only (short form)")
    print("\n🔧 Configuration:")
    print("  Required in .env file: API_KEY_PROWLARR=your_api_key")
    print("  Optional in .env file: PROWLARR_HOST=http://localhost")
    print("  Optional in .env file: PROWLARR_PORT=9696")


def create_configuration() -> ProwlarrConfiguration:
  """Create configuration from environment variables."""
  api_key = os.getenv("API_KEY_PROWLARR")
  if not api_key:
    print("❌ Error: API_KEY_PROWLARR not found in .env file")
    print("Please ensure your .env file contains: API_KEY_PROWLARR=your_api_key_here")
    sys.exit(1)

  host = os.getenv("PROWLARR_HOST", DEFAULT_HOST)
  port = int(os.getenv("PROWLARR_PORT", str(DEFAULT_PORT)))

  return ProwlarrConfiguration(api_key=api_key, host=host, port=port)


def main() -> None:
  """Main entry point."""
  try:
    config = create_configuration()
    dry_run = "--dry-run" in sys.argv or "-n" in sys.argv

    updater = IndexerPriorityUpdater(config, dry_run)
    updater.run()

  except KeyboardInterrupt:
    print("\n\n🛑 Operation cancelled by user")
    sys.exit(1)
  except Exception as e:
    print(f"\n❌ Unexpected error: {e}")
    sys.exit(1)


if __name__ == "__main__":
  main()
