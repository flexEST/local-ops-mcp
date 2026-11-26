from ipaddress import ip_address
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def private_ip(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or (
        address.is_private and not (address.is_link_local or address.is_unspecified
                                    or address.is_reserved or address.is_multicast)
    )


def configured_secret(variable: str | None) -> str | None:
    if variable is None:
        return None
    value = os.environ.get(variable)
    if not value:
        raise ValueError(f"Set the configured environment variable {variable}")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def router_status(url: str, token_env: str | None) -> dict:
    endpoint = urlsplit(url)
    if (endpoint.scheme not in ("http", "https") or not private_ip(endpoint.hostname or "")
            or endpoint.username is not None or endpoint.fragment):
        raise ValueError("Configure a fixed HTTP(S) URL with a private or loopback IP address")
    if token_env and endpoint.scheme != "https":
        raise ValueError("Router credentials require HTTPS")
    token = configured_secret(token_env)
    headers = {"Accept": "application/json, text/plain"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(Request(url, headers=headers), timeout=3) as response:
        body = response.read(16_385)
        return {
            "status": response.status,
            "body": body[:16_384].decode("utf-8", errors="replace"),
            "truncated": len(body) > 16_384,
        }


def dns_log_tail(path: Path, limit: int) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as log:
        info = os.fstat(log.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("The DNS log must be a regular file")
        offset = max(0, info.st_size - 65_536)
        log.seek(offset)
        data = log.read(65_536)
    if offset:
        _, _, data = data.partition(b"\n")
    lines = data.decode("utf-8", errors="replace").splitlines()
    return {"lines": lines[-limit:], "truncated": bool(offset or len(lines) > limit)}
