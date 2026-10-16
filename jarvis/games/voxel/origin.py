from urllib.parse import urlsplit


def same_origin(origin, host, scheme):
    try:
        value = urlsplit(origin)
        expected = "https" if scheme in {"https", "wss"} else "http"
        return bool(
            value.scheme == expected
            and value.netloc == host
            and (not value.username)
            and (not value.password)
            and (value.path in {"", "/"})
            and (not value.query)
            and (not value.fragment)
        )
    except (ValueError, TypeError):
        return False


class VoxelOriginGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        if scope["type"] == "websocket" and scope.get("path") in {
            "/ws",
            "/games/text",
            "/games/voxel/stream",
        }:
            if not same_origin(
                headers.get(b"origin", b"").decode("latin1"),
                headers.get(b"host", b"").decode("latin1"),
                scope.get("scheme"),
            ):
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1008,
                        "reason": "Same-origin browser session required",
                    }
                )
                return

        async def respond(message):
            if (
                scope["type"] == "http"
                and message["type"] == "http.response.start"
                and (
                    scope.get("path") == "/"
                    or scope.get("path", "").startswith("/static/")
                )
            ):
                message = {
                    **message,
                    "headers": [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() != b"cache-control"
                    ]
                    + [(b"cache-control", b"no-cache, must-revalidate")],
                }
            if (
                scope["type"] == "http"
                and scope.get("path") == "/"
                and (message["type"] == "http.response.start")
            ):
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (
                            b"content-security-policy",
                            b"frame-ancestors 'self'; base-uri 'self'; object-src 'none'",
                        ),
                        (b"x-frame-options", b"SAMEORIGIN"),
                    ],
                }
            await send(message)

        await self.app(scope, receive, respond)
