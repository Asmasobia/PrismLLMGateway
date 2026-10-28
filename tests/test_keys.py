"""Virtual-key hashing and the non-secret prefix."""

from __future__ import annotations

from prism.keys import hash_virtual_key, key_prefix

KEY = "prism-sk-search-1a2b3c"


def test_hash_is_deterministic() -> None:
    """Lookup depends on it: the same key must always produce the same digest."""
    assert hash_virtual_key(KEY) == hash_virtual_key(KEY)
    assert len(hash_virtual_key(KEY)) == 64


def test_hash_differs_per_key() -> None:
    assert hash_virtual_key(KEY) != hash_virtual_key(KEY + "x")


def test_hash_does_not_contain_the_key() -> None:
    assert KEY not in hash_virtual_key(KEY)


def test_prefix_identifies_the_tenant_without_revealing_the_secret() -> None:
    assert key_prefix(KEY) == "prism-sk-search"
    # The random tail is what makes the key a secret; it must not survive.
    assert "1a2b3c" not in key_prefix(KEY)


def test_prefix_of_an_unconventional_key_stays_short() -> None:
    """A key with no dashes must not be echoed almost in full."""
    assert key_prefix("abcdefghijklmnopqrstuvwxyz0123456789") == "abcdefghijklmnop"


def test_prefix_of_a_long_multi_segment_key_keeps_three_segments() -> None:
    assert key_prefix("prism-sk-budget-demo-0j1k2l") == "prism-sk-budget"
