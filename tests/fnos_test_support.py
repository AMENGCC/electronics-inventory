"""Test-only TCP transport for the fnOS HTTP handler on development computers.

This module is never included in an FPK or imported by the application.
"""
import secrets
import socket
from socketserver import TCPServer

from app import GATEWAY_PREFIX, GatewayServer, Handler


class GatewayTCPHarness(GatewayServer):
    address_family = socket.AF_INET

    def __init__(self, inventory, prefix=GATEWAY_PREFIX, address=("127.0.0.1", 0)):
        self.inventory = inventory
        self.base_path = prefix
        self.csrf_token = secrets.token_urlsafe(32)
        TCPServer.__init__(self, address, Handler)
        self.server_port = self.server_address[1]
