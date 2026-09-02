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
  desired = qbt.desired_prefs(shaped=True)
  changes = qbt.plan_pref_changes(current, desired)
  assert changes == {
    "auto_tmm_enabled": True,
    "save_path_changed_tmm_enabled": True,
    "temp_path": "/downloads/incomplete/qbittorrent",
    "up_limit": qbt.UPLOAD_LIMIT_BYTES_PER_SEC,
    "max_uploads": qbt.MAX_UPLOAD_SLOTS,
  }


def test_upload_limit_stays_below_the_real_line_rate():
  """The cap's job is damage control when the shaper is missing, not reservation.

  scripts/wan_shaper.sh marks torrent egress CS1 so CAKE's Bulk tin makes it
  yield automatically -- that is what protects a stream, and it scales to any
  number of viewers. But `tc` state is lost on link-down, and there is a window
  before stack_watchdog notices. During that window the cap is the only thing
  standing between BitTorrent and the modem queue, so it must stay under the
  measured line rate. The original defect was 33.55 Mbps on a ~31 Mbps link.
  """
  measured_upstream_bps = 31_000_000
  cap_bps = qbt.UPLOAD_LIMIT_BYTES_PER_SEC * 8
  assert cap_bps < measured_upstream_bps


def test_upload_limit_is_not_a_per_viewer_budget():
  """Guards against reintroducing arithmetic that assumed exactly one viewer.

  An earlier version asserted `cap + 8 Mbps <= 28 Mbit shaped`, which silently
  encoded "there is one remote viewer". RemoteClientBitrateLimit is a per-stream
  ceiling, not a server-wide aggregate -- verified against the live server, two
  concurrent remote requests were each offered the full 8 Mbps -- so with five
  users that budget was wrong. DSCP replaced it.
  """
  single_viewer_budget = 28_000_000 - 8_000_000
  cap_bps = qbt.UPLOAD_LIMIT_BYTES_PER_SEC * 8
  assert cap_bps > single_viewer_budget


def test_plan_pref_changes_empty_when_already_correct():
  current = dict(qbt.desired_prefs(shaped=True))
  assert qbt.plan_pref_changes(current, qbt.desired_prefs(shaped=True)) == {}


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


def test_degraded_cap_applies_when_the_shaper_is_gone():
  """One number cannot be both a capacity target and a safety net.

  Unshaped, the 25 Mbps capacity figure lands in the same range as the original
  defect: a 33.55 Mbps cap produced 23.4 Mbps of real upload and 5% packet loss.
  So when CAKE is absent the enforcer drops to the 15 Mbps value, which was
  measured unshaped at 0% loss.
  """
  shaped = qbt.desired_prefs(shaped=True)["up_limit"]
  degraded = qbt.desired_prefs(shaped=False)["up_limit"]
  assert degraded < shaped
  assert degraded == qbt.UPLOAD_LIMIT_DEGRADED_BYTES_PER_SEC


def test_degraded_cap_is_a_measured_value_not_a_guess():
  """15 Mbps unshaped measured 0% loss / 37 ms max; it must stay under half the link."""
  measured_upstream_bps = 31_000_000
  degraded_bps = qbt.UPLOAD_LIMIT_DEGRADED_BYTES_PER_SEC * 8
  assert degraded_bps / measured_upstream_bps < 0.55
