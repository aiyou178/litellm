from __future__ import annotations

import inspect
import warnings
from collections.abc import Awaitable, Callable
from typing import Final, TypeAlias

import httpx
import httpx2
from respx import mocks as respx_mocks
from respx.mocks import Mocker
from respx.models import PassThrough, Route, SideEffectError
from respx.transports import TryTransport
from starlette.testclient import TestClient

TransportSpec: TypeAlias = Callable[..., httpx.BaseTransport]
HTTPX2Transport: TypeAlias = httpx2.BaseTransport | httpx2.AsyncBaseTransport


class _HTTPX2Stream(httpx2.SyncByteStream, httpx2.AsyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream | httpx.AsyncByteStream) -> None:
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self) -> None:
        close = getattr(self.stream, "close", None)
        if close is not None:
            close()

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        aclose = getattr(self.stream, "aclose", None)
        if aclose is not None:
            await aclose()


class _HTTPXStream(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, stream: httpx2.SyncByteStream | httpx2.AsyncByteStream) -> None:
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self) -> None:
        close = getattr(self.stream, "close", None)
        if close is not None:
            close()

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        aclose = getattr(self.stream, "aclose", None)
        if aclose is not None:
            await aclose()


def _to_legacy_request(request: httpx2.Request) -> httpx.Request:
    return httpx.Request(
        request.method,
        str(request.url),
        headers=request.headers,
        content=request.content,
        extensions=request.extensions,
    )


def _to_httpx2_request(request: httpx.Request) -> httpx2.Request:
    return httpx2.Request(
        request.method,
        str(request.url),
        headers=request.headers,
        content=request.content,
        extensions=request.extensions,
    )


def _to_legacy_response(
    response: httpx2.Response,
    request: httpx.Request | None = None,
) -> httpx.Response:
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        stream=_HTTPXStream(response.stream),
        request=request,
        extensions=dict(response.extensions),
    )


def _to_httpx2_response(response: httpx.Response, request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        response.status_code,
        headers=response.headers,
        stream=_HTTPX2Stream(response.stream),
        request=request,
        extensions=dict(response.extensions),
    )


async def _to_httpx2_response_async(response: httpx.Response, request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(
        response.status_code,
        headers=response.headers,
        stream=_HTTPX2Stream(response.stream),
        request=request,
        extensions=dict(response.extensions),
    )


def _store_route_return_value(route: Route, value: httpx.Response | httpx2.Response | None) -> None:
    if isinstance(value, httpx2.Response):
        value = _to_legacy_response(value)
    route._return_value = value


def _call_route_side_effect(
    route: Route,
    effect: Callable[..., httpx.Response | httpx2.Response | httpx.Request | None],
    request: httpx.Request,
    **kwargs: object,
) -> httpx.Response | httpx.Request | None:
    parameters = inspect.getfullargspec(effect)
    if "route" in kwargs:
        warnings.warn(f"Matched context contains reserved word `route`: {route.pattern!r}")
    if "route" in parameters.args:
        kwargs["route"] = route

    try:
        result = effect(request, **kwargs)
    except Exception as error:
        raise SideEffectError(route, origin=error) from error

    if isinstance(result, httpx2.Response):
        return _to_legacy_response(result, request)
    if isinstance(result, Awaitable):

        async def await_legacy_response() -> httpx.Response:
            awaited: Final = await result
            if isinstance(awaited, httpx2.Response):
                return _to_legacy_response(awaited, request)
            return awaited

        return await_legacy_response()
    if result is not None and not isinstance(result, (httpx.Response, httpx.Request)):
        raise TypeError(
            f"Side effects must return either an httpx.Response, an httpx2.Response, "
            f"a request for pass-through, or None. Got {result!r}"
        )
    return result


Route.return_value = property(Route.return_value.fget, _store_route_return_value)
Route._call_side_effect = _call_route_side_effect


class HTTPX2Mocker(Mocker):
    name = "httpx2"
    targets = (
        "httpx._client.Client",
        "httpx._client.AsyncClient",
        "httpx2._client.Client",
        "httpx2._client.AsyncClient",
    )
    target_methods = ("_transport_for_url",)

    @classmethod
    def mock(cls, spec: TransportSpec) -> Callable[..., httpx.BaseTransport | httpx2.BaseTransport]:
        def legacy_transport_for_url(instance: httpx.Client | httpx.AsyncClient, *args: object) -> httpx.BaseTransport:
            handler: Final[Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]] = (
                cls.async_handler if inspect.iscoroutinefunction(instance.request) else cls.handler
            )
            return TryTransport([httpx.MockTransport(handler), spec(instance, *args)])

        def transport_for_url(
            instance: object, *args: object, **kwargs: object
        ) -> httpx.BaseTransport | httpx2.BaseTransport:
            is_async = inspect.iscoroutinefunction(instance.request)
            legacy_router_handler: Final[Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]] = (
                cls.async_handler if is_async else cls.handler
            )
            pass_through_transport: Final[HTTPX2Transport] = spec(instance, *args, **kwargs)
            if isinstance(pass_through_transport, (httpx2.ASGITransport, httpx2.WSGITransport)):
                return pass_through_transport

            def sync_handler(request: httpx2.Request) -> httpx2.Response:
                try:
                    response = legacy_router_handler(_to_legacy_request(request))
                except PassThrough:
                    return pass_through_transport.handle_request(request)
                return _to_httpx2_response(response, request)

            async def async_handler(
                request: httpx2.Request,
            ) -> httpx2.Response:
                try:
                    result = legacy_router_handler(_to_legacy_request(request))
                except PassThrough:
                    return await pass_through_transport.handle_async_request(request)
                if isinstance(result, Awaitable):
                    result = await result
                return await _to_httpx2_response_async(result, request)

            handler: Final = async_handler if is_async else sync_handler
            return httpx2.MockTransport(handler)

        def dispatch_transport_for_url(
            instance: object, *args: object, **kwargs: object
        ) -> httpx.BaseTransport | httpx2.BaseTransport:
            if isinstance(instance, TestClient):
                return spec(instance, *args, **kwargs)
            if isinstance(instance, (httpx.Client, httpx.AsyncClient)):
                return legacy_transport_for_url(instance, *args)
            return transport_for_url(instance, *args, **kwargs)

        return dispatch_transport_for_url


respx_mocks.DEFAULT_MOCKER = HTTPX2Mocker.name
