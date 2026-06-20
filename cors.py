import json
import os
import traceback
from urllib.parse import quote
from fastapi import Request, Response, Cookie
from fastapi.responses import RedirectResponse
from request_helper import Requester
from typing import Annotated


async def cors(request: Request, origins, method="GET") -> Response:
    current_domain = request.headers.get("origin")
    if current_domain is None:
        current_domain = origins
    if current_domain not in origins.replace(", ", ",").split(",") and origins != "*":
        return Response()
    if not request.query_params.get('url'):
        return Response()
    file_type = request.query_params.get('type')
    requested = Requester(str(request.url))
    # Behind nginx/Cloudflare the app sees plain http, so rewritten playlist/
    # segment URLs would point to http:// and the browser's CORS request would
    # hit a CORS-less 301 redirect to https -> blocked. Force the external
    # scheme to https (the proxy is always served over https in production),
    # honoring X-Forwarded-Proto when present and falling back to http only for
    # direct local access.
    fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    is_local = requested.domain.split(":")[0] in ("localhost", "127.0.0.1")
    ext_scheme = fwd_proto or ("http" if is_local else "https")
    ext_host = ext_scheme + "://" + requested.domain
    main_url = ext_host + requested.path + "?url="
    # Carry the headers param (e.g. the Referer the CDN requires for hotlink
    # protection) onto rewritten child playlist/segment URLs, so the whole
    # chain is fetched with the same headers instead of being blocked.
    raw_headers_param = request.query_params.get("headers")
    child_suffix = ("&headers=" + quote(raw_headers_param)) if raw_headers_param else ""
    url = requested.query_params.get("url")
    url += "?"+requested.query_string(requested.remaining_params)
    requested = Requester(url)
    hdrs = request.headers.mutablecopy()
    hdrs["Accept-Encoding"] = ""
    hdrs.update(json.loads(request.query_params.get("headers", "{}").replace("'", '"')))
    try:
        content, headers, code, cookies = requested.get(
            data=None,
            headers=hdrs,
            cookies=request.cookies,
            method=request.query_params.get("method", method),
            json_data=json.loads(request.query_params.get("json", "{}")),
            additional_params=json.loads(request.query_params.get('params', '{}'))
        )
    except Exception:
        traceback.print_exc()
        raise
    headers['Access-Control-Allow-Origin'] = current_domain
    # if "text/html" not in headers.get('Content-Type'):
    #     headers['Content-Disposition'] = 'attachment; filename="master.m3u8"'
    del_keys = [
        'Vary',
        # 'Server',
        # 'Report-To',
        # 'NEL',
        'Content-Encoding',
        'Transfer-Encoding',
        'Content-Length',
        # "Content-Type"
    ]
    for key in del_keys:
        headers.pop(key, None)

    if (file_type == "m3u8" or ".m3u8" in url) and code != 404:
        content = content.decode("utf-8")
        # Ad segments injected by the source point at these CDNs / paths and are
        # IP-signed, so they 403 through the proxy and break HLS playback. Drop
        # each ad segment line along with its preceding #EXTINF (and any lone
        # #EXT-X-DISCONTINUITY) so the playlist stays valid.
        ad_markers = ("ad-site-i18n", "ad-site-sign", "ibyteimg.com", "/ad-site-")
        out_lines = []
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and any(m in line for m in ad_markers):
                while out_lines and out_lines[-1].strip().startswith(("#EXTINF", "#EXT-X-DISCONTINUITY")):
                    out_lines.pop()
                continue
            if line.startswith("#"):
                out_lines.append(line)
            elif line.startswith('/'):
                out_lines.append(main_url + requested.safe_sub(requested.host + line) + child_suffix)
            elif line.startswith('http'):
                out_lines.append(main_url + requested.safe_sub(line) + child_suffix)
            elif line.strip(' '):
                out_lines.append(main_url + requested.safe_sub(
                    requested.host +
                    '/'.join(str(requested.path).split('?')[0].split('/')[:-1]) +
                    '/' +
                    requested.safe_sub(line)
                ) + child_suffix)
            else:
                out_lines.append(line)
        content = "\n".join(out_lines)
    if "location" in headers:
        if headers["location"].startswith("/"):
            headers["location"] = requested.host + headers["location"]
        headers["location"] = main_url + headers["location"]
    resp = Response(content, code, headers=headers)
    resp.set_cookie("_last_requested", requested.host, max_age=3600, httponly=True)
    return resp


def _cors_headers(origin: str, origins: str) -> dict:
    allowed = origin if origin and (origins == "*" or origin in origins.replace(", ", ",").split(",")) else origins
    return {
        "Access-Control-Allow-Origin": allowed if allowed else "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Credentials": "true",
    }


def add_cors(app, origins, setup_with_no_url_param=False):
    cors_path = os.getenv('cors_url', '/cors')

    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def attach_cors_headers(request: Request, call_next):
        origin = request.headers.get("origin", "")
        try:
            response = await call_next(request)
        except Exception as exc:
            headers = _cors_headers(origin, origins)
            return JSONResponse({"detail": str(exc)}, status_code=500, headers=headers)
        for k, v in _cors_headers(origin, origins).items():
            response.headers[k] = v
        return response

    @app.options(cors_path)
    async def cors_preflight(request: Request) -> Response:
        origin = request.headers.get("origin", "")
        return Response(status_code=204, headers=_cors_headers(origin, origins))

    @app.get(cors_path)
    async def cors_caller(request: Request) -> Response:
        return await cors(request, origins=origins)

    @app.post(cors_path)
    async def cors_caller_post(request: Request) -> Response:
        return await cors(request, origins=origins, method="POST")

    if setup_with_no_url_param:
        @app.get("/{mistaken_relative:path}")
        async def cors_caller_for_relative(request: Request, mistaken_relative: str, _last_requested: Annotated[str, Cookie(...)]) -> RedirectResponse:
            x = Requester(str(request.url))
            x = x.query_string(x.query_params)
            resp = RedirectResponse(f"/cors?url={_last_requested}/{mistaken_relative}{'&' + x if x else ''}")
            return resp

        @app.post("/{mistaken_relative:path}")
        async def cors_caller_for_relative(request: Request, mistaken_relative: str,
                                           _last_requested: Annotated[str, Cookie(...)]) -> RedirectResponse:
            x = Requester(str(request.url))
            x = x.query_string(x.query_params)
            resp = RedirectResponse(f"/cors?url={_last_requested}/{mistaken_relative}{'&' + x if x else ''}")
            return resp
