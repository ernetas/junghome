"""Translation-file consistency checks.

`strings.json` and the 26 files in `translations/` have to stay in lockstep: a
key added to one and not the others silently falls back to English for every
other locale, which is exactly how the device-trigger strings ended up
English-only in 25 locales. hassfest only validates `en` for custom
integrations, so this is the only automated guard available.
"""

import json
import re
from pathlib import Path

import pytest

_COMPONENT = Path(__file__).parent.parent / "custom_components" / "junghome"
_STRINGS = _COMPONENT / "strings.json"
_TRANSLATIONS = sorted((_COMPONENT / "translations").glob("*.json"))


def _load(path: Path) -> dict:
    """Parse a translation file, rejecting duplicate keys.

    `json.load` keeps the last of a duplicated key, so a botched merge that
    writes the same block twice parses cleanly and compares equal. Reject it
    here instead.
    """

    def _no_duplicates(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise ValueError(f"duplicate key {key!r} in {path.name}")
            seen.add(key)
        return dict(pairs)

    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates
    )


def _leaves(obj: object, prefix: str = "") -> dict[str, str]:
    """Flatten a nested translation dict to {dotted.key: value}."""
    if not isinstance(obj, dict):
        return {prefix: obj}
    out: dict[str, str] = {}
    for key, value in obj.items():
        out.update(_leaves(value, f"{prefix}.{key}" if prefix else key))
    return out


def _placeholders(value: object) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", str(value)))


def test_translations_directory_is_not_empty() -> None:
    """Guard the parametrisation itself — an empty glob would vacuously pass."""
    assert len(_TRANSLATIONS) >= 26


@pytest.mark.parametrize("path", _TRANSLATIONS, ids=lambda p: p.stem)
def test_locale_matches_strings_json(path: Path) -> None:
    """Every locale carries exactly the keys `strings.json` defines."""
    expected = _leaves(_load(_STRINGS))
    actual = _leaves(_load(path))

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    assert not missing, f"{path.name} is missing {len(missing)} keys: {missing}"
    assert not extra, f"{path.name} has {len(extra)} keys not in strings.json: {extra}"


@pytest.mark.parametrize("path", _TRANSLATIONS, ids=lambda p: p.stem)
def test_locale_preserves_placeholders(path: Path) -> None:
    """A renamed/dropped {placeholder} raises KeyError when HA formats the string."""
    expected = _leaves(_load(_STRINGS))
    for key, value in _leaves(_load(path)).items():
        if key in expected:
            assert _placeholders(value) == _placeholders(expected[key]), (
                f"{path.name}: placeholder mismatch in {key}"
            )


@pytest.mark.parametrize("path", [_STRINGS, *_TRANSLATIONS], ids=lambda p: p.stem)
def test_no_angle_brackets(path: Path) -> None:
    """`<`/`>` in translation text breaks Home Assistant's translation parser."""
    for key, value in _leaves(_load(path)).items():
        assert "<" not in str(value), f"{path.name}: '<' in {key}"
        assert ">" not in str(value), f"{path.name}: '>' in {key}"
