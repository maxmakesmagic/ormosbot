"""Site utilities for OrmosBot."""

import json
from pathlib import Path

import pywikibot
from pywikibot.comms import http
from pywikibot.site import BaseSite


def load_headers(config_path: Path) -> dict[str, str]:
    """Load custom headers from the project JSON config."""
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # help operator diagnose missing secrets
        raise RuntimeError(f"Missing config file: {config_path}") from exc

    headers = data.get("headers")
    if not isinstance(headers, dict):
        raise RuntimeError(f"{config_path} must define a 'headers' object")

    return {str(key): str(value) for key, value in headers.items()}


def ensure_custom_headers(config_path: Path) -> None:
    """Ensure the shared Pywikibot session sends the configured headers."""
    custom_headers = load_headers(config_path)
    for key, value in custom_headers.items():
        if http.session.headers.get(key) == value:
            continue
        http.session.headers[key] = value


def get_site(config_path: Path, lang: str = "en", family: str = "mtg") -> BaseSite:
    """Get a Pywikibot site with custom headers applied."""
    ensure_custom_headers(config_path)
    site = pywikibot.Site(code=lang, fam=family)
    return site
