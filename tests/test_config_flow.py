"""Tests for the config-flow host normalisation helper."""

from custom_components.junghome.config_flow import _normalize_host


def test_normalize_host_strips_scheme_whitespace_and_slash():
    cases = {
        "192.168.1.10": "192.168.1.10",
        "  192.168.1.10  ": "192.168.1.10",
        "https://junghome.local": "junghome.local",
        "http://junghome.local/": "junghome.local",
        "HTTPS://Gateway/": "Gateway",  # host casing is preserved
        "gateway/": "gateway",
    }
    for raw, expected in cases.items():
        assert _normalize_host(raw) == expected
