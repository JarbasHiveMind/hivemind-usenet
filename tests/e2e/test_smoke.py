"""Baseline hivescope wiring check for hivemind-usenet.

Verifies the package co-installs with hivescope and that a single-satellite
topology completes a handshake end-to-end. The Usenet-carrier-specific
transport e2e — a real HiveMind handshake + bus round-trip over the real
UsenetCarrier (PGP + chunking + hSub) on a faked in-memory NNTP server — lives
in ``test_usenet_e2e.py`` (harness: ``usenet_link.py``).
"""
from hivescope.assertions import assert_handshake_complete


def test_hivescope_wiring_handshake(hive):
    master, satellite = hive
    assert_handshake_complete(master, satellite)
