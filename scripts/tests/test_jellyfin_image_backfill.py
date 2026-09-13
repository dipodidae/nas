"""Tests for scripts/jellyfin_image_backfill.py — pure logic, no Jellyfin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  spec = importlib.util.spec_from_file_location(
    "jellyfin_image_backfill", scripts_dir / "jellyfin_image_backfill.py"
  )
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


jib = _load_module()


def item(item_id: str, primary: bool = False, name: str = "") -> dict:
  return {
    "Id": item_id,
    "Name": name or item_id,
    "ImageTags": {"Primary": "abc"} if primary else {},
  }


def test_has_primary_reads_the_image_tag():
  assert jib.has_primary(item("a", primary=True))
  assert not jib.has_primary(item("b"))
  assert not jib.has_primary({"Id": "c"})  # no ImageTags key at all


def test_items_with_an_image_are_never_targets():
  items = [item("has", primary=True), item("missing")]
  assert [i["Id"] for i in jib.select_targets(items, {}, 1.0e9, 30.0, 10)] == ["missing"]


def test_cooldown_benches_a_recent_attempt():
  now = 1.0e9
  items = [item("recent"), item("old")]
  attempts = {"recent": now - 86400, "old": now - 60 * 86400}
  assert [i["Id"] for i in jib.select_targets(items, attempts, now, 30.0, 10)] == ["old"]


def test_a_never_attempted_item_is_due_regardless_of_clock():
  """attempts.get(...) defaulting to 0.0 made this fail for any small `now`."""
  assert [i["Id"] for i in jib.select_targets([item("new")], {}, 100.0, 30.0, 10)] == ["new"]


def test_never_attempted_sorts_ahead_of_long_ago():
  now = 1.0e9
  items = [item("stale"), item("fresh")]
  targets = jib.select_targets(items, {"stale": 1.0}, now, 30.0, 10)
  assert [i["Id"] for i in targets] == ["fresh", "stale"]


def test_limit_bounds_the_batch():
  items = [item(f"i{n}") for n in range(50)]
  assert len(jib.select_targets(items, {}, 1.0e9, 30.0, 7)) == 7


def test_record_attempts_does_not_mutate_the_input():
  before = {"a": 1.0}
  after = jib.record_attempts(before, ["b", "c"], 5.0)
  assert before == {"a": 1.0}
  assert after == {"a": 1.0, "b": 5.0, "c": 5.0}


def test_prune_drops_ids_jellyfin_no_longer_has():
  assert jib.prune_attempts({"gone": 1.0, "here": 2.0}, {"here"}) == {"here": 2.0}


def test_state_round_trips(tmp_path: Path):
  path = tmp_path / "state.json"
  jib.save_state(path, {"a": 1.5})
  assert jib.load_state(path) == {"a": 1.5}


def test_a_corrupt_state_file_is_not_fatal(tmp_path: Path):
  path = tmp_path / "state.json"
  path.write_text("{not json")
  assert jib.load_state(path) == {}


def test_a_missing_state_file_is_not_fatal(tmp_path: Path):
  assert jib.load_state(tmp_path / "nope.json") == {}
