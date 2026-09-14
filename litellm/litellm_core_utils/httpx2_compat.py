from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Final, Generic, Literal, TypeAlias, TypeGuard, TypeVar

import httpx2 as httpx
from httpx2 import Headers, Response, Timeout
from httpx2._types import (
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


def is_httpx2_client(value: object, *, async_client: bool) -> TypeGuard[httpx.Client | httpx.AsyncClient]:
    client_type: Final = httpx.AsyncClient if async_client else httpx.Client
    return isinstance(value, client_type)


def is_legacy_httpx_client(value: object, *, async_client: bool) -> TypeGuard[httpx.Client | httpx.AsyncClient]:
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
    "accepts_legacy_httpx_client",
    "boundary_types",
    "environment_proxy_mounts",
    "environment_proxy_urls",
    "is_httpx2_client",
    "is_legacy_httpx_client",
]
