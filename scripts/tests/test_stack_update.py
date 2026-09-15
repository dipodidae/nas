"""Tests for scripts/stack_update.py — the update workflow's decision logic.

Each test here is a real failure this stack has produced, not a hypothetical:

* `rank_key` exists because diun ranked `5.2.3_v2.0.14-ls475` as newest two
  days after `ls476` shipped, so the tests pin the comparisons a semver or
  lexicographic sort gets wrong.
* `stop_is_truncated` exists because CAP_KILL was granted, asserted, and the
  stop was still a SIGKILL (ADR-0041).
* `wal_is_clean` exists because a dirty WAL is what makes a backup silently
  stale rather than loudly broken.
* `verdict` exists because `cron_job.py` treats exit 0 and 1 alike, so a
  failure that exits 1 cannot alert anyone (ADR-0003).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  spec = importlib.util.spec_from_file_location(
    "stack_update", scripts_dir / "stack_update.py"
  )
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


su = _load_module()
Kind, Store = su.Kind, su.Store


# ---------------------------------------------------------------------------
# rank_key / newer_tags — the diun blind spot
# ---------------------------------------------------------------------------


def test_lsio_build_number_orders_numerically_not_lexicographically():
  """ls476 > ls475. A string sort gets this right; ls99 vs ls101 does not."""
  assert su.rank_key("5.2.3_v2.0.14-ls476") > su.rank_key("5.2.3_v2.0.14-ls475")
  assert su.rank_key("12.1ubu2604-ls101") > su.rank_key("12.1ubu2604-ls99")
  assert "12.1ubu2604-ls101" < "12.1ubu2604-ls99"  # the sort we must not use


def test_the_exact_tag_diun_missed_is_seen_as_newer():
  """Regression: qbittorrent ls476 shipped 2026-09-13; diun still said ls475."""
  current = "5.2.3_v2.0.14-ls475"
  tags = ["5.2.3_v2.0.14-ls474", "5.2.3_v2.0.14-ls475", "5.2.3_v2.0.14-ls476"]
  assert su.newer_tags(current, tags) == ["5.2.3_v2.0.14-ls476"]


def test_jellyfin_two_component_version_still_ranks():
  """12.0 -> 12.1: Jellyfin dropped its leading `10.`, so this has TWO
  numeric components where a three-component filter matched nothing."""
  assert su.rank_key("12.1ubu2604-ls49") > su.rank_key("12.0ubu2604-ls48")
  assert su.newer_tags(
    "12.0ubu2604-ls48", ["12.0ubu2604-ls48", "12.1ubu2604-ls49"]
  ) == ["12.1ubu2604-ls49"]


def test_minor_version_20_beats_9():
  """v2.20.0 > v2.9.0 numerically, and < lexicographically."""
  assert su.rank_key("v2.20.0") > su.rank_key("v2.9.0")
  assert su.newer_tags("v2.18.1", ["v2.19.2", "v2.20.0", "v2.18.1"]) == [
    "v2.20.0", "v2.19.2"
  ]


def test_suffixed_tag_ranks_on_its_numbers():
  assert su.newer_tags(
    "v0.9.3-omnibus", ["v0.9.3-omnibus", "v0.9.4-omnibus"]
  ) == ["v0.9.4-omnibus"]


def test_nightly_is_not_offered_as_an_upgrade_from_a_release():
  """Same number of numeric runs, entirely different meaning. Shape separates
  them; without that check a nightly outranks every stable release."""
  tags = ["12.0ubu2604-ls48", "12.1ubu2604-ls49",
          "nightly-2026091410ubu2604-ls101"]
  assert su.newer_tags("12.0ubu2604-ls48", tags) == ["12.1ubu2604-ls49"]


def test_current_tag_is_never_returned_as_an_upgrade():
  assert su.newer_tags("v1.2.3", ["v1.2.3"]) == []


def test_older_tags_are_not_offered():
  assert su.newer_tags("v2.0.0", ["v1.9.9", "v1.0.0"]) == []


def test_tag_with_no_digits_ranks_empty_and_never_upgrades():
  assert su.rank_key("latest") == ()
  assert su.newer_tags("latest", ["stable", "latest"]) == []


# ---------------------------------------------------------------------------
# classify — what shape of update a service has
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
  "image,expected",
  [
    ("lscr.io/linuxserver/sonarr:latest", Kind.DRIFT),
    ("lscr.io/linuxserver/lidarr:nightly", Kind.DRIFT),
    ("lscr.io/linuxserver/jellyfin:12.1ubu2604-ls49", Kind.PINNED),
    ("ghcr.io/tinyauthapp/tinyauth:v5.2.0", Kind.PINNED),
    ("recyclarr/recyclarr:8", Kind.PINNED),
  ],
)
def test_classify_by_tag(image, expected):
  assert su.classify(image, has_build=False) is expected


def test_untagged_image_is_drift_not_pinned():
  """No tag means :latest, which moves."""
  assert su.classify("nginx", has_build=False) is Kind.DRIFT


def test_registry_port_is_not_mistaken_for_a_tag():
  assert su.classify("registry.local:5000/app", has_build=False) is Kind.DRIFT


def test_build_section_wins_over_a_pinned_looking_tag():
  """The trap `pnpm update` falls into: a built service names a local image
  tag, and a pull against it does nothing at all."""
  assert su.classify("nas/4eva-rootpage:latest", has_build=True) is Kind.BUILT
  assert su.classify("nas/ongehoord:v1.2.3", has_build=True) is Kind.BUILT


# ---------------------------------------------------------------------------
# backup policy
# ---------------------------------------------------------------------------


def test_stateful_services_need_a_backup():
  assert su.needs_backup("jellyfin", Kind.PINNED) is True
  assert su.needs_backup("sonarr", Kind.DRIFT) is True
  assert su.needs_backup("streamystats-db", Kind.PINNED) is True


def test_stateless_services_do_not():
  assert su.needs_backup("swag", Kind.DRIFT) is False
  assert su.needs_backup("autoheal", Kind.DRIFT) is False


def test_built_services_need_no_backup_because_no_image_moves():
  assert su.needs_backup("jellyfin", Kind.BUILT) is False


def test_postgres_services_are_not_treated_as_sqlite():
  """A filesystem tar of PGDATA is not a worse backup, it is an empty one."""
  assert su.store_of("streamystats-db") is Store.POSTGRES
  assert su.store_of("playlist-generator-db") is Store.POSTGRES
  assert su.store_of("jellyfin") is Store.SQLITE


def test_one_way_services_are_flagged():
  """Reverting these leaves the old binary unable to open its own data."""
  assert su.is_one_way("jellyfin") is True
  assert su.is_one_way("tinyauth") is True
  assert su.is_one_way("sonarr") is False


def test_unknown_service_is_assumed_stateless():
  assert su.store_of("something-new") is Store.NONE


# ---------------------------------------------------------------------------
# ADR-0041 — CAP_KILL without headroom
# ---------------------------------------------------------------------------


def test_cap_kill_without_grace_period_is_a_truncated_stop():
  """The exact defect found on jellyfin 2026-09-15."""
  assert su.stop_is_truncated(["CHOWN", "KILL", "SETUID"], None) is True


def test_grace_equal_to_the_docker_default_buys_nothing():
  assert su.stop_is_truncated(["KILL"], "10s") is True


def test_grace_above_the_default_is_fine():
  assert su.stop_is_truncated(["KILL"], "120s") is False
  assert su.stop_is_truncated(["KILL"], "2m0s") is False


def test_without_cap_kill_the_grace_period_is_not_this_defect():
  """A different failure (EPERM on SIGTERM), owned by ADR-0004/0021."""
  assert su.stop_is_truncated(["CHOWN"], None) is False


def test_cap_prefix_is_tolerated():
  """docker inspect reports CAP_KILL; compose says KILL."""
  assert su.stop_is_truncated(["CAP_KILL"], None) is True


@pytest.mark.parametrize(
  "raw,expected",
  [("120s", 120), ("2m0s", 120), ("1m", 60), ("1h", 3600), (120, 120),
   ("90", 90), (None, None), ("", None), ("soon", None)],
)
def test_grace_seconds_parsing(raw, expected):
  assert su.grace_seconds(raw) == expected


# ---------------------------------------------------------------------------
# WAL assertion — what separates a real stop from a reported one
# ---------------------------------------------------------------------------


def test_clean_stop_leaves_no_wal():
  assert su.wal_is_clean(["/c/jellyfin/data/data/jellyfin.db"]) is True


def test_dirty_wal_is_caught():
  """`docker compose stop` returned 0 here. The WAL is the only witness."""
  assert su.wal_is_clean([
    "/c/jellyfin/data/data/jellyfin.db",
    "/c/jellyfin/data/data/jellyfin.db-wal",
  ]) is False


def test_shm_alone_also_counts_as_dirty():
  assert su.wal_is_clean(["/c/x.db", "/c/x.db-shm"]) is False


def test_empty_listing_is_clean():
  assert su.wal_is_clean([]) is True


# ---------------------------------------------------------------------------
# Action / plan
# ---------------------------------------------------------------------------


def _model():
  return {
    "jellyfin": {"image": "lscr.io/linuxserver/jellyfin:12.0ubu2604-ls48"},
    "sonarr": {"image": "lscr.io/linuxserver/sonarr:latest"},
    "swag": {"image": "lscr.io/linuxserver/swag:latest"},
    "tinyauth": {"image": "ghcr.io/tinyauthapp/tinyauth:v5.2.0"},
    "4eva-rootpage": {"image": "nas/4eva-rootpage:latest",
                      "build": {"context": "."}},
  }


def test_plan_marks_only_behind_services():
  newest = {"jellyfin": "12.1ubu2604-ls49", "sonarr": "latest",
            "swag": "latest", "tinyauth": "v5.2.0", "4eva-rootpage": None}
  plan = su.build_plan(_model(), newest)
  behind = {a.service for a in plan if a.behind}
  assert behind == {"jellyfin"}


def test_plan_orders_blast_radius_services_last():
  """swag and tinyauth move after the *arr apps, so a halt breaks less."""
  newest = dict.fromkeys(_model(), None)
  order = [a.service for a in su.build_plan(_model(), newest)]
  assert order.index("sonarr") < order.index("tinyauth")
  assert order.index("jellyfin") < order.index("swag")


def test_plan_can_be_limited_to_one_service():
  newest = dict.fromkeys(_model(), None)
  plan = su.build_plan(_model(), newest, only={"jellyfin"})
  assert [a.service for a in plan] == ["jellyfin"]


def test_plan_can_be_limited_by_kind():
  newest = dict.fromkeys(_model(), None)
  plan = su.build_plan(_model(), newest, kinds={Kind.PINNED})
  assert {a.service for a in plan} == {"jellyfin", "tinyauth"}


def test_one_way_action_demands_a_proven_backup():
  a = su.Action("jellyfin", Kind.PINNED, "12.0ubu2604-ls48",
                "12.1ubu2604-ls49", Store.SQLITE, one_way=True)
  assert a.behind is True
  assert a.needs_proof is True


def test_a_current_one_way_service_demands_nothing():
  a = su.Action("jellyfin", Kind.PINNED, "12.1ubu2604-ls49",
                "12.1ubu2604-ls49", Store.SQLITE, one_way=True)
  assert a.behind is False
  assert a.needs_proof is False


def test_describe_names_the_rollback_risk_and_the_dependants():
  a = su.Action("tinyauth", Kind.PINNED, "v5.2.0", "v5.3.0",
                Store.SQLITE, one_way=True, dependants=("swag",))
  text = a.describe()
  assert "ONE-WAY" in text
  assert "swag" in text


# ---------------------------------------------------------------------------
# verdict — the exit contract
# ---------------------------------------------------------------------------


def test_clean_run_is_zero():
  assert su.verdict(applied=3, skipped=0, failed=0) == 0


def test_skips_are_partial():
  assert su.verdict(applied=1, skipped=2, failed=0) == 1


def test_a_failure_is_fatal_not_partial():
  """Exit 1 cannot alert: cron_job.py treats 0 and 1 as fine (ADR-0003)."""
  assert su.verdict(applied=0, skipped=0, failed=1) == 2


def test_a_failure_outranks_skips():
  assert su.verdict(applied=5, skipped=5, failed=1) == 2


def test_nothing_to_do_is_zero():
  assert su.verdict(applied=0, skipped=0, failed=0) == 0


# ---------------------------------------------------------------------------
# is_floating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
  "tag,expected",
  [("latest", True), ("nightly", True), ("nightly-2026091410ubu2604-ls101", True),
   ("main", True), ("v1.2.3", False), ("12.1ubu2604-ls49", False),
   ("8", False), ("pg17-v0.4.1", False)],
)
def test_is_floating(tag, expected):
  assert su.is_floating(tag) is expected


# ---------------------------------------------------------------------------
# tag_of / repo_of — a colon is not always a tag separator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
  "image,tag,repo",
  [
    ("lscr.io/linuxserver/sonarr:latest", "latest", "lscr.io/linuxserver/sonarr"),
    ("nginx", "", "nginx"),
    ("nginx:1.25", "1.25", "nginx"),
    # the case that classified an untagged image as pinned
    ("registry.local:5000/app", "", "registry.local:5000/app"),
    ("registry.local:5000/app:v2", "v2", "registry.local:5000/app"),
    ("ghcr.io/analogj/scrutiny:v0.9.4-omnibus", "v0.9.4-omnibus",
     "ghcr.io/analogj/scrutiny"),
  ],
)
def test_tag_and_repo_split(image, tag, repo):
  assert su.tag_of(image) == tag
  assert su.repo_of(image) == repo


def test_repo_of_round_trips_with_tag_of():
  for image in ("a/b:c", "a", "h:1/a:b", "h:1/a"):
    tag = su.tag_of(image)
    rebuilt = su.repo_of(image) + (f":{tag}" if tag else "")
    assert rebuilt == image


# ---------------------------------------------------------------------------
# Postgres — credentials come from the model, and a major is not a restart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
  "tag,major",
  [("pg16", 16), ("pg17-v0.4.1", 17), ("pg18-v1.1.1", 18), ("18", 18),
   ("18-alpine", 18), ("v2.20.0", None), ("latest", None), ("", None)],
)
def test_pg_major_parsing(tag, major):
  assert su.pg_major(tag) == major


def test_a_major_change_is_detected():
  """pg16 -> pg18 needs dump+restore: the new binary refuses the old PGDATA."""
  assert su.is_pg_major_change("pg16", "pg18-v1.1.1") is True
  assert su.is_pg_major_change("pg17-v0.4.1", "pg18-v1.1.1") is True


def test_a_point_release_on_the_same_major_is_not():
  """Same major, new extension build — a plain restart, no cut-over."""
  assert su.is_pg_major_change("pg17-v0.4.1", "pg17-v1.1.1") is False


def test_a_non_postgres_tag_pair_is_never_a_major_change():
  assert su.is_pg_major_change("v2.18.1", "v2.20.0") is False
  assert su.is_pg_major_change("latest", "latest") is False


def test_credentials_come_from_the_resolved_model():
  """`docker compose config` substitutes .env, so refusing to read them and
  demanding they be passed in was a missing feature, not a safety measure."""
  svc = {"environment": {"POSTGRES_USER": "streamystats",
                         "POSTGRES_PASSWORD": "s3cret",
                         "POSTGRES_DB": "streamystats"}}
  assert su.pg_env(svc) == ("streamystats", "s3cret", "streamystats")


def test_list_form_environment_is_handled():
  svc = {"environment": ["POSTGRES_USER=u", "POSTGRES_PASSWORD=p", "TZ=UTC"]}
  user, pw, db = su.pg_env(svc)
  assert (user, pw) == ("u", "p")
  assert db == "u"  # POSTGRES_DB defaults to the user, as Postgres itself does


def test_missing_environment_falls_back_to_postgres_defaults():
  assert su.pg_env({}) == ("postgres", "", "postgres")


def test_pgdata_is_read_from_the_bind_mount_not_a_convention():
  svc = {"volumes": [
    {"type": "bind", "source": "/etc/localtime", "target": "/etc/localtime"},
    {"type": "bind", "source": "/c/streamystats-db",
     "target": "/var/lib/postgresql/data"},
  ]}
  assert su.pgdata_path(svc) == Path("/c/streamystats-db")


def test_no_pgdata_mount_is_reported_rather_than_guessed():
  assert su.pgdata_path({"volumes": []}) is None
  assert su.pgdata_path({}) is None


# ---------------------------------------------------------------------------
# bump_image_line — the step whose absence made every pinned bump a no-op
# ---------------------------------------------------------------------------


COMPOSE = """  streamystats-db:
    # NOTE: pinned, see tensorchord/vchord-postgres:pg17-v0.4.1 upstream
    image: tensorchord/vchord-postgres:pg17-v0.4.1
    container_name: streamystats-db
"""


def test_the_image_line_is_rewritten():
  out, n = su.bump_image_line(
    COMPOSE, "tensorchord/vchord-postgres:pg17-v0.4.1",
    "tensorchord/vchord-postgres:pg18-v1.1.1")
  assert n == 1
  assert "image: tensorchord/vchord-postgres:pg18-v1.1.1" in out


def test_a_mention_in_a_comment_is_left_alone():
  """A bare substring replace would rewrite prose and the diun manifest."""
  out, _ = su.bump_image_line(
    COMPOSE, "tensorchord/vchord-postgres:pg17-v0.4.1",
    "tensorchord/vchord-postgres:pg18-v1.1.1")
  assert "# NOTE: pinned, see tensorchord/vchord-postgres:pg17-v0.4.1 upstream" in out


def test_indentation_is_preserved():
  out, _ = su.bump_image_line(COMPOSE, "tensorchord/vchord-postgres:pg17-v0.4.1",
                              "tensorchord/vchord-postgres:pg18-v1.1.1")
  assert "\n    image: tensorchord/vchord-postgres:pg18-v1.1.1\n" in out


def test_no_match_reports_zero_rather_than_silently_succeeding():
  """0 hits must be a refusal: the pull would be real and the recreate a no-op."""
  _, n = su.bump_image_line(COMPOSE, "lscr.io/linuxserver/sonarr:latest", "x:1")
  assert n == 0


def test_two_services_sharing_an_image_are_counted():
  """beszel/beszel-agent shape: the caller refuses rather than moving both."""
  text = "    image: a/b:1\n    image: a/b:1\n"
  _, n = su.bump_image_line(text, "a/b:1", "a/b:2")
  assert n == 2


def test_a_tag_that_is_a_prefix_of_another_does_not_match_it():
  text = "    image: a/b:1.2\n    image: a/b:1.20\n"
  out, n = su.bump_image_line(text, "a/b:1.2", "a/b:9")
  assert n == 1
  assert "image: a/b:1.20" in out


# ---------------------------------------------------------------------------
# Postgres 18 moved the data layout — a tag bump alone crash-loops
# ---------------------------------------------------------------------------


def test_pg18_needs_the_mount_moved_to_the_parent():
  """Measured: pg18 crash-looped with the tag bumped and the mount untouched."""
  assert su.needs_parent_mount(18, "/var/lib/postgresql/data") is True
  assert su.needs_parent_mount(19, "/var/lib/postgresql/data") is True


def test_pg17_does_not():
  assert su.needs_parent_mount(17, "/var/lib/postgresql/data") is False
  assert su.needs_parent_mount(16, "/var/lib/postgresql/data") is False


def test_an_already_moved_mount_is_not_moved_again():
  assert su.needs_parent_mount(18, "/var/lib/postgresql") is False


def test_an_unparseable_major_does_not_trigger_a_mount_change():
  assert su.needs_parent_mount(None, "/var/lib/postgresql/data") is False


VOLUMES = """    volumes:
      - /etc/localtime:/etc/localtime:ro
      - ${CONFIG_DIRECTORY}/streamystats-db:/var/lib/postgresql/data
"""


def test_the_mount_line_is_rewritten_in_raw_compose_text():
  """The source is still `${CONFIG_DIRECTORY}/...` in the file; only the model
  has it resolved, so the rewrite must match the unexpanded form."""
  out, n = su.rewrite_pgdata_mount(VOLUMES, "streamystats-db")
  assert n == 1
  assert "${CONFIG_DIRECTORY}/streamystats-db:/var/lib/postgresql\n" in out
  assert "/var/lib/postgresql/data" not in out


def test_other_mounts_are_untouched():
  out, _ = su.rewrite_pgdata_mount(VOLUMES, "streamystats-db")
  assert "- /etc/localtime:/etc/localtime:ro" in out


def test_a_different_service_does_not_match():
  _, n = su.rewrite_pgdata_mount(VOLUMES, "playlist-generator-db")
  assert n == 0


def test_pgdata_path_accepts_both_layouts():
  legacy = {"volumes": [{"type": "bind", "source": "/c/db",
                         "target": "/var/lib/postgresql/data"}]}
  parent = {"volumes": [{"type": "bind", "source": "/c/db",
                         "target": "/var/lib/postgresql"}]}
  assert su.pgdata_path(legacy) == Path("/c/db")
  assert su.pgdata_path(parent) == Path("/c/db")


def test_a_match_on_the_final_line_keeps_its_newline():
  """`\\s*$` is greedy across newlines: it eats the terminator on a last-line
  match and welds the rewritten line to whatever comes next."""
  text = "    volumes:\n      - ${C}/db:/var/lib/postgresql/data\n"
  out, n = su.rewrite_pgdata_mount(text, "db")
  assert n == 1
  assert out.endswith(":/var/lib/postgresql\n")

  img = "    image: a/b:1\n"
  out2, n2 = su.bump_image_line(img, "a/b:1", "a/b:2")
  assert n2 == 1
  assert out2 == "    image: a/b:2\n"
