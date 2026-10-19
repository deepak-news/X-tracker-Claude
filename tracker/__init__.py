# One trust store for every read (tls.py): certifi's, plus the chains in tracker/certs
from . import tls as _tls

try:
    _tls.trust_everywhere()
except Exception:                                                     # noqa: BLE001
    pass
