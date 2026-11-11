"""Small, dependency-free helpers for public LINKCHECK URL configuration."""
from urllib.parse import urlsplit, urlunsplit


def normalize_https_url(value: str | None) -> str:
    """Normalize a hostname or HTTP(S) URL to an HTTPS URL; return empty if invalid."""
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")

    try:
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        if not parsed.netloc or not parsed.hostname:
            return ""
        if parsed.username or parsed.password:
            return ""
    except ValueError:
        return ""

    return urlunsplit((
        "https",
        parsed.netloc,
        parsed.path.rstrip("/"),
        parsed.query,
        parsed.fragment,
    ))


def derive_bothost_domain(bot_id: str | None) -> str:
    """
    Derive Bothost's automatic hostname from its documented BOT_ID format.
    Bothost replaces underscores with hyphens and appends .bothost.tech.
    """
    identifier = str(bot_id or "").strip()
    if not identifier.startswith("bot_"):
        return ""
    hostname = identifier.replace("_", "-") + ".bothost.tech"
    return normalize_https_url(hostname)


def resolve_miniapp_url(configured_url: str | None, platform_domain: str | None) -> str:
    """
    Prefer Bothost's assigned DOMAIN when MINIAPP_URL is stale.
    Preserve an explicitly configured path when both hostnames match.
    """
    configured = normalize_https_url(configured_url)
    platform = normalize_https_url(platform_domain)
    if not platform:
        return configured
    if not configured:
        return platform

    configured_host = urlsplit(configured).hostname
    platform_host = urlsplit(platform).hostname
    if configured_host and configured_host.casefold() == platform_host.casefold():
        return configured
    return platform
