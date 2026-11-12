from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Final, Generic, Literal, Protocol, TypeAlias, TypeVar, cast

import httpx2 as httpx
from httpx2 import Headers, Response, Timeout
from httpx2._client import UseClientDefault
from httpx2._types import (
    AuthTypes,
    CertTypes,
    CookieTypes,
    FileContent,
    FileTypes,
    QueryParamTypes,
    RequestContent,
    RequestFiles,
)
from httpx2._utils import get_environment_proxies

_HTTPTransportT = TypeVar("_HTTPTransportT", httpx.HTTPTransport, httpx.AsyncHTTPTransport)

_BoundaryKind: TypeAlias = Literal["client", "response", "timeout", "url", "exception"]

_LegacyRawHeader: TypeAlias = tuple[bytes, bytes]


class _LegacyHeaders(Protocol):
    raw: Sequence[_LegacyRawHeader]


class _LegacySyncStream(Protocol):
    def __iter__(self) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class _LegacyAsyncStream(Protocol):
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class _LegacyURL(Protocol):
    pass


class _LegacyRequest(Protocol):
    method: str
    url: _LegacyURL
    content: bytes
    extensions: Mapping[str, object]


class _LegacyResponse(Protocol):
    status_code: int
    headers: _LegacyHeaders
    stream: _LegacySyncStream | _LegacyAsyncStream
    extensions: Mapping[str, object]


class _LegacyRequestFactory(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: Sequence[_LegacyRawHeader],
        content: bytes,
        extensions: Mapping[str, object],
    ) -> _LegacyRequest: ...


class _LegacySyncTransport(Protocol):
    def handle_request(self, request: _LegacyRequest) -> _LegacyResponse: ...


class _LegacyAsyncTransport(Protocol):
    def handle_async_request(self, request: _LegacyRequest) -> _LegacyResponse | Awaitable[_LegacyResponse]: ...


class _LegacyTimeout(Protocol):
    connect: float | None
    read: float | None
    write: float | None
    pool: float | None


class _LegacyClient(Protocol):
    timeout: _LegacyTimeout
    headers: _LegacyHeaders
    follow_redirects: bool


class _HTTPX2SyncByteStream(httpx.SyncByteStream):
    def __init__(self, stream: _LegacySyncStream) -> None:
        self.stream = stream

    def __iter__(self) -> Iterator[bytes]:
        yield from self.stream

    def close(self) -> None:
        self.stream.close()


class _HTTPX2AsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, stream: _LegacyAsyncStream) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


def _legacy_request(request: httpx.Request) -> tuple[ModuleType, _LegacyRequest]:
    legacy_module = legacy_httpx_module()
    if legacy_module is None:
        raise TypeError("A legacy HTTPX transport requires the httpx package")
    request_factory = cast(_LegacyRequestFactory, legacy_module.Request)
    return legacy_module, request_factory(
        request.method,
        str(request.url),
        headers=list(request.headers.raw),
        content=request.content,
        extensions=dict(request.extensions),
    )


def _legacy_response_type(legacy_module: ModuleType) -> type[object]:
    return cast(type[object], legacy_module.Response)


class _LegacySyncTransportAdapter(httpx.BaseTransport):
    def __init__(self, transport: object) -> None:
        self.transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        legacy_module, legacy_request = _legacy_request(request)
        transport = cast(_LegacySyncTransport, self.transport)
        response = transport.handle_request(legacy_request)
        if not isinstance(response, _legacy_response_type(legacy_module)):
            raise TypeError(f"Legacy transport returned {type(response)!r}, expected httpx.Response")
        return httpx.Response(
            response.status_code,
            headers=list(response.headers.raw),
            stream=_HTTPX2SyncByteStream(cast(_LegacySyncStream, response.stream)),
            request=request,
            extensions=dict(response.extensions),
        )


class _LegacyAsyncTransportAdapter(httpx.AsyncBaseTransport):
    def __init__(self, transport: object) -> None:
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        legacy_module, legacy_request = _legacy_request(request)
        transport = cast(_LegacyAsyncTransport, self.transport)
        result = transport.handle_async_request(legacy_request)
        if isinstance(result, Awaitable):
            result = await result
        if not isinstance(result, _legacy_response_type(legacy_module)):
            raise TypeError(f"Legacy transport returned {type(result)!r}, expected httpx.Response")
        return httpx.Response(
            result.status_code,
            headers=list(result.headers.raw),
            stream=_HTTPX2AsyncByteStream(cast(_LegacyAsyncStream, result.stream)),
            request=request,
            extensions=dict(result.extensions),
        )


class _LegacySyncClientTransport(httpx.BaseTransport):
    def __init__(self, client: object) -> None:
        self.client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        legacy_module, legacy_request = _legacy_request(request)
        client = cast(_LegacyClient, self.client)
        transport_for_url = cast(
            Callable[[_LegacyURL], _LegacySyncTransport],
            getattr(client, "_transport_for_url", None),
        )
        if transport_for_url is None:
            raise TypeError("The legacy HTTPX client does not expose _transport_for_url")
        transport = transport_for_url(legacy_request.url)
        response = transport.handle_request(legacy_request)
        if not isinstance(response, _legacy_response_type(legacy_module)):
            raise TypeError(f"Legacy transport returned {type(response)!r}, expected httpx.Response")
        return httpx.Response(
            response.status_code,
            headers=list(response.headers.raw),
            stream=_HTTPX2SyncByteStream(cast(_LegacySyncStream, response.stream)),
            request=request,
            extensions=dict(response.extensions),
        )


def normalize_legacy_sync_client(client: object) -> httpx.Client:
    legacy_client = cast(_LegacyClient, client)
    legacy_timeout = legacy_client.timeout
    return httpx.Client(
        transport=_LegacySyncClientTransport(legacy_client),
        timeout=httpx.Timeout(
            connect=legacy_timeout.connect,
            read=legacy_timeout.read,
            write=legacy_timeout.write,
            pool=legacy_timeout.pool,
        ),
        headers=list(legacy_client.headers.raw),
        follow_redirects=legacy_client.follow_redirects,
        trust_env=False,
    )


def normalize_sync_transport(transport: object) -> httpx.BaseTransport:
    if isinstance(transport, httpx.BaseTransport):
        return transport
    handle_request = getattr(transport, "handle_request", None)
    if callable(handle_request):
        return _LegacySyncTransportAdapter(transport)
    raise TypeError(f"Invalid sync HTTP transport: {type(transport)!r}")


def normalize_async_transport(transport: object) -> httpx.AsyncBaseTransport:
    if transport is None:
        raise TypeError("An async HTTP transport is required")
    if isinstance(transport, httpx.AsyncBaseTransport):
        return transport
    handle_async_request = getattr(transport, "handle_async_request", None)
    if callable(handle_async_request):
        return _LegacyAsyncTransportAdapter(transport)
    raise TypeError(f"Invalid async HTTP transport: {type(transport)!r}")


@dataclass(frozen=True, slots=True)
class EnvironmentProxyMounts(Generic[_HTTPTransportT]):
    mounts: Mapping[str, _HTTPTransportT | None]


def environment_proxy_mounts(
    build_proxy_transport: Callable[[str], _HTTPTransportT],
) -> EnvironmentProxyMounts[_HTTPTransportT]:
    return EnvironmentProxyMounts(
        mounts={
            pattern: None if proxy_url is None else build_proxy_transport(proxy_url)
            for pattern, proxy_url in get_environment_proxies().items()
        }
    )


def environment_proxy_urls() -> Mapping[str, str | None]:
    return get_environment_proxies()


def legacy_httpx_module() -> ModuleType | None:
    return sys.modules.get("httpx")


def is_httpx2_client(value: object, *, async_client: bool) -> bool:
    client_type: Final = httpx.AsyncClient if async_client else httpx.Client
    return isinstance(value, client_type)


def is_legacy_httpx_client(value: object, *, async_client: bool) -> bool:
    module: Final = legacy_httpx_module()
    if module is None:
        return False
    client_type: Final = getattr(module, "AsyncClient" if async_client else "Client", None)
    if not isinstance(client_type, type):
        return False
    return isinstance(value, client_type)


def accepts_legacy_httpx_client(value: object, *, async_client: bool) -> bool:
    return not is_httpx2_client(value, async_client=async_client) and is_legacy_httpx_client(
        value, async_client=async_client
    )


def normalize_timeout(timeout: object, *, supports_timeout: bool) -> float | Timeout:
    if isinstance(timeout, Timeout) and supports_timeout:
        return timeout
    if isinstance(timeout, Timeout):
        return timeout.read or 600
    if isinstance(timeout, (int, float, str)) and not isinstance(timeout, bool):
        return float(timeout)
    return 600.0


def boundary_types(kind: _BoundaryKind) -> tuple[type, ...]:
    by_kind: Final[dict[_BoundaryKind, tuple[type, ...]]] = {
        "client": (httpx.Client, httpx.AsyncClient),
        "response": (httpx.Response,),
        "timeout": (httpx.Timeout,),
        "url": (httpx.URL,),
        "exception": (
            httpx.HTTPStatusError,
            httpx.TimeoutException,
            httpx.HTTPError,
        ),
    }
    return by_kind[kind]


__all__ = [
    "AuthTypes",
    "CertTypes",
    "CookieTypes",
    "EnvironmentProxyMounts",
    "FileContent",
    "FileTypes",
    "Headers",
    "QueryParamTypes",
    "RequestContent",
    "RequestFiles",
    "Response",
    "Timeout",
    "UseClientDefault",
    "accepts_legacy_httpx_client",
    "boundary_types",
    "environment_proxy_mounts",
    "environment_proxy_urls",
    "is_httpx2_client",
    "is_legacy_httpx_client",
    "normalize_async_transport",
    "normalize_legacy_sync_client",
    "normalize_sync_transport",
    "normalize_timeout",
]
