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
    "max_uploads": qbt.MAX_UPLOAD_SLOTS,
  }


def test_upload_limit_fits_the_shaped_pipe_alongside_a_remote_stream():
  """The cap plus a remote Jellyfin stream must fit inside the CAKE shaper.

  Before scripts/wan_shaper.sh existed the rule was "stay well under raw
  capacity", because nothing was managing the modem's queue. Now CAKE shapes
  internet egress to 28 Mbit and Jellyfin's RemoteClientBitrateLimit caps a
  remote client at 8 Mbps, so the real constraint is a budget: seeding plus one
  remote stream has to fit in the shaped pipe, or they compete for it.
  """
  shaped_egress_bps = 28_000_000        # scripts/wan_shaper.sh SHAPE_MBIT
  remote_stream_bps = 8_000_000         # Jellyfin RemoteClientBitrateLimit
  budget_bps = qbt.UPLOAD_LIMIT_BYTES_PER_SEC * 8 + remote_stream_bps
  assert budget_bps <= shaped_egress_bps


def test_upload_limit_is_not_above_the_measured_uplink():
  """The original defect: a cap of 33.55 Mbps on a ~31 Mbps link is no cap."""
  measured_upstream_bps = 31_000_000
  cap_bps = qbt.UPLOAD_LIMIT_BYTES_PER_SEC * 8
  assert cap_bps < measured_upstream_bps


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
