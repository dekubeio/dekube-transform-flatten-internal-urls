"""flatten-internal-urls — dekube transform.

Strips Docker Compose network aliases and rewrites K8s FQDNs to short
compose service names. Restores nerdctl compatibility (nerdctl silently
ignores network aliases) and simplifies compose DNS resolution.

Note: cert-manager declares incompatibility with this transform.
"""

import os
import re

from dekube import apply_alias_map, rewrite_k8s_dns

# Chars that make a bare-host match ambiguous when glued to either side —
# same care as the engine's own DNS/alias regexes (apply_alias_map, _K8S_DNS_RE).
_HOST_CHAR = r'[A-Za-z0-9_.-]'


class FlattenInternalUrls:  # pylint: disable=too-few-public-methods  # contract: one class, one method
    """Strip network aliases and rewrite FQDNs to short Docker names."""

    name = "flatten-internal-urls"
    priority = 2000  # run after other transforms

    @staticmethod
    def _bare_host_pattern(alias):
        """Boundary-safe regex for a bare ``alias`` or ``alias:port`` token.

        apply_alias_map only rewrites aliases preceded by ``/`` or ``@`` (URL/URI
        hostname positions) — a bare host with no scheme is never touched. This
        covers that gap, with word boundaries so e.g. "docs-media-bucket" never
        matches alias "docs-media".
        """
        return re.compile(rf'(?<!{_HOST_CHAR}){re.escape(alias)}(?P<port>:\d+)?(?!{_HOST_CHAR})')

    @staticmethod
    def _rewrite_bare_hosts(text, alias_map):
        """Rewrite bare ``alias``/``alias:port`` tokens (no scheme, no ``@``) in text."""
        for alias, target in (alias_map or {}).items():
            if alias in text:
                text = FlattenInternalUrls._bare_host_pattern(alias).sub(
                    lambda m, _target=target: _target + (m.group("port") or ""), text)
        return text

    @staticmethod
    def _rewrite_text(text, alias_map):
        """Apply FQDN flattening + alias map resolution (scheme-based and bare) to a string."""
        text = rewrite_k8s_dns(text)
        if alias_map:
            text = apply_alias_map(text, alias_map)
            text = FlattenInternalUrls._rewrite_bare_hosts(text, alias_map)
        return text

    @staticmethod
    def _strip_aliases(compose_services, unsafe_aliases):
        """Remove network aliases from all compose services.

        A short alias flagged in ``unsafe_aliases`` (a bare-host reference found in
        content we couldn't safely rewrite, e.g. binary configmap data) is kept
        instead of stripped — a redundant alias beats a silently broken hostname.
        """
        for svc in compose_services.values():
            networks = svc.get("networks")
            if isinstance(networks, dict):
                for net_cfg in networks.values():
                    if isinstance(net_cfg, dict):
                        aliases = net_cfg.get("aliases")
                        if aliases:
                            kept = [a for a in aliases if a in unsafe_aliases]
                            if kept:
                                net_cfg["aliases"] = kept
                            else:
                                net_cfg.pop("aliases", None)
                if all(not v for v in networks.values()):
                    del svc["networks"]

    @staticmethod
    def _rewrite_env(compose_services, alias_map):
        """Rewrite FQDN/alias references in environment variables."""
        for svc in compose_services.values():
            env = svc.get("environment")
            if not env or not isinstance(env, dict):
                continue
            for key in list(env):
                val = env[key]
                if isinstance(val, str):
                    rewritten = FlattenInternalUrls._rewrite_text(val, alias_map)
                    if rewritten != val:
                        env[key] = rewritten

    @staticmethod
    def _rewrite_command_args(compose_services, alias_map):
        """Rewrite FQDN/alias references inside ``command``/``entrypoint`` list items."""
        for svc in compose_services.values():
            for key in ("command", "entrypoint"):
                items = svc.get(key)
                if not isinstance(items, list):
                    continue
                for i, item in enumerate(items):
                    if isinstance(item, str):
                        rewritten = FlattenInternalUrls._rewrite_text(item, alias_map)
                        if rewritten != item:
                            items[i] = rewritten

    @staticmethod
    def _rewrite_configmap_files(output_dir, alias_map, unsafe_aliases):
        """Rewrite FQDN/alias references in configmap files on disk.

        Binary files (from base64 ``binaryData``) can't be safely text-rewritten —
        a different-length replacement would corrupt the format. If one contains an
        alias's raw bytes, flag it unsafe instead of guessing.
        """
        cm_dir = os.path.join(output_dir, "configmaps")
        if not os.path.isdir(cm_dir):
            return
        for root, _dirs, files in os.walk(cm_dir):
            for fname in files:
                fpath = os.path.join(root, fname)
                if os.path.islink(fpath):
                    continue
                with open(fpath, "rb") as f:
                    raw = f.read()
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    for alias in (alias_map or {}):
                        if alias.encode("utf-8") in raw:
                            unsafe_aliases.add(alias)
                    continue  # skip binary files
                rewritten = FlattenInternalUrls._rewrite_text(content, alias_map)
                if rewritten != content:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(rewritten)

    @staticmethod
    def _rewrite_ingress_entries(ingress_entries, alias_map):
        """Rewrite FQDN upstreams and server_sni in ingress entries."""
        for entry in ingress_entries:
            upstream = entry.get("upstream") or ""
            # FQDN flattening first
            rewritten = rewrite_k8s_dns(upstream)
            # Upstream is bare host:port — extract host, resolve alias, rebuild
            if ":" in rewritten:
                host, port = rewritten.rsplit(":", 1)
                resolved = alias_map.get(host, host)
                rewritten = f"{resolved}:{port}"
            else:
                rewritten = alias_map.get(rewritten, rewritten)
            if rewritten != upstream:
                entry["upstream"] = rewritten

            sni = entry.get("server_sni") or ""
            if sni:
                rewritten = rewrite_k8s_dns(sni)
                if rewritten != sni:
                    entry["server_sni"] = rewritten

    def transform(self, compose_services, ingress_entries, ctx):
        """Flatten all K8s FQDNs to short compose service names."""
        unsafe_aliases = set()
        self._rewrite_env(compose_services, ctx.alias_map)
        self._rewrite_command_args(compose_services, ctx.alias_map)
        self._rewrite_configmap_files(ctx.output_dir, ctx.alias_map, unsafe_aliases)
        self._rewrite_ingress_entries(ingress_entries, ctx.alias_map)
        self._strip_aliases(compose_services, unsafe_aliases)
