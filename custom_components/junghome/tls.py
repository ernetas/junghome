"""TLS certificate pinning for the JUNG HOME Gateway.

The gateway serves its REST API and WebSocket over HTTPS with a **self-signed**
certificate (CN ``junghome.local``), so certificate-authority verification is
impossible and every client talks to it with verification off — this
integration through Home Assistant's shared ``verify_ssl=False`` session.
Verification off means *any* HTTPS responder at the stored address is
accepted, and the very first request hands it the API token (which is
equivalent to the mesh keys: ``GET /project/cdb`` — docs/gateway-rest-api.md).

The JUNG app closes that hole by pinning the certificate: it reads the
gateway's SHA-256 fingerprint over the mesh (vendor property ``0xC003
fingerprint_sha256``) and its trust manager accepts only that certificate.
Home Assistant has no mesh path, so the fingerprint is learned on first
contact (**trust on first use**) and pinned from then on: every request and
WebSocket upgrade passes ``ssl=aiohttp.Fingerprint(...)``, which aiohttp
checks immediately after the TLS handshake, before a single HTTP byte — and
so before the ``token`` header — is written (``connector.py``,
``_create_direct_connection``: the transport is closed and
``ServerFingerprintMismatch`` raised on a mismatch).

Learning uses the same mechanism in reverse: a connection pinned to a digest
no certificate can have (``PROBE_DIGEST``) is aborted by aiohttp at the
handshake, and the peer's real digest is read from the mismatch error. No
request is ever sent on that connection, so nothing — no token, no path —
reaches the peer before its identity is known.

This module holds the pure aiohttp plumbing; where the fingerprint is
learned, stored and checked is the coordinator (TOFU for existing entries,
the ``tls_certificate_changed`` repair issue on a mismatch), the config flow
(learned at registration, verified on reconfigure and rediscovery) and
``repairs.py`` (re-pinned only after the user confirms).
"""

import asyncio
from functools import lru_cache

import aiohttp

# Length of a SHA-256 hex digest, the only form ``CONF_TLS_FINGERPRINT`` holds.
FINGERPRINT_HEX_LENGTH = 64

# A digest no certificate can have (SHA-256 preimage resistance). Pinning a
# connection to it guarantees aiohttp aborts at the handshake and reports the
# peer's real digest in ``ServerFingerprintMismatch.got`` — the learn step.
PROBE_DIGEST = bytes(32)

# Bound for the learn-time handshake. The gateway is on the LAN: it either
# completes a TLS handshake within seconds or it is not answering, and a
# config-flow form or a discovery flow is waiting on the result.
LEARN_TIMEOUT = 10


@lru_cache(maxsize=16)
def fingerprint_ssl(fingerprint: str) -> aiohttp.Fingerprint:
    """Return the ``ssl=`` argument that pins a connection to ``fingerprint``.

    ``fingerprint`` is the SHA-256 hex digest as stored in the entry. Cached
    per digest deliberately: aiohttp keys its connection pool on the ``ssl``
    object by identity, so a fresh ``Fingerprint`` per request would defeat
    keep-alive and cost a TLS handshake per poll. A handful of gateways at
    most, so the cache stays tiny. Raises ``ValueError`` on a malformed
    digest (wrong length or non-hex) — a hand-edited entry — rather than
    silently pinning nothing.
    """
    digest = bytes.fromhex(fingerprint)
    if len(digest) != len(PROBE_DIGEST):
        raise ValueError("TLS fingerprint must be a SHA-256 hex digest")
    return aiohttp.Fingerprint(digest)


def normalize_fingerprint(value: object) -> str | None:
    """Return ``value`` as a stored fingerprint, or None if it is not one.

    Accepts the hex digest with or without the conventional ``:`` separators
    and in either case, so a value copied from the JUNG app or an ``openssl``
    printout compares equal to a learned one. Anything that is not exactly a
    SHA-256 digest reads as "no fingerprint" — an entry with a corrupted value
    then behaves like one that never had a pin (learns again on first use)
    instead of failing every request against garbage.
    """
    if not isinstance(value, str):
        return None
    text = value.replace(":", "").strip().lower()
    if len(text) != FINGERPRINT_HEX_LENGTH:
        return None
    try:
        bytes.fromhex(text)
    except ValueError:
        return None
    return text


def format_fingerprint(fingerprint: str) -> str:
    """Render a hex digest in the conventional ``AB:CD:...`` form for display."""
    text = fingerprint.upper()
    return ":".join(text[i : i + 2] for i in range(0, len(text), 2))


async def async_learn_fingerprint(session: aiohttp.ClientSession, host: str) -> str:
    """Return the SHA-256 hex digest of the certificate ``host`` presents.

    Opens one TLS connection pinned to ``PROBE_DIGEST``; aiohttp closes it at
    the handshake and the real digest comes back in the mismatch error. The
    URL is the gateway's unauthenticated ``version`` endpoint — it carries no
    token in any case, and aiohttp never gets as far as sending it.

    Network failures (unreachable, refused, handshake timeout) propagate as
    the ``aiohttp.ClientError`` / ``TimeoutError`` every caller already
    handles as "cannot connect". A peer that *accepts* the probe digest
    cannot exist; should the request somehow complete (a test double that
    ignores ``ssl=``), it is reported as a connection error too, so a caller
    never proceeds without a real fingerprint.
    """
    url = f"https://{host}/api/junghome/version/"
    try:
        async with (
            asyncio.timeout(LEARN_TIMEOUT),
            session.get(url, ssl=aiohttp.Fingerprint(PROBE_DIGEST)),
        ):
            pass
    except aiohttp.ServerFingerprintMismatch as err:
        return err.got.hex()
    raise aiohttp.ClientConnectionError(
        f"{host} completed a TLS handshake without presenting a certificate"
    )
