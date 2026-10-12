import ssl

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


class PinnedBackend(AutoBackend):
    def __init__(self, addresses):
        self.addresses = addresses

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        choices = self.addresses.get(host)
        if not choices:
            raise httpcore.ConnectError("UNVALIDATED_DESTINATION")
        error = None
        for address in choices:
            try:
                return await super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (OSError, httpcore.ConnectError) as exc:
                error = exc
        raise httpcore.ConnectError("VALIDATED_DESTINATION_UNAVAILABLE") from error


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class SafeHTTPTransport(httpx.AsyncBaseTransport):
    """Pin validated DNS addresses while preserving the original TLS hostname."""
    def __init__(self, host, addresses):
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PinnedBackend({host: addresses}), max_connections=2,
        )

    async def handle_async_request(self, request):
        response = await self.pool.handle_async_request(httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        ))
        return httpx.Response(response.status, headers=response.headers,
                              stream=ResponseStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()
