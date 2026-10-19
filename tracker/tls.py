"""Certificate checks for government sites that send an incomplete chain (Oct 18 2026, security review).

Before, the e-Gazette and the National Centre for Seismology were read with verify=False: any machine
between GitHub and the site could have shown the tracker a fake gazette or quake. Now they are read with
the usual trust store plus the public chain in certs/extra-chain.pem, which completes what egazette.gov.in
leaves out.

watchlist.yml switches (top level):
  tls_verify: false   -> back to the old way (no check at all)
  tls_strict: true    -> a site that fails the check is not read (reported as failing); the default,
                         false, reads it once more without the check and says so in the log, so a site
                         changing its certificate never silently stops the gazettes
"""
from __future__ import annotations

import pathlib
import tempfile

import certifi
import requests
import yaml

_HERE = pathlib.Path(__file__).resolve().parent
_bundle: str | None = None


def _switch(name: str, default: bool) -> bool:
    try:
        cfg = yaml.safe_load((_HERE.parent / "watchlist.yml").read_text()) or {}
        value = cfg.get(name, default)
        return bool(value) if isinstance(value, bool) else default
    except Exception:                                                 # noqa: BLE001
        return default


def bundle() -> str | bool:
    """What to pass as requests' verify=: the trust store plus the extra chain (or False when switched off)."""
    global _bundle
    if not _switch("tls_verify", True):
        return False
    if _bundle is None:
        extra = "\n".join(p.read_text() for p in sorted((_HERE / "certs").glob("*.pem")))
        out = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
        out.write(pathlib.Path(certifi.where()).read_text() + "\n" + extra)
        out.close()
        _bundle = out.name
    return _bundle


def get(session_or_requests, method: str, url: str, **kw) -> requests.Response:
    """session.get/post with the certificate checked; see tls_strict above for a site that fails it."""
    call = getattr(session_or_requests, method.lower())
    try:
        return call(url, verify=bundle(), **kw)
    except requests.exceptions.SSLError as err:
        if _switch("tls_strict", False):
            raise
        print(f"tls: {url.split('/')[2]} failed the certificate check ({str(err)[:120]}); read without it this time")
        return call(url, verify=False, **kw)


def trust_everywhere() -> None:
    """Every other read (requests' plain verify=True) uses the same trust store, through
    REQUESTS_CA_BUNDLE: certifi 2026.10.18 dropped eMudhra's emSign roots, which most Indian
    government sites chain to (certs/emsign-roots.pem puts them back; Oct 19 2026)."""
    import os
    path = bundle()
    if path and not os.environ.get("REQUESTS_CA_BUNDLE"):
        os.environ["REQUESTS_CA_BUNDLE"] = path
