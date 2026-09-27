#!/usr/bin/env python3
"""Converge both Bookshelf instances, and their Prowlarr apps, to ADR-0057.

Bookshelf (the Readarr revival) keeps everything that matters in its own
SQLite: the root folder, the qBittorrent client and its category, the remote
path mapping, the format ranking, the login. None of it is in git, and a
restored or fresh ``${CONFIG_DIRECTORY}/bookshelf`` would come back as a blank
Readarr that downloads nothing. This declares that state and applies it
idempotently, the same way ``configure_arr_notifications.py`` does for
connectors (which owns the ntfy side of Bookshelf -- not repeated here).

Per instance
------------
* Forms login: ``TINYAUTH_USER`` + ``BOOKSHELF_PASSWORD``, like every *arr
  behind the door. The password is write-only through the API, so ``--check``
  can assert the method and the username but never the password itself.
* Root folder ``/data/books/{ebooks,audiobooks}`` -- inside the one ``/data``
  mount, so an import is a rename or a hardlink (ADR-0002).
* qBittorrent at ``qbittorrent:8080``, category ``arr-bookshelf[-audio]``, plus
  the ``/downloads/`` -> ``/data/downloads/`` remote path mapping every *arr
  here needs (qBittorrent mounts only the downloads dir).
* Format ranking. Ebooks: EPUB best and the cutoff (Jellyfin's reader and every
  e-reader app take it), then MOBI, AZW3, and PDF as a last resort. Audio: M4B
  best and the cutoff, then FLAC, MP3. Upgrades on.
* Metadata profile: English and Dutch (``nld``) editions, plus editions with
  no language.

Prowlarr
--------
One Readarr-type application per instance, with category sets that do not
overlap -- 7000/7020 (Books, Books/EBook) for ebooks, 3030 (Audio/Audiobook)
for audio -- so the ebook instance is never offered an audiobook and vice versa.

Exit codes
----------
  0  everything matches (or was brought to match)
  1  something differs (with --check) or a write failed
  2  fatal: a required env var is unset, or an app is unreachable

Usage
-----
  python scripts/bookshelf_setup.py --check   # assert only (verify-runtime)
  python scripts/bookshelf_setup.py --apply   # converge
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

PROWLARR = "http://localhost:9696/api/v1"
# In-network names: Prowlarr pushes indexers to these, and Bookshelf reaches
# qBittorrent by them. Never localhost -- that is each container's own.
PROWLARR_IN_NETWORK = "http://prowlarr:9696"
QBIT_HOST = "qbittorrent"
QBIT_PORT = 8080
REMOTE_PATH = "/downloads/"
LOCAL_PATH = "/data/downloads/"
# ISO 639-3 only: "dut" (the 639-2/B code) is rejected as "Unknown languages".
LANGUAGES = "eng, nld, null"


@dataclass(frozen=True)
class Instance:
  name: str               # compose service name, also the in-network host
  title: str              # the Prowlarr app and root folder name
  instance_name: str      # UPSTREAM VALIDATES it must contain "Readarr"
  base: str               # host-side API base
  key_env: str
  root: str
  category: str
  profile: str            # quality profile name as the image ships it
  ranking: tuple[str, ...]  # allowed qualities, WORST first (API order)
  cutoff: str
  prowlarr_categories: tuple[int, ...]


INSTANCES: tuple[Instance, ...] = (
  Instance(
    "bookshelf", "Bookshelf", "Readarr (Bookshelf)", "http://localhost:8787/api/v1", "API_KEY_BOOKSHELF",
    "/data/books/ebooks", "arr-bookshelf", "eBook",
    ("PDF", "AZW3", "MOBI", "EPUB"), "EPUB", (7000, 7020),
  ),
  Instance(
    "bookshelf-audio", "Bookshelf Audio", "Readarr (Bookshelf Audio)",
    "http://localhost:8788/api/v1",
    "API_KEY_BOOKSHELF_AUDIO", "/data/books/audiobooks", "arr-bookshelf-audio",
    "Spoken", ("MP3", "FLAC", "M4B"), "M4B", (3030,),
  ),
)

REQUIRED_ENV = (
  "API_KEY_BOOKSHELF", "API_KEY_BOOKSHELF_AUDIO", "API_KEY_PROWLARR",
  "BOOKSHELF_PASSWORD", "TINYAUTH_USER", "QBITTORRENT_USER", "QBITTORRENT_PASS",
)


@dataclass
class Report:
  differs: list[str] = field(default_factory=list)
  fixed: list[str] = field(default_factory=list)
  failed: list[str] = field(default_factory=list)


# --- transport -------------------------------------------------------------


def _request(method: str, url: str, key: str, payload: object | None = None):
  data = json.dumps(payload).encode() if payload is not None else None
  req = urllib.request.Request(url, data=data, method=method, headers={
    "X-Api-Key": key, "Content-Type": "application/json", "Accept": "application/json",
  })
  with urllib.request.urlopen(req, timeout=60) as resp:
    body = resp.read()
  return json.loads(body) if body else None


def _fields(obj: dict) -> dict[str, object]:
  return {f["name"]: f.get("value") for f in obj.get("fields", [])}


def _set_fields(obj: dict, values: dict[str, object]) -> dict:
  out = dict(obj)
  out["fields"] = [
    {**f, "value": values[f["name"]]} if f["name"] in values else f
    for f in obj.get("fields", [])
  ]
  return out


# --- pure desired-state builders -------------------------------------------


def ranked_items(items: list[dict], ranking: tuple[str, ...]) -> list[dict]:
  """Allow exactly `ranking`, in that order at the END of the list (the API
  ranks later items higher); everything else disallowed and kept in front."""
  by_name = {i["quality"]["name"]: i for i in items if "quality" in i}
  missing = [q for q in ranking if q not in by_name]
  if missing:
    raise ValueError(f"profile has no quality named {missing}")
  rest = [dict(i, allowed=False) for i in items
          if "quality" not in i or i["quality"]["name"] not in ranking]
  return rest + [dict(by_name[q], allowed=True) for q in ranking]


def profile_matches(profile: dict, inst: Instance) -> bool:
  allowed = [i["quality"]["name"] for i in profile["items"]
             if "quality" in i and i.get("allowed")]
  names = {i["quality"]["id"]: i["quality"]["name"] for i in profile["items"] if "quality" in i}
  tail = [i["quality"]["name"] for i in profile["items"] if "quality" in i][-len(inst.ranking):]
  return (allowed == list(inst.ranking) and tail == list(inst.ranking)
          and names.get(profile["cutoff"]) == inst.cutoff and profile["upgradeAllowed"])


def desired_profile(profile: dict, inst: Instance) -> dict:
  items = ranked_items(profile["items"], inst.ranking)
  cutoff = next(i["quality"]["id"] for i in items
                if "quality" in i and i["quality"]["name"] == inst.cutoff)
  return {**profile, "items": items, "cutoff": cutoff, "upgradeAllowed": True}


def qbit_values(inst: Instance, env: dict[str, str]) -> dict[str, object]:
  return {
    "host": QBIT_HOST, "port": QBIT_PORT, "useSsl": False,
    "username": env["QBITTORRENT_USER"], "password": env["QBITTORRENT_PASS"],
    "musicCategory": inst.category,
  }


def prowlarr_values(inst: Instance, env: dict[str, str]) -> dict[str, object]:
  return {
    "prowlarrUrl": PROWLARR_IN_NETWORK,
    "baseUrl": f"http://{inst.name}:8787",
    "apiKey": env[inst.key_env],
    "syncCategories": list(inst.prowlarr_categories),
  }


# --- convergence -----------------------------------------------------------


def converge_instance(inst: Instance, env: dict[str, str], apply: bool, rep: Report) -> None:
  key = env[inst.key_env]

  def call(method: str, path: str, payload: object | None = None):
    return _request(method, inst.base + path, key, payload)

  def step(what: str, write) -> None:
    rep.differs.append(f"{inst.name}: {what}")
    if not apply:
      return
    try:
      write()
      rep.fixed.append(f"{inst.name}: {what}")
    except urllib.error.HTTPError as exc:
      rep.failed.append(f"{inst.name}: {what}: HTTP {exc.code} {exc.read()[:300]!r}")

  # 1. login + identity
  host = call("GET", "/config/host")
  domain = env.get("PUBLIC_DOMAIN", "")
  app_url = f"https://{inst.name}.{domain}" if domain else host.get("applicationUrl", "")
  if (host["authenticationMethod"] != "forms" or host["username"] != env["TINYAUTH_USER"]
      or host["instanceName"] != inst.instance_name or host.get("applicationUrl") != app_url):
    body = {**host, "authenticationMethod": "forms", "authenticationRequired": "enabled",
            "username": env["TINYAUTH_USER"], "password": env["BOOKSHELF_PASSWORD"],
            "passwordConfirmation": env["BOOKSHELF_PASSWORD"],
            "instanceName": inst.instance_name, "applicationUrl": app_url}
    step("host config (forms login, instance name, application URL)",
         lambda: call("PUT", f"/config/host/{host['id']}", body))

  # 2. quality ranking (before the root folder, which references the profile)
  profiles = call("GET", "/qualityprofile")
  profile = next((p for p in profiles if p["name"] == inst.profile), None)
  if profile is None:
    rep.failed.append(f"{inst.name}: no quality profile named {inst.profile!r}")
    return
  if not profile_matches(profile, inst):
    want = desired_profile(profile, inst)
    step(f"quality profile {inst.profile}: {' < '.join(inst.ranking)}, cutoff {inst.cutoff}",
         lambda: call("PUT", f"/qualityprofile/{profile['id']}", want))

  # 3. metadata profile languages
  meta = next((m for m in call("GET", "/metadataprofile") if m["name"] == "Standard"), None)
  if meta is not None and meta.get("allowedLanguages") != LANGUAGES:
    step(f"metadata profile languages -> {LANGUAGES}",
         lambda: call("PUT", f"/metadataprofile/{meta['id']}", {**meta, "allowedLanguages": LANGUAGES}))

  # 4. root folder
  roots = call("GET", "/rootfolder")
  if not any(r["path"].rstrip("/") == inst.root for r in roots):
    body = {"name": inst.title, "path": inst.root,
            "defaultQualityProfileId": profile["id"],
            "defaultMetadataProfileId": meta["id"] if meta else 1,
            "defaultMonitorOption": "all", "defaultNewItemMonitorOption": "all",
            "defaultTags": [], "isCalibreLibrary": False}
    step(f"root folder {inst.root}", lambda: call("POST", "/rootfolder", body))

  # 5. qBittorrent client. The password is masked on GET, so it is re-sent on
  #    every write, and a match is judged on everything else.
  clients = call("GET", "/downloadclient")
  qbit = next((c for c in clients if c["implementation"] == "QBittorrent"), None)
  want_fields = qbit_values(inst, env)
  if qbit is None or any(_fields(qbit).get(k) != v for k, v in want_fields.items()
                         if k != "password") or not qbit.get("enable"):
    if qbit is None:
      schema = next(s for s in call("GET", "/downloadclient/schema")
                    if s["implementation"] == "QBittorrent")
      body = _set_fields({**schema, "name": "qBittorrent", "enable": True,
                          "protocol": "torrent", "priority": 1,
                          "removeCompletedDownloads": False, "removeFailedDownloads": True,
                          "tags": []}, want_fields)
      step(f"qBittorrent client, category {inst.category}",
           lambda: call("POST", "/downloadclient", body))
    else:
      body = _set_fields({**qbit, "enable": True}, want_fields)
      step(f"qBittorrent client, category {inst.category}",
           lambda: call("PUT", f"/downloadclient/{qbit['id']}", body))

  # 6. remote path mapping
  maps = call("GET", "/remotepathmapping")
  if not any(m["host"] == QBIT_HOST and m["remotePath"] == REMOTE_PATH
             and m["localPath"] == LOCAL_PATH for m in maps):
    step(f"remote path mapping {QBIT_HOST}:{REMOTE_PATH} -> {LOCAL_PATH}",
         lambda: call("POST", "/remotepathmapping",
                      {"host": QBIT_HOST, "remotePath": REMOTE_PATH, "localPath": LOCAL_PATH}))


def converge_prowlarr(env: dict[str, str], apply: bool, rep: Report) -> None:
  key = env["API_KEY_PROWLARR"]
  apps = _request("GET", f"{PROWLARR}/applications", key)
  schema = next(s for s in _request("GET", f"{PROWLARR}/applications/schema", key)
                if s["implementation"] == "Readarr")
  for inst in INSTANCES:
    want = prowlarr_values(inst, env)
    have = next((a for a in apps if a["name"] == inst.title), None)
    matches = have is not None and have.get("syncLevel") == "fullSync" and all(
      _fields(have).get(k) == v for k, v in want.items() if k != "apiKey")
    if matches:
      continue
    what = f"Prowlarr app {inst.title!r} -> {want['baseUrl']} categories {want['syncCategories']}"
    rep.differs.append(f"prowlarr: {what}")
    if not apply:
      continue
    base = have if have is not None else {**schema, "name": inst.title, "tags": []}
    body = _set_fields({**base, "syncLevel": "fullSync"}, want)
    try:
      if have is None:
        _request("POST", f"{PROWLARR}/applications", key, body)
      else:
        _request("PUT", f"{PROWLARR}/applications/{have['id']}", key, body)
      rep.fixed.append(f"prowlarr: {what}")
    except urllib.error.HTTPError as exc:
      rep.failed.append(f"prowlarr: {what}: HTTP {exc.code} {exc.read()[:300]!r}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  mode = ap.add_mutually_exclusive_group(required=True)
  mode.add_argument("--check", action="store_true", help="assert only; exit 1 on any difference")
  mode.add_argument("--apply", action="store_true", help="converge")
  return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  env = dict(os.environ)
  missing = [k for k in REQUIRED_ENV if not env.get(k)]
  if missing:
    print(f"ERROR: unset in the environment: {missing} (run via `make bookshelf-setup`)",
          file=sys.stderr)
    return 2
  rep = Report()
  try:
    for inst in INSTANCES:
      converge_instance(inst, env, args.apply, rep)
    converge_prowlarr(env, args.apply, rep)
  except (OSError, urllib.error.URLError, ValueError) as exc:
    print(f"FATAL: {exc}", file=sys.stderr)
    return 2

  for line in rep.fixed:
    print(f"  fixed: {line}")
  for line in rep.failed:
    print(f"  FAILED: {line}", file=sys.stderr)
  if args.check:
    for line in rep.differs:
      print(f"  DIFFERS: {line}")
  if not rep.differs:
    print("  ok: both Bookshelf instances and their Prowlarr apps match ADR-0057")
  if rep.failed or (args.check and rep.differs):
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
