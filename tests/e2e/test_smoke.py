"""Baseline hivescope wiring check for hivemind-usenet.

Verifies the package co-installs with hivescope and that a single-satellite
topology completes a handshake end-to-end. Usenet-carrier-specific transport
e2e (carrier round-trip over a real/mock NNTP backend) is a follow-up.
"""
from hivescope.assertions import assert_handshake_complete


def test_hivescope_wiring_handshake(hive):
    master, satellite = hive
    assert_handshake_complete(master, satellite)
