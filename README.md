# flatten-internal-urls

![vibe coded](https://img.shields.io/badge/vibe-coded-ff69b4)
![python 3](https://img.shields.io/badge/python-3-3776AB)
![heresy: NaN/10](https://img.shields.io/badge/heresy-NaN%2F10-blueviolet)

dekube transform that strips Docker Compose network aliases and rewrites K8s FQDNs to short compose service names.

## Why

dekube-engine v2.1+ uses Docker network aliases for K8s DNS resolution — each service gets `networks.default.aliases` with FQDN variants (`svc.ns.svc.cluster.local`, `svc.ns.svc`, `svc.ns`). This preserves cert SANs and works transparently with Docker Compose.

**nerdctl compose silently ignores aliases.** Services referencing other services by FQDN fail to connect. This transform fixes that by reverting to the pre-v2.1 approach: strip all aliases and rewrite FQDNs to short compose names that nerdctl resolves natively.

Beyond nerdctl, flattening also produces cleaner compose output — no alias blocks, no `keycloak.auth.svc.cluster.local` in environment variables when `keycloak` would do.

## What it does

1. **Strips `networks.default.aliases`** from all compose services — unless a short alias was found in content we couldn't safely rewrite (see below), in which case it's kept instead of removed
2. **Rewrites FQDN references** in environment variables (`svc.ns.svc.cluster.local` → `svc`)
3. **Rewrites FQDN references** in `command`/`entrypoint` list items — narrower than env/ConfigMap text: only an actual `scheme://host` or `@host` URL position, or a bare `host:<port>` with a real numeric port. A path segment (`/usr/local/bin/api`) or a bare word with no port (`nc -z redis 6379`) is never touched, since argv has no reliable way to tell a hostname from a path or a positional argument.
4. **Rewrites FQDN references** in ConfigMap files on disk
5. **Rewrites FQDN upstreams** in Caddy entries
6. **Resolves K8s Service aliases** to compose service names (e.g. `keycloak-service` → `keycloak`) in URL host position — after `//` or `@`, so `http://gw/api/v1` keeps its `/api/` path segment (engines up to v1.7.0 rewrote it too) —, including bare `host:<port>` references with a real numeric port and no scheme (word-boundary safe — `docs-media-bucket` never matches alias `docs-media`, `redis://...` is never mistaken for a `redis:<port>` host since the port is mandatory, and an image path like `docker.io/library/redis:7` or an IPv6 hex group like `fd00::db:5432` are excluded too, since a `/` or `:` immediately to the left is never a real bare host)

A **bare word with no port** (`CACHE_DRIVER=redis`, a YAML key `redis:`, a scheme name `redis://`, a lone argv token) is **never rewritten** — there's no reliable way to tell a real hostname reference from a config key, a URL scheme, or an unrelated word once there's no port attached. Its alias is kept instead of stripped, so DNS resolution for it still works. Binary ConfigMap files (base64 `binaryData`) get the same treatment for the same reason — a different-length text replacement would corrupt the format, so an alias found in raw bytes there is kept too rather than guessed at.

`postgres:16` (an image tag) and `IMAGE=redis:7` are still indistinguishable from a real `host:port` when nothing at all (or a generic separator like `=`) precedes them — marked `# CBA:` in `_alias_port_pattern` with the upgrade path (restrict to known hostname-bearing fields instead of scanning arbitrary text). Rare in practice: it needs an `alias_map` key that also happens to be an image name.

## Install

```bash
python3 dekube-manager.py flatten-internal-urls
```

Or add to `dekube.yaml`:

```yaml
depends:
  - flatten-internal-urls
```

## Usage

The transform is loaded automatically via `--extensions-dir`. No configuration needed — it processes everything.

```bash
# Via dekube-manager run mode
python3 dekube-manager.py run -e compose

# Manual
python3 helmfile2compose.py --from-dir /tmp/rendered \
  --extensions-dir .dekube/extensions --output-dir .
```

Verify it loaded: `Loaded transforms: FlattenInternalUrls` appears on stderr.

## Priority

2000 (runs after other transforms).

## Code quality

*Last updated: 2026-09-24*

| Metric | Value |
|--------|-------|
| Pylint | 9.35/10 |
| Pyflakes | clean |
| Radon MI | 55.10 (A) |
| Radon avg CC | 4.8 (A) |

Worst CC: `_strip_aliases` (11, C). No function rated D or worse.

Most of the pylint gap is `E0401: Unable to import 'dekube'`, which this file doesn't suppress inline — extensions import from dekube-engine at runtime, not at lint time.

## License

Public domain.
