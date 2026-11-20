from __future__ import annotations

import base64
import http.client
import json
import ssl
import struct
import sys
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import BinaryIO, Protocol

from .remote_http import (
    FRAME_HEADER,
    MAX_BODY,
    MAX_INPUT,
    MAX_REPLY_HEADER,
    RemoteHttpError,
    printable_ascii,
    validate_endpoint,
    validate_headers,
)
from .remote_types import HttpReply, object_dict


class Connection(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str]) -> None: ...
    def getresponse(self) -> http.client.HTTPResponse: ...
    def close(self) -> None: ...


def _connection(host: str) -> Connection:
    return http.client.HTTPSConnection(host, timeout=15, context=ssl.create_default_context())


def fetch(
    url: str, headers: Mapping[str, str], *, connect: Callable[[str], Connection] = _connection
) -> HttpReply:
    host, path = validate_endpoint(url)
    clean_headers = validate_headers(headers)
    connection = connect(host)
    try:
        connection.request("GET", path, headers=clean_headers)
        response = connection.getresponse()
        if HTTPStatus.MULTIPLE_CHOICES <= response.status < HTTPStatus.BAD_REQUEST:
            raise RemoteHttpError("http_redirect")
        reply_headers = {}
        for key in ("retry-after", "content-type"):
            value = response.getheader(key)
            if value is not None:
                if len(value) > MAX_REPLY_HEADER or not printable_ascii(value):
                    raise RemoteHttpError("http_protocol")
                reply_headers[key] = value
        body = bytearray()
        if response.status == HTTPStatus.OK:
            while True:
                chunk = response.read(min(65536, MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise RemoteHttpError("http_size")
        return HttpReply(response.status, reply_headers, bytes(body))
    finally:
        connection.close()


def _read_request(stream: BinaryIO) -> dict[str, object]:
    size = stream.read(4)
    if len(size) != FRAME_HEADER:
        raise RemoteHttpError("http_protocol")
    length = struct.unpack("!I", size)[0]
    if length > MAX_INPUT:
        raise RemoteHttpError("http_protocol")
    payload = stream.read(length)
    if len(payload) != length:
        raise RemoteHttpError("http_protocol")
    return object_dict(json.loads(payload))


def main() -> int:
    try:
        raw = _read_request(sys.stdin.buffer)
        url, raw_headers = raw["url"], object_dict(raw["headers"])
        if not isinstance(url, str) or any(
            not isinstance(value, str) for value in raw_headers.values()
        ):
            raise RemoteHttpError("http_protocol")
        reply = fetch(url, {key: str(value) for key, value in raw_headers.items()})
        payload: dict[str, object] = {
            "status": reply.status,
            "headers": dict(reply.headers),
            "body": base64.b64encode(reply.body).decode("ascii"),
        }
    except RemoteHttpError as exc:
        payload = {"error": exc.code}
    except (OSError, ValueError, KeyError, TypeError, RecursionError, http.client.HTTPException):
        payload = {"error": "http_error"}
    encoded = json.dumps(payload, allow_nan=False).encode()
    sys.stdout.buffer.write(struct.pack("!I", len(encoded)) + encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
