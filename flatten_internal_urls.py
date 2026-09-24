"""flatten-internal-urls — dekube transform.

Strips Docker Compose network aliases and rewrites K8s FQDNs to short
compose service names. Restores nerdctl compatibility (nerdctl silently
ignores network aliases) and simplifies compose DNS resolution.

Note: cert-manager declares incompatibility with this transform.
"""

import os
import re

from dekube import apply_alias_map, rewrite_k8s_dns

# Chars that make a token boundary ambiguous when glued to either side — same
# care as the engine's own DNS/alias regexes (apply_alias_map, _K8S_DNS_RE).
_HOST_CHAR = r'[A-Za-z0-9_.-]'

# Left-boundary exclusion for `_alias_port_pattern`: also rejects `/` (image path,
# e.g. `docker.io/library/redis:7` — a real URL host after `://` is already caught
# by the scheme pass) and `:` (IPv6 hex group, e.g. `fd00::db:5432`).
_LEFT_BOUNDARY_EXCLUDE = r'[A-Za-z0-9_./:-]'


class FlattenInternalUrls:  # pylint: disable=too-few-public-methods  # contract: one class, one method
    """Strip network aliases and rewrite FQDNs to short Docker names."""

    name = "flatten-internal-urls"
    priority = 2000  # run after other transforms

    @staticmethod
    def _alias_port_pattern(alias):
        """Regex for a bare ``alias:<digits>`` token — a numeric port is mandatory.

        The port is NOT optional: an earlier version made it optional, which let the
        regex backtrack to an empty port and match a scheme name (``redis://...``) or
        a YAML/JSON key (``redis:\\n``) as if it were a bare host. Requiring digits
        means the only way to match is a real ``host:port`` shape.

        The left boundary additionally excludes ``/`` and ``:`` (an image path like
        ``docker.io/library/redis:7``, or an IPv6 hex group like ``fd00::db:5432``)
        — neither is a bare host, and a real URL host is already caught by the
        scheme/``@`` pass before this one runs.

        # CBA: a bare `alias:<digits>` with nothing at all before it (start of
        # string, `=`, `,`, whitespace...) is still indistinguishable from an image
        # tag (`postgres:16`) or a `KEY=value` port-shaped number (`IMAGE=redis:7`).
        # Upgrade path: restrict the rewrite to fields known to hold a hostname
        # (an env var name allow/deny-list, or skip values that look like an image
        # reference) instead of scanning arbitrary text.
        """
        return re.compile(rf'(?<!{_LEFT_BOUNDARY_EXCLUDE}){re.escape(alias)}:(?P<port>\d+)(?!{_HOST_CHAR})')

    @staticmethod
    def _rewrite_alias_port(text, alias_map):
        """Rewrite bare ``alias:<port>`` tokens (no scheme, no ``@``) to ``target:<port>``."""
        for alias, target in (alias_map or {}).items():
            if alias in text:
                text = FlattenInternalUrls._alias_port_pattern(alias).sub(
                    lambda m, _target=target: f"{_target}:{m.group('port')}", text)
        return text

    @staticmethod
    def _bare_word_pattern(alias):
        """Boundary-safe regex for ``alias`` with nothing attached on either side.

        Callers run this only after ``_rewrite_alias_port`` has already consumed
        every ``alias:<port>`` occurrence, so any match left is a genuine bare word
        with no port — e.g. ``CACHE_DRIVER=redis`` or a lone ``nc host`` argv item.
        """
        return re.compile(rf'(?<!{_HOST_CHAR}){re.escape(alias)}(?!{_HOST_CHAR})')

    @staticmethod
    def _mark_unsafe_bare_words(text, alias_map, unsafe_aliases):
        """Flag aliases that appear as a bare word (no port) — never rewritten.

        Rewriting a bare word is exactly what corrupted URL schemes and YAML/JSON
        keys before (see ``_alias_port_pattern``'s docstring): there is no way to
        tell "this word is a hostname" from "this word is a scheme/config key" once
        the port is gone. So we never touch it — we only keep the short network
        alias standing in for it (the brief's fallback clause), same mechanism
        already used for binary ConfigMap content.
        """
        for alias in (alias_map or {}):
            if FlattenInternalUrls._bare_word_pattern(alias).search(text):
                unsafe_aliases.add(alias)

    @staticmethod
    def _rewrite_text(text, alias_map, unsafe_aliases):
        """Apply FQDN flattening + alias resolution to free text (env vars, ConfigMap files).

        Order matters: `rewrite_k8s_dns` collapses FQDNs to bare service names first,
        then `apply_alias_map` (engine helper) handles the `//`-/`@`-anchored case
        (engine ≥ v1.8.0 leaves path segments alone; older engines rewrote them too),
        then the new `alias:<port>` case, then whatever's still a bare word is left
        untouched and its alias flagged unsafe-to-strip.
        """
        text = rewrite_k8s_dns(text)
        if alias_map:
            text = apply_alias_map(text, alias_map)
            text = FlattenInternalUrls._rewrite_alias_port(text, alias_map)
            FlattenInternalUrls._mark_unsafe_bare_words(text, alias_map, unsafe_aliases)
        return text

    @staticmethod
    def _rewrite_scheme_or_at_host(text, alias_map):
        """Rewrite ``alias`` only when it's an actual URL host: right after ``scheme://`` or ``@``.

        Engines ≤ v1.7.0 had `apply_alias_map` treat any `/`-preceded token as a URL
        host, which is wrong for argv, where a bare `/` almost always means a
        filesystem path (``/usr/local/bin/<alias>``); this stays self-contained so
        argv is safe on those engines too.
        """
        for alias, target in (alias_map or {}).items():
            if alias not in text:
                continue
            pattern = re.compile(r'(?:(?<=://)|(?<=@))' + re.escape(alias) + r'''(?=[/:\s"']|$)''')
            text = pattern.sub(target, text)
        return text

    @staticmethod
    def _rewrite_argv_text(text, alias_map, unsafe_aliases):
        """Apply FQDN flattening + alias resolution to one ``command``/``entrypoint`` item.

        Deliberately narrower than `_rewrite_text`: no generic `apply_alias_map` here
        (see `_rewrite_scheme_or_at_host`), just scheme/`@` hosts and `alias:<port>`.
        Anything else stays untouched and its alias is flagged unsafe-to-strip —
        e.g. `/usr/local/bin/api` or a bare `nc host port` argv item.
        """
        text = rewrite_k8s_dns(text)
        if alias_map:
            text = FlattenInternalUrls._rewrite_scheme_or_at_host(text, alias_map)
            text = FlattenInternalUrls._rewrite_alias_port(text, alias_map)
            FlattenInternalUrls._mark_unsafe_bare_words(text, alias_map, unsafe_aliases)
        return text

    @staticmethod
    def _strip_aliases(compose_services, unsafe_aliases):
        """Remove network aliases from all compose services.

        A short alias flagged in ``unsafe_aliases`` (a bare-word reference found
        anywhere we couldn't safely rewrite — free text, argv, or binary ConfigMap
        data) is kept instead of stripped — a redundant alias beats a silently
        broken hostname.
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
    def _rewrite_env(compose_services, alias_map, unsafe_aliases):
        """Rewrite FQDN/alias references in environment variables."""
        for svc in compose_services.values():
            env = svc.get("environment")
            if not env or not isinstance(env, dict):
                continue
            for key in list(env):
                val = env[key]
                if isinstance(val, str):
                    rewritten = FlattenInternalUrls._rewrite_text(val, alias_map, unsafe_aliases)
                    if rewritten != val:
                        env[key] = rewritten

    @staticmethod
    def _rewrite_command_args(compose_services, alias_map, unsafe_aliases):
        """Rewrite FQDN/alias references inside ``command``/``entrypoint`` list items."""
        for svc in compose_services.values():
            for key in ("command", "entrypoint"):
                items = svc.get(key)
                if not isinstance(items, list):
                    continue
                for i, item in enumerate(items):
                    if isinstance(item, str):
                        rewritten = FlattenInternalUrls._rewrite_argv_text(item, alias_map, unsafe_aliases)
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
                rewritten = FlattenInternalUrls._rewrite_text(content, alias_map, unsafe_aliases)
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
        self._rewrite_env(compose_services, ctx.alias_map, unsafe_aliases)
        self._rewrite_command_args(compose_services, ctx.alias_map, unsafe_aliases)
        self._rewrite_configmap_files(ctx.output_dir, ctx.alias_map, unsafe_aliases)
        self._rewrite_ingress_entries(ingress_entries, ctx.alias_map)
        self._strip_aliases(compose_services, unsafe_aliases)
