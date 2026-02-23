"""flatten-internal-urls — h2c transform.

Strips Docker Compose network aliases and rewrites K8s FQDNs to short
compose service names. Restores nerdctl compatibility (nerdctl silently
ignores network aliases) and simplifies compose DNS resolution.

Note: cert-manager declares incompatibility with this transform.
"""

import os
import re


# K8s internal DNS → short service name (same pattern as h2c-core _K8S_DNS_RE)
_K8S_DNS_RE = re.compile(
    r'([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.'       # service name (captured)
    r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.'       # namespace (discarded)
    r'svc(?:\.cluster\.local)?'                    # svc[.cluster.local]
)


def _rewrite_k8s_dns(text):
    """Replace <svc>.<ns>.svc.cluster.local with just <svc>."""
    return _K8S_DNS_RE.sub(r'\1', text)


def _apply_alias_map(text, alias_map):
    """Replace K8s Service names with compose service names in hostname positions.

    Matches aliases preceded by :// or @ (URLs, Redis URIs) and followed by
    / : whitespace, quotes, or end-of-string — so only hostnames are affected,
    not substrings like bucket names.
    """
    for alias, target in alias_map.items():
        text = re.sub(
            r'(?<=[/@])'
            + re.escape(alias)
            + r'''(?=[/:\s"']|$)''',
            target,
            text,
        )
    return text


def _rewrite_text(text, alias_map):
    """Apply FQDN flattening + alias map resolution to a string."""
    text = _rewrite_k8s_dns(text)
    if alias_map:
        text = _apply_alias_map(text, alias_map)
    return text


def _strip_aliases(compose_services):
    """Remove network aliases from all compose services."""
    for svc in compose_services.values():
        networks = svc.get("networks")
        if isinstance(networks, dict):
            for net_cfg in networks.values():
                if isinstance(net_cfg, dict):
                    net_cfg.pop("aliases", None)
            if all(not v for v in networks.values()):
                del svc["networks"]


def _rewrite_env(compose_services, alias_map):
    """Rewrite FQDN references in environment variables."""
    for svc in compose_services.values():
        env = svc.get("environment")
        if not env or not isinstance(env, dict):
            continue
        for key in list(env):
            val = env[key]
            if isinstance(val, str):
                rewritten = _rewrite_text(val, alias_map)
                if rewritten != val:
                    env[key] = rewritten


def _rewrite_configmap_files(output_dir, alias_map):
    """Rewrite FQDN references in configmap files on disk."""
    cm_dir = os.path.join(output_dir, "configmaps")
    if not os.path.isdir(cm_dir):
        return
    for root, _dirs, files in os.walk(cm_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            rewritten = _rewrite_text(content, alias_map)
            if rewritten != content:
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(rewritten)


def _rewrite_ingress_entries(ingress_entries, alias_map):
    """Rewrite FQDN upstreams and server_sni in ingress entries."""
    for entry in ingress_entries:
        upstream = entry.get("upstream", "")
        # FQDN flattening first
        rewritten = _rewrite_k8s_dns(upstream)
        # Upstream is bare host:port — extract host, resolve alias, rebuild
        if ":" in rewritten:
            host, port = rewritten.rsplit(":", 1)
            resolved = alias_map.get(host, host)
            rewritten = f"{resolved}:{port}"
        else:
            rewritten = alias_map.get(rewritten, rewritten)
        if rewritten != upstream:
            entry["upstream"] = rewritten

        sni = entry.get("server_sni", "")
        if sni:
            rewritten = _rewrite_k8s_dns(sni)
            if rewritten != sni:
                entry["server_sni"] = rewritten


class FlattenInternalUrls:
    """Strip network aliases and rewrite FQDNs to short Docker names."""

    name = "flatten-internal-urls"
    priority = 2000  # run after other transforms

    def transform(self, compose_services, ingress_entries, ctx):
        """Flatten all K8s FQDNs to short compose service names."""
        _strip_aliases(compose_services)
        _rewrite_env(compose_services, ctx.alias_map)
        _rewrite_configmap_files(ctx.output_dir, ctx.alias_map)
        _rewrite_ingress_entries(ingress_entries, ctx.alias_map)
