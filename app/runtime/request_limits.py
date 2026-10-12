from starlette.responses import JSONResponse


class RequestLimitMiddleware:
    """Bound the actual stream, including chunked requests without Content-Length."""
    def __init__(self, app, limit=5 * 1024 * 1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                response = JSONResponse({"error": {"code": "REQUEST_SIZE_LIMIT"}}, status_code=413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def bounded_receive():
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, bounded_receive, send)
