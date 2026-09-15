# ADR-0045 — An unrouted hostname answers 404, and `default.conf` is tracked

**Date:** 2026-09-15
**Status:** accepted
**Extends:** ADR-0022 — proxy-confs are tracked, and the conf is the mechanism

## What happened

Navidrome and AdGuard Home were added and their proxy-confs written. Before
`make swag-apply` had run, `navidrome.4eva.me` was opened in a browser and answered
**`200 Welcome to your SWAG instance`**. The access log dates it precisely:

```
GET navidrome.4eva.me   18:49:07 +0200  =  16:49:07 UTC
swag recreated with the new conf        =  16:54:41 UTC
```

Nothing was broken — the route simply was not deployed yet, and five minutes later it
answered `302` to the login page as designed. But the _symptom of not being deployed_ was a
cheerful, successful-looking page, which is indistinguishable in a browser from a working
service.

This is the same failure as ADR-0022's `lingarr`, which carried a `swag=enable` label and no
conf and therefore "worked" — it answered `200` from this same default vhost for as long as
nobody looked closely.

## Decision 1 — the default vhost returns 404

Stock SWAG's `server_name _` block serves `/config/www/index.html` with a `200`. It now
returns `404` with a small page naming the host that was asked for.

The value is that the four things a hostname can say are now distinct, and each names its
own cause:

| Answer | Meaning                                          |
| ------ | ------------------------------------------------ |
| `302`  | the tinyauth door — the route exists (ADR-0034)  |
| `500`  | the door is jammed shut; tinyauth is unreachable |
| `502`  | the route exists and its upstream is down        |
| `404`  | **there is no such route here at all**           |

It also stops advertising the name of the software running here to anyone who probes a
random subdomain, which is a small gain and free.

## Decision 2 — `site-confs/default.conf` is tracked in this repo

It was not, and it is load-bearing twice over: the `include proxy-confs/*.subdomain.conf`
at the bottom of it is what publishes **every** route in the stack, and the `server_name _`
block is what answers everything else. It existed only in the gitignored config directory,
where the nightly backup was the sole thing keeping it — with local edits already applied
(a `.bak` from 2026-09-01 sits beside it).

It is now bind-mounted read-only like every other tracked conf, so the ADR-0022 inode rule
applies to it: apply a change with `make swag-apply`, never `nginx -s reload`.
`check-swag-conf-drift.sh` learned a `swag/site-confs/*` case and now covers 23 confs.

## Decision 3 — swag's healthcheck must address a real vhost

This is the trap the change sets for the next person, and it is asserted rather than
remembered. swag's healthcheck was:

```yaml
test: [CMD, curl, -f, 'https://localhost:443', -k]
```

No `Host` header, so it lands on the default vhost — the one that now returns `404`. `curl
-f` turns a `404` into a non-zero exit, so **swag reports unhealthy forever while nginx
serves every real route perfectly**, and `make swag-apply` then waits for a health state
that can never arrive. It is now:

```yaml
test: [CMD, curl, -f, -k, -H, 'Host: ${PUBLIC_DOMAIN}', 'https://localhost:443']
```

which also proves more than the old one did: that a real vhost is being served, not merely
that something is listening on 443. `make check`'s `swag-healthcheck-host` fails if the Host
header is ever dropped while the probe still targets `localhost:443`.

## Implementation note that cost an outage

The first version held the page body in a `map` and split it across lines as adjacent
quoted strings, C-style. **nginx does not concatenate adjacent quoted strings.** It failed
with:

```
nginx: [emerg] invalid number of the map parameters in .../default.conf:43
```

nginx then refused to start at all, so the entire public surface was down until it was
fixed — the blast radius of an edit to this particular file is everything.

Two lessons, both now in the conf as comments:

- The body is **one** single-quoted string. Single quotes so the HTML can use double
  quotes.
- It is inlined in `return`, not held in a `map`. A `map` value is a static string and
  would **not** have expanded `$host`; `return` does.

And the procedural one: because the file is bind-mounted by inode and a Python
`open(path, 'w')` truncates in place rather than replacing the inode, the container sees
such an edit immediately. So `docker exec swag nginx -t` validates a change to a mounted
conf **before** recreating anything. Use it — a recreate with a broken conf is an outage,
and a syntax check is free.
