from ipaddress import ip_address
import os
from pathlib import Path
import ssl
import stat
from threading import Event
from time import monotonic
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import paho.mqtt.client as mqtt
import serial

MAX_PAYLOAD = 4096


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


def serial_read(port: str, baudrate: int, seconds: float, limit: int) -> dict:
    if not 0.1 <= seconds <= 5:
        raise ValueError("seconds must be between 0.1 and 5")
    if not 1 <= limit <= MAX_PAYLOAD:
        raise ValueError(f"limit must be between 1 and {MAX_PAYLOAD}")
    data = bytearray()
    with serial.Serial(port, baudrate=baudrate, timeout=min(seconds, 0.1)) as device:
        deadline = monotonic() + seconds
        while len(data) < limit and monotonic() < deadline:
            size = min(max(device.in_waiting, 1), limit - len(data))
            data.extend(device.read(size))
    return {"text": data.decode("utf-8", errors="replace"), "bytes_read": len(data)}


def exact_topic(topic: str) -> str:
    if not topic or any(char in topic for char in ("#", "+", "\0")) or topic.startswith("$"):
        raise ValueError("Configure a non-system MQTT topic without wildcards")
    return topic


class Broker:
    def __init__(self, config: dict):
        self.config = config
        self.connected = Event()
        self.error = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        self.client.on_connect = self.on_connect
        host = config["host"]
        if config.get("tls", True):
            context = ssl.create_default_context(cafile=config.get("ca_file"))
            self.client.tls_set_context(context)
        elif not (host == "localhost" or private_ip(host)):
            raise ValueError("Plaintext MQTT requires localhost or a private IP address")
        password = configured_secret(config.get("password_env"))
        if config.get("username"):
            self.client.username_pw_set(config["username"], password)
        elif password is not None:
            raise ValueError("MQTT password authentication requires a username")

    def on_connect(self, client, userdata, flags, reason, properties):
        if reason.is_failure:
            self.error = f"MQTT connection rejected ({reason})"
        self.connected.set()

    def __enter__(self):
        tls = self.config.get("tls", True)
        self.client.connect_timeout = 4
        self.client.connect_async(self.config["host"], self.config.get("port", 8883 if tls else 1883))
        self.client.loop_start()
        try:
            if not self.connected.wait(5):
                raise TimeoutError("MQTT connection timed out")
            if self.error is not None:
                raise ConnectionError(self.error)
        except BaseException:
            self.__exit__()
            raise
        return self.client

    def __exit__(self, *exc):
        self.client.disconnect()
        self.client.loop_stop()


def mqtt_read(config: dict, topic: str, seconds: float) -> dict:
    topic = exact_topic(topic)
    if not 0.1 <= seconds <= 10:
        raise ValueError("seconds must be between 0.1 and 10")
    received = Event()
    output = {}

    def on_message(client, userdata, message):
        if message.topic == topic and not received.is_set():
            output.update(
                text=message.payload[:MAX_PAYLOAD].decode("utf-8", errors="replace"),
                retained=bool(message.retain),
                truncated=len(message.payload) > MAX_PAYLOAD,
            )
            received.set()

    broker = Broker(config)
    broker.client.on_message = on_message
    with broker as client:
        code, _ = client.subscribe(topic, qos=1)
        if code != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError("MQTT subscribe could not be sent")
        if not received.wait(seconds):
            raise TimeoutError("No MQTT message arrived before the timeout")
    return output


def mqtt_relay(config: dict, relay: dict, on: bool) -> dict:
    if config.get("enable_relay_control") is not True:
        raise ValueError("Relay control is disabled")
    topic = exact_topic(relay["topic"])
    payload = relay["on_payload" if on else "off_payload"]
    if len(payload.encode("utf-8")) > MAX_PAYLOAD:
        raise ValueError("Relay payload exceeds the allowed size")
    with Broker(config) as client:
        message = client.publish(topic, payload, qos=1, retain=False)
        message.wait_for_publish(timeout=5)
        if not message.is_published():
            raise TimeoutError("Broker did not acknowledge the publish")
    return {"broker_acknowledged": True, "requested_on": on}
