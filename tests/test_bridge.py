"""Targeted test for UsenetBridge's HiveMind client credentials.

The HiveMind bus client is stubbed entirely -- no live hive or usenet
server needed.
"""
from unittest.mock import patch

from hivemind_usenet.bridge import UsenetBridge


def test_get_hm_client_passes_password_for_v3_noise():
    """A v3-Noise-only hub requires the PSK password alongside the key."""
    bridge = UsenetBridge(
        creds=None,
        server=None,
        hive_host="127.0.0.1",
        hive_port=5678,
        hive_key="testkey",
        hive_password="testpassword",
        my_secret="codeword",
        poll_seconds=99999,
    )
    with patch("hivemind_usenet.bridge.HiveMessageBusClient") as MockClient:
        bridge._get_hm_client()

        MockClient.assert_called_once()
        kwargs = MockClient.call_args.kwargs
        assert kwargs.get("key") == "testkey"
        assert kwargs.get("password") == "testpassword"
