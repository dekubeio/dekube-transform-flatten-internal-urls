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

1. **Strips `networks.default.aliases`** from all compose services
2. **Rewrites FQDN references** in environment variables (`svc.ns.svc.cluster.local` → `svc`)
3. **Rewrites FQDN references** in ConfigMap files on disk
4. **Rewrites FQDN upstreams** in Caddy entries
5. **Resolves K8s Service aliases** to compose service names (e.g. `keycloak-service` → `keycloak`)

## Install

```bash
python3 dekube-manager.py flatten-internal-urls
```

Or add to `helmfile2compose.yaml`:

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

200 (runs after other transforms).

## Code quality

*Last updated: 2026-02-23*

| Metric | Value |
|--------|-------|
| Pylint | 10.00/10 |
| Pyflakes | clean |
| Radon MI | 63.89 (A) |
| Radon avg CC | 3.7 (A) |

No C-rated functions.

## License

Public domain.
