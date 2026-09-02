import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "qbittorrent_settings_enforce.py"
  spec = importlib.util.spec_from_file_location("qbittorrent_settings_enforce", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


qbt = _load_module()


# ---- plan_pref_changes ---------------------------------------------------


def test_plan_pref_changes_returns_only_differing_keys():
  current = {
    "auto_tmm_enabled": False,
    "category_changed_tmm_enabled": True,
    "save_path_changed_tmm_enabled": False,
    "temp_path_enabled": True,
    "temp_path": "/downloads/incomplete",
    # the cap qBittorrent shipped with: 33.55 Mbps, above this link's entire
    # ~31 Mbps upstream, which is what kept the uplink queue permanently full
    "up_limit": 4194304,
    "unrelated": "x",
  }
  desired = qbt.DESIRED_PREFS
  changes = qbt.plan_pref_changes(current, desired)
  assert changes == {
    "auto_tmm_enabled": True,
    "save_path_changed_tmm_enabled": True,
    "temp_path": "/downloads/incomplete/qbittorrent",
    "up_limit": qbt.UPLOAD_LIMIT_BYTES_PER_SEC,
  }


def test_upload_limit_leaves_headroom_on_the_measured_uplink():
  """The cap must stay well under the ~31 Mbps upstream measured 2026-09-02.

  Set at or above capacity, BitTorrent keeps the modem queue full and every
  other flow on the link -- notably remote Jellyfin playback -- loses packets.
  """
  measured_upstream_bps = 31_000_000
  headroom_fraction = qbt.UPLOAD_LIMIT_BYTES_PER_SEC * 8 / measured_upstream_bps
  assert headroom_fraction < 0.6


def test_plan_pref_changes_empty_when_already_correct():
  current = dict(qbt.DESIRED_PREFS)
  assert qbt.plan_pref_changes(current, qbt.DESIRED_PREFS) == {}


# ---- collect_unmanaged_hashes --------------------------------------------


def test_collect_unmanaged_hashes_picks_non_auto_tmm():
  torrents = [
    {"hash": "a", "auto_tmm": False},
    {"hash": "b", "auto_tmm": True},
    {"hash": "c", "auto_tmm": False},
    {"hash": "d"},  # missing -> treated as unmanaged
  ]
  assert qbt.collect_unmanaged_hashes(torrents) == ["a", "c", "d"]


def test_collect_unmanaged_hashes_empty():
  assert qbt.collect_unmanaged_hashes([]) == []
  assert qbt.collect_unmanaged_hashes([{"hash": "x", "auto_tmm": True}]) == []


# ---- summarize_targets ---------------------------------------------------


def test_summarize_targets_counts_by_target_path():
  torrents = [
    {"hash": "a", "category": "arr-sonarr"},
    {"hash": "b", "category": "arr-sonarr"},
    {"hash": "c", "category": "arr-radarr"},
    {"hash": "d", "category": ""},  # uncategorized -> default/manual
  ]
  categories = {
    "arr-sonarr": {"savePath": "/downloads/complete/sonarr"},
    "arr-radarr": {"savePath": "/downloads/complete/radarr"},
  }
  out = qbt.summarize_targets(torrents, categories)
  assert out == {
    "/downloads/complete/sonarr": 2,
    "/downloads/complete/radarr": 1,
    "(default save path)": 1,
  }
