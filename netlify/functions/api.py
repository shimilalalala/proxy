import json
import os
from urllib.parse import urlparse, parse_qs, quote, unquote
from typing import Annotated

import requests as http_requests
from fastapi import FastAPI, Request, Response, Cookie
from fastapi.responses import RedirectResponse
from mangum import Mangum


class Requester:
    def __init__(self, url):
        parsed_url = urlparse(url)
        self.url = url
        self.schema = parsed_url.scheme
        self.domain = parsed_url.netloc
        self.query_params = self.query(parsed_url)
        self.host = self.get_host(parsed_url)
        self.path = parsed_url.path
        params = self.query_params.copy()
        params.pop("url", None)
        params.pop("type", None)
        params.pop("headers", None)
        params.pop("method", None)
        params.pop("json", None)
        params.pop("params", None)
        self.remaining_params = params
        self.req_url = self.host + self.path + "?" + self.query_string(params)
        self.base_headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'connection': 'keep-alive',
            'referer': None,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "Linux",
        }

    def get(self, data=None, headers=None, method='get', json_data=None, additional_params=None, cookies=None):
        headers = self.headers(headers)
        try:
            additional_params = json.loads(additional_params)
        except (json.JSONDecodeError, TypeError):
            pass
        additional_params = {} if additional_params is None or type(additional_params) != dict else additional_params
        cookies = cookies if cookies else {}
        json_data = {} if json_data is None else json_data
        if additional_params:
            self.req_url += "&" if "=" in self.req_url else ""
            self.req_url += self.query_string(additional_params)
        self.req_url = self.req_url.replace("%3F", "&").replace("%3f", "&").replace("%3D", "=").replace("%3d", "=").replace("%20", " ")
        if method == "post":
            data = http_requests.post(self.req_url, headers=headers, data=data, timeout=9,
                                      json=json_data, allow_redirects=False, cookies=cookies)
        else:
            data = http_requests.get(self.req_url, headers=headers, data=data, timeout=9,
                                     json=json_data, allow_redirects=False, cookies=cookies)
        return [data.content, data.headers, data.status_code, data.cookies]

    def headers(self, headers):
        header = self.base_headers.copy()
        header.update(headers if headers is not None else header)
        header.pop("host", None)
        header.pop("cookie", None)
        return header

    @staticmethod
    def safe_sub(url):
        return quote(url)

    @staticmethod
    def query(parsed_url):
        return {k: unquote(str(v[0]), 'utf-8') for k, v in dict(parse_qs(parsed_url.query)).items()}

    @staticmethod
    def query_string(queries: dict):
        strings = []
        for query in queries:
            strings.append(query + "=" + quote(queries[query]))
        return "&".join(strings)

    @staticmethod
    def get_host(parsed_url):
        return parsed_url.scheme + '://' + parsed_url.netloc


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
    main_url = requested.host + requested.path + "?url="
    url = requested.query_params.get("url")
    url += "?" + requested.query_string(requested.remaining_params)
    requested = Requester(url)
    hdrs = request.headers.mutablecopy()
    hdrs["Accept-Encoding"] = ""
    hdrs.update(json.loads(request.query_params.get("headers", "{}").replace("'", '"')))
    content, headers, code, cookies = requested.get(
        data=None,
        headers=hdrs,
        cookies=request.cookies,
        method=request.query_params.get("method", method),
        json_data=json.loads(request.query_params.get("json", "{}")),
        additional_params=json.loads(request.get('params', '{}'))
    )
    headers = dict(headers)
    headers['Access-Control-Allow-Origin'] = current_domain
    for key in ['Vary', 'Content-Encoding', 'Transfer-Encoding', 'Content-Length']:
        headers.pop(key, None)
    if (file_type == "m3u8" or ".m3u8" in url) and code != 404:
        content = content.decode("utf-8")
        new_content = ""
        for line in content.split("\n"):
            if line.startswith("#"):
                new_content += line
            elif line.startswith('/'):
                new_content += main_url + requested.safe_sub(requested.host + line)
            elif line.startswith('http'):
                new_content += main_url + requested.safe_sub(line)
            elif line.strip(' '):
                new_content += main_url + requested.safe_sub(
                    requested.host +
                    '/'.join(str(requested.path).split('?')[0].split('/')[:-1]) +
                    '/' + requested.safe_sub(line)
                )
            new_content += "\n"
        content = new_content
    if "location" in headers:
        if headers["location"].startswith("/"):
            headers["location"] = requested.host + headers["location"]
        headers["location"] = main_url + headers["location"]
    resp = Response(content, code, headers=headers)
    resp.set_cookie("_last_requested", requested.host, max_age=3600, httponly=True)
    return resp


allowed_origins = os.getenv("origins", "*")
cors_path = os.getenv('cors_url', '/cors')

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


@app.get(cors_path)
async def cors_caller(request: Request) -> Response:
    return await cors(request, origins=allowed_origins)


@app.post(cors_path)
async def cors_caller_post(request: Request) -> Response:
    return await cors(request, origins=allowed_origins, method="POST")


handler = Mangum(app, lifespan="off")
