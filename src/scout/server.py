"""Serve on explicit local interfaces without exposing a wildcard listener."""

import socket
from contextlib import ExitStack

import uvicorn

from .api import create_app


def serve(store, settings, port):
    config = uvicorn.Config(
        create_app(store, settings),
        port=port,
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=",".join(settings.trusted_proxies),
    )
    with ExitStack() as stack:
        sockets = []
        for address in settings.bind_hosts:
            sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
            sock.listen(128)
            sockets.append(sock)
        uvicorn.Server(config).run(sockets=sockets)
