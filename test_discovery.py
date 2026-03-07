#!/usr/bin/env python3
"""
Test discovery on localhost.
"""

import os
import time
import threading

# Set different IDs
os.environ["MESHLINK_NODE_ID"] = "test1"
os.environ["MESHLINK_NODE_NAME"] = "Test1"

from core.discovery import DiscoveryService

def test_discovery():
    d1 = DiscoveryService()
    d1._public_key = "test_key_1"
    d1._signing_key = "test_sign_1"

    # Change env for second
    os.environ["MESHLINK_NODE_ID"] = "test2"
    os.environ["MESHLINK_NODE_NAME"] = "Test2"

    from importlib import reload
    import core.config
    reload(core.config)

    d2 = DiscoveryService()
    d2._public_key = "test_key_2"
    d2._signing_key = "test_sign_2"

    peers1 = []
    peers2 = []

    def on_peer1(peer):
        print(f"D1 discovered: {peer.name} {peer.ip}")
        peers1.append(peer)

    def on_peer2(peer):
        print(f"D2 discovered: {peer.name} {peer.ip}")
        peers2.append(peer)

    d1.on_peer_joined = on_peer1
    d2.on_peer_joined = on_peer2

    print("Starting discovery services...")
    d1.start()
    d2.start()

    time.sleep(10)  # Wait for discovery

    print(f"D1 peers: {len(d1.get_peers())}")
    print(f"D2 peers: {len(d2.get_peers())}")

    d1.stop()
    d2.stop()

if __name__ == "__main__":
    test_discovery()