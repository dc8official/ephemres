"""Zero-dependency ASGI framework and HTTP toolkit for Ephemres.

Implements FastAPI, Starlette, APIRouter, Dependency Injection, Middleware,
Request/Response, and TestClient using 100% Python Standard Library.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping
import concurrent.futures
import contextlib
import copy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import functools
import http.cookies
from http import HTTPStatus
import inspect
import io
import json
import logging
import os
import re
import sys
import threading
import types
import typing
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    get_args,
    get_origin,
)
import urllib.parse
from uuid import UUID

from app.core.schema import (
    BaseModel,
    TypeAdapter,
    ValidationError,
    _FieldInfo,
    _coerce_value,
    _validate_constraints,
)

RequestValidationError = ValidationError

MAX_BODY_BYTES = int(os.environ.get("EPHEMRES_MAX_BODY_BYTES", 10 * 1024 * 1024))

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 1. HTTP Status Codes & Exceptions
# ---------------------------------------------------------------------------
class _StatusNamespace:
    """Standard HTTP status codes matching Starlette / FastAPI status constants."""

    HTTP_100_CONTINUE = 100
    HTTP_101_SWITCHING_PROTOCOLS = 101
    HTTP_200_OK = 200
    HTTP_201_CREATED = 201
    HTTP_202_ACCEPTED = 202
    HTTP_204_NO_CONTENT = 204
    HTTP_301_MOVED_PERMANENTLY = 301
    HTTP_302_FOUND = 302
    HTTP_304_NOT_MODIFIED = 304
    HTTP_307_TEMPORARY_REDIRECT = 307
    HTTP_308_PERMANENT_REDIRECT = 308
    HTTP_400_BAD_REQUEST = 400
    HTTP_401_UNAUTHORIZED = 401
    HTTP_403_FORBIDDEN = 403
    HTTP_404_NOT_FOUND = 404
    HTTP_405_METHOD_NOT_ALLOWED = 405
    HTTP_408_REQUEST_TIMEOUT = 408
    HTTP_409_CONFLICT = 409
    HTTP_410_GONE = 410
    HTTP_422_UNPROCESSABLE_ENTITY = 422
    HTTP_429_TOO_MANY_REQUESTS = 429
    HTTP_500_INTERNAL_SERVER_ERROR = 500
    HTTP_501_NOT_IMPLEMENTED = 501
    HTTP_502_BAD_GATEWAY = 502
    HTTP_503_SERVICE_UNAVAILABLE = 503
    HTTP_504_GATEWAY_TIMEOUT = 504

    def __getattr__(self, name: str) -> int:
        if name.startswith("HTTP_"):
            code_str = name.split("_")[1]
            try:
                return int(code_str)
            except ValueError:
                pass
        raise AttributeError(f"Unknown status code '{name}'")


status = _StatusNamespace()


class HTTPException(Exception):
    """HTTP Exception that results in an automated HTTP error response."""

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail if detail is not None else HTTPStatus(status_code).phrase
        self.headers = headers or {}
        super().__init__(self.detail)


# ---------------------------------------------------------------------------
# 2. Dependency Injection & Parameter Markers
# ---------------------------------------------------------------------------
class Depends:
    """Dependency injection marker."""

    def __init__(
        self,
        dependency: Optional[Callable[..., Any]] = None,
        *,
        use_cache: bool = True,
    ) -> None:
        self.dependency = dependency
        self.use_cache = use_cache

    def __repr__(self) -> str:
        dep_name = getattr(self.dependency, "__name__", str(self.dependency))
        return f"Depends({dep_name})"


class _ParamInfo(_FieldInfo):
    """Base class for HTTP parameter extractors (Query, Path, Header, Cookie, Body)."""

    def __init__(
        self,
        default: Any = ...,
        *,
        alias: Optional[str] = None,
        ge: Optional[Union[int, float]] = None,
        gt: Optional[Union[int, float]] = None,
        le: Optional[Union[int, float]] = None,
        lt: Optional[Union[int, float]] = None,
        min_length: Optional[int] = None,
        max_length: Optional[int] = None,
        pattern: Optional[Union[str, re.Pattern[str]]] = None,
        description: Optional[str] = None,
        title: Optional[str] = None,
        include_in_schema: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            default=default,
            alias=alias,
            ge=ge,
            gt=gt,
            le=le,
            lt=lt,
            min_length=min_length,
            max_length=max_length,
            pattern=pattern,
            description=description,
            title=title,
            extra=kwargs,
        )
        self.include_in_schema = include_in_schema

    def __bool__(self) -> bool:
        if self.default is None or self.default is ... or self.default is inspect._empty:
            return False
        return bool(self.default)


class QueryParamInfo(_ParamInfo):
    pass


class PathParamInfo(_ParamInfo):
    pass


class HeaderParamInfo(_ParamInfo):
    pass


class CookieParamInfo(_ParamInfo):
    pass


class BodyParamInfo(_ParamInfo):
    pass


def Query(default: Any = ..., **kwargs: Any) -> Any:
    return QueryParamInfo(default=default, **kwargs)


def Path(default: Any = ..., **kwargs: Any) -> Any:
    return PathParamInfo(default=default, **kwargs)


def Header(default: Any = ..., **kwargs: Any) -> Any:
    return HeaderParamInfo(default=default, **kwargs)


def Cookie(default: Any = ..., **kwargs: Any) -> Any:
    return CookieParamInfo(default=default, **kwargs)


def Body(default: Any = ..., **kwargs: Any) -> Any:
    return BodyParamInfo(default=default, **kwargs)


class BackgroundTasks:
    """Manages asynchronous or synchronous background tasks executing after response return."""

    def __init__(self) -> None:
        self.tasks: List[Tuple[Callable, Tuple, Dict]] = []

    def add_task(self, func: Callable, *args: Any, **kwargs: Any) -> None:
        self.tasks.append((func, args, kwargs))

    async def __call__(self) -> None:
        for func, args, kwargs in self.tasks:
            try:
                if inspect.iscoroutinefunction(func):
                    await func(*args, **kwargs)
                else:
                    res = func(*args, **kwargs)
                    if inspect.isawaitable(res):
                        await res
            except Exception as exc:
                logger.error("Background task %s failed: %s", func, exc, exc_info=True)


# ---------------------------------------------------------------------------
# 3. Headers, URL, & QueryParams
# ---------------------------------------------------------------------------
class MutableHeaders:
    """Case-insensitive, multi-value header container."""

    def __init__(
        self,
        raw: Optional[Sequence[Tuple[bytes, bytes]]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._list: List[Tuple[str, str]] = []
        if raw:
            for k, v in raw:
                self._list.append((k.decode("latin-1"), v.decode("latin-1")))
        elif headers:
            for k, v in headers.items():
                self._list.append((str(k), str(v)))

    def get(self, key: str, default: Any = None) -> Any:
        k_low = key.lower()
        for k, v in reversed(self._list):
            if k.lower() == k_low:
                return v
        return default

    def getlist(self, key: str) -> List[str]:
        k_low = key.lower()
        return [v for k, v in self._list if k.lower() == k_low]

    def append(self, key: str, value: str) -> None:
        self._list.append((key, value))

    def __setitem__(self, key: str, value: str) -> None:
        k_low = key.lower()
        self._list = [(k, v) for k, v in self._list if k.lower() != k_low]
        self._list.append((key, value))

    def __getitem__(self, key: str) -> str:
        val = self.get(key)
        if val is None:
            raise KeyError(key)
        return val

    def __delitem__(self, key: str) -> None:
        k_low = key.lower()
        self._list = [(k, v) for k, v in self._list if k.lower() != k_low]

    def __contains__(self, key: str) -> bool:
        k_low = key.lower()
        return any(k.lower() == k_low for k, _ in self._list)

    def items(self) -> List[Tuple[str, str]]:
        return list(self._list)

    def keys(self) -> List[str]:
        return [k for k, _ in self._list]

    def values(self) -> List[str]:
        return [v for _, v in self._list]

    @property
    def raw(self) -> List[Tuple[bytes, bytes]]:
        return [
            (k.lower().encode("latin-1"), v.encode("latin-1"))
            for k, v in self._list
        ]

    def __repr__(self) -> str:
        return f"Headers({dict(self._list)})"


class URL:
    """Parsed URL attributes."""

    def __init__(self, scope: Dict[str, Any]) -> None:
        self.scheme = scope.get("scheme", "http")
        self.path = scope.get("path", "/")
        qs = scope.get("query_string", b"")
        self.query = qs.decode("latin-1") if isinstance(qs, bytes) else str(qs)

        headers = MutableHeaders(raw=scope.get("headers", []))
        host_header = headers.get("host")
        server = scope.get("server")
        if host_header:
            self.netloc = host_header
        elif server:
            host, port = server
            default_port = 443 if self.scheme == "https" else 80
            self.netloc = f"{host}:{port}" if port != default_port else host
        else:
            self.netloc = "localhost"

    @property
    def hostname(self) -> str:
        return self.netloc.split(":")[0]

    @property
    def port(self) -> Optional[int]:
        parts = self.netloc.split(":")
        if len(parts) > 1:
            try:
                return int(parts[1])
            except ValueError:
                pass
        return None

    def __str__(self) -> str:
        q = f"?{self.query}" if self.query else ""
        return f"{self.scheme}://{self.netloc}{self.path}{q}"

    def __repr__(self) -> str:
        return f"URL({str(self)!r})"


class QueryParams(Mapping[str, str]):
    """Immutable multi-dict query string view."""

    def __init__(self, query_string: Union[str, bytes]) -> None:
        qs = query_string.decode("latin-1") if isinstance(query_string, bytes) else query_string
        self._dict: Dict[str, List[str]] = urllib.parse.parse_qs(qs, keep_blank_values=True)

    def get(self, key: str, default: Any = None) -> Any:
        vals = self._dict.get(key)
        return vals[0] if vals else default

    def getlist(self, key: str) -> List[str]:
        return list(self._dict.get(key, []))

    def __getitem__(self, key: str) -> str:
        vals = self._dict.get(key)
        if not vals:
            raise KeyError(key)
        return vals[0]

    def __contains__(self, key: object) -> bool:
        return key in self._dict

    def __iter__(self) -> Iterator[str]:
        return iter(self._dict)

    def __len__(self) -> int:
        return len(self._dict)

    def __repr__(self) -> str:
        return f"QueryParams({dict(self)})"


@dataclass
class Address:
    host: str
    port: int


# ---------------------------------------------------------------------------
# 4. Request & Response
# ---------------------------------------------------------------------------
class Request:
    """ASGI HTTP Request representation."""

    def __init__(
        self,
        scope: Dict[str, Any],
        receive: Optional[Callable[[], Any]] = None,
        send: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self.scope = scope
        self._receive = receive or (lambda: asyncio.sleep(0, result={"type": "http.request"}))
        self._send = send
        self.method: str = scope.get("method", "GET").upper()
        self.url: URL = URL(scope)
        self.headers: MutableHeaders = MutableHeaders(raw=scope.get("headers", []))
        self.path_params: Dict[str, Any] = scope.get("path_params", {})
        self.query_params: QueryParams = QueryParams(scope.get("query_string", b""))
        self.state: types.SimpleNamespace = scope.get("state", types.SimpleNamespace())
        self._body: Optional[bytes] = None
        self._is_disconnected: bool = False

        # Client address
        client = scope.get("client")
        if client:
            self.client: Optional[Address] = Address(host=client[0], port=client[1])
        else:
            self.client = Address(host="127.0.0.1", port=0)

        # Parse cookies
        cookie_header = self.headers.get("cookie", "")
        self.cookies: Dict[str, str] = {}
        if cookie_header:
            try:
                cookie_obj = http.cookies.SimpleCookie(cookie_header)
                for k, morsel in cookie_obj.items():
                    self.cookies[k] = morsel.value
            except Exception:
                for chunk in cookie_header.split(";"):
                    if "=" in chunk:
                        k, v = chunk.split("=", 1)
                        self.cookies[k.strip()] = v.strip().strip('"')

    async def body(self) -> bytes:
        if self._body is None:
            chunks = []
            total_size = 0
            while True:
                message = await self._receive()
                m_type = message.get("type", "")
                if m_type == "http.request":
                    chunk = message.get("body", b"")
                    if chunk:
                        total_size += len(chunk)
                        if total_size > MAX_BODY_BYTES:
                            raise HTTPException(413, "Request body too large")
                        chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                elif m_type == "http.disconnect":
                    self._is_disconnected = True
                    break
            self._body = b"".join(chunks)
        elif len(self._body) > MAX_BODY_BYTES:
            raise HTTPException(413, "Request body too large")
        return self._body

    async def json(self) -> Any:
        raw_body = await self.body()
        if not raw_body:
            return None
        return json.loads(raw_body.decode("utf-8"))

    async def is_disconnected(self) -> bool:
        if self._is_disconnected:
            return True
        try:
            message = await asyncio.wait_for(self._receive(), timeout=0.001)
            if message.get("type") == "http.disconnect":
                self._is_disconnected = True
                return True
        except asyncio.TimeoutError:
            pass
        return self._is_disconnected


class Response:
    """HTTP Response representation."""

    media_type: Optional[str] = None
    charset: str = "utf-8"

    def __init__(
        self,
        content: Any = None,
        status_code: int = 200,
        headers: Optional[Mapping[str, str]] = None,
        media_type: Optional[str] = None,
    ) -> None:
        self.status_code = status_code
        if media_type is not None:
            self.media_type = media_type
        self.headers = MutableHeaders(headers=headers)
        self.body = self.render(content)
        self._init_headers()

    def _init_headers(self) -> None:
        if self.body is not None and "content-length" not in self.headers:
            self.headers["content-length"] = str(len(self.body))
        if self.media_type and "content-type" not in self.headers:
            ct = self.media_type
            if ct.startswith("text/") and "charset=" not in ct:
                ct += f"; charset={self.charset}"
            self.headers["content-type"] = ct

    def render(self, content: Any) -> bytes:
        if content is None:
            return b""
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode(self.charset)
        return str(content).encode(self.charset)

    def set_cookie(
        self,
        key: str,
        value: str = "",
        max_age: Optional[int] = None,
        expires: Optional[Union[datetime, str, int]] = None,
        path: str = "/",
        domain: Optional[str] = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: Optional[str] = "lax",
    ) -> None:
        cookie_parts = [f"{key}={value}", f"Path={path}"]
        if max_age is not None:
            cookie_parts.append(f"Max-Age={int(max_age)}")
        if expires is not None:
            if isinstance(expires, datetime):
                exp_str = expires.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
                cookie_parts.append(f"Expires={exp_str}")
            elif isinstance(expires, (int, float)):
                if expires == 0:
                    cookie_parts.append("Expires=Thu, 01 Jan 1970 00:00:00 GMT")
                else:
                    exp_dt = datetime.fromtimestamp(expires, tz=timezone.utc)
                    cookie_parts.append(f"Expires={exp_dt.strftime('%a, %d %b %Y %H:%M:%S GMT')}")
            else:
                cookie_parts.append(f"Expires={expires}")
        if domain:
            cookie_parts.append(f"Domain={domain}")
        if secure:
            cookie_parts.append("Secure")
        if httponly:
            cookie_parts.append("HttpOnly")
        if samesite:
            s_name = samesite.capitalize() if samesite.lower() in ("lax", "strict", "none") else samesite
            cookie_parts.append(f"SameSite={s_name}")

        self.headers.append("set-cookie", "; ".join(cookie_parts))

    def delete_cookie(
        self,
        key: str,
        path: str = "/",
        domain: Optional[str] = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: Optional[str] = "lax",
    ) -> None:
        self.set_cookie(
            key=key,
            value="",
            max_age=0,
            expires=0,
            path=path,
            domain=domain,
            secure=secure,
            httponly=httponly,
            samesite=samesite,
        )

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[Dict[str, Any]], Any],
    ) -> None:
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": self.headers.raw,
        })
        await send({
            "type": "http.response.body",
            "body": self.body,
            "more_body": False,
        })


def _json_serialize(obj: Any) -> Any:
    """Recursively serialize Python / BaseModel data into JSON-safe data structures."""
    if obj is None:
        return None
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", by_alias=True)
    if isinstance(obj, (UUID, Enum)):
        return str(obj) if isinstance(obj, UUID) else obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (list, tuple, set)):
        return [_json_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): _json_serialize(v) for k, v in obj.items()}
    return obj


class JSONResponse(Response):
    """JSON HTTP Response."""

    media_type = "application/json"

    def __init__(
        self,
        content: Any,
        status_code: int = 200,
        headers: Optional[Mapping[str, str]] = None,
        media_type: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            content=content,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )

    def render(self, content: Any) -> bytes:
        serialized = _json_serialize(content)
        return json.dumps(serialized, default=str).encode(self.charset)


class HTMLResponse(Response):
    media_type = "text/html"


class PlainTextResponse(Response):
    media_type = "text/plain"


class RedirectResponse(Response):
    def __init__(
        self,
        url: str,
        status_code: int = 307,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(content=b"", status_code=status_code, headers=headers)
        self.headers["location"] = quote_location(url)


def quote_location(url: str) -> str:
    return urllib.parse.quote(url, safe="@:/?#[]!$&'()*+,;=")


class StreamingResponse(Response):
    """Streaming HTTP Response supporting async iterators, sync generators, and file-like objects."""

    def __init__(
        self,
        content: Any,
        status_code: int = 200,
        headers: Optional[Mapping[str, str]] = None,
        media_type: Optional[str] = None,
    ) -> None:
        self.content_iterator = content
        super().__init__(
            content=None,
            status_code=status_code,
            headers=headers,
            media_type=media_type,
        )

    def _init_headers(self) -> None:
        if self.media_type and "content-type" not in self.headers:
            ct = self.media_type
            if ct.startswith("text/") and "charset=" not in ct:
                ct += f"; charset={self.charset}"
            self.headers["content-type"] = ct

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[Dict[str, Any]], Any],
    ) -> None:
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": self.headers.raw,
        })

        content = self.content_iterator
        if hasattr(content, "read"):
            # File-like object (e.g. io.BytesIO)
            chunk_size = 64 * 1024
            while True:
                chunk = content.read(chunk_size)
                if not chunk:
                    break
                raw_bytes = chunk if isinstance(chunk, bytes) else chunk.encode(self.charset)
                await send({
                    "type": "http.response.body",
                    "body": raw_bytes,
                    "more_body": True,
                })
        elif hasattr(content, "__aiter__"):
            # Async generator / iterator
            async for chunk in content:
                raw_bytes = chunk if isinstance(chunk, bytes) else chunk.encode(self.charset)
                await send({
                    "type": "http.response.body",
                    "body": raw_bytes,
                    "more_body": True,
                })
        elif hasattr(content, "__iter__"):
            # Sync generator / iterator
            for chunk in content:
                raw_bytes = chunk if isinstance(chunk, bytes) else chunk.encode(self.charset)
                await send({
                    "type": "http.response.body",
                    "body": raw_bytes,
                    "more_body": True,
                })

        await send({
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        })


# ---------------------------------------------------------------------------
# 5. Routing Engine & Route Class
# ---------------------------------------------------------------------------
class Route:
    """Compiled route endpoint with path parameter extraction regex."""

    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        methods: Sequence[str],
        status_code: int = 200,
        response_model: Optional[Type[Any]] = None,
        name: Optional[str] = None,
        include_in_schema: bool = True,
        tags: Optional[List[str]] = None,
        dependencies: Optional[Sequence[Any]] = None,
    ) -> None:
        self.path = path.rstrip("/") if path != "/" else "/"
        self.endpoint = endpoint
        self.methods = set(m.upper() for m in methods)
        self.status_code = status_code
        self.response_model = response_model
        self.name = name or getattr(endpoint, "__name__", "endpoint")
        self.include_in_schema = include_in_schema
        self.tags = tags or []
        self.dependencies = list(dependencies or [])

        # Compile path regex for parameter extraction: {param}
        self.param_names: List[str] = []
        pattern = "^"
        segments = [s for s in path.strip("/").split("/") if s]
        self.num_static_segments = 0
        self.num_dynamic_params = 0

        for seg in segments:
            if seg.startswith("{") and seg.endswith("}"):
                p_name = seg[1:-1]
                self.param_names.append(p_name)
                pattern += f"/(?P<{p_name}>[^/]+)"
                self.num_dynamic_params += 1
            else:
                pattern += f"/{re.escape(seg)}"
                self.num_static_segments += 1

        if not segments:
            pattern += "/?"
        else:
            pattern += "/?"
        pattern += "$"
        self.regex = re.compile(pattern)

    def matches(self, request_path: str) -> Optional[Dict[str, str]]:
        match = self.regex.match(request_path)
        if match:
            return match.groupdict()
        return None

    @property
    def score(self) -> Tuple[int, int, int]:
        """Sorting key: Static-before-dynamic (0 dynamic params first, then more static segments)."""
        return (self.num_dynamic_params, -self.num_static_segments, -len(self.path))


class APIRouter:
    """Modular endpoint router with path prefix and dependency sharing."""

    def __init__(
        self,
        prefix: str = "",
        tags: Optional[List[str]] = None,
        dependencies: Optional[Sequence[Depends]] = None,
    ) -> None:
        self.prefix = prefix.rstrip("/")
        self.tags = tags or []
        self.dependencies = list(dependencies or [])
        self.routes: List[Route] = []

    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        methods: Sequence[str],
        *,
        status_code: int = 200,
        response_model: Optional[Type[Any]] = None,
        name: Optional[str] = None,
        include_in_schema: bool = True,
        tags: Optional[List[str]] = None,
        dependencies: Optional[Sequence[Any]] = None,
    ) -> None:
        clean_path = path if path.startswith("/") or not path else f"/{path}"
        full_path = f"{self.prefix}{clean_path}" if self.prefix else (clean_path or "/")
        merged_tags = list(self.tags) + (tags or [])
        merged_deps = list(self.dependencies) + list(dependencies or [])
        route = Route(
            path=full_path,
            endpoint=endpoint,
            methods=methods,
            status_code=status_code,
            response_model=response_model,
            name=name,
            include_in_schema=include_in_schema,
            tags=merged_tags,
            dependencies=merged_deps,
        )
        self.routes.append(route)

    def get(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self._add_method_decorator(["GET"], path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self._add_method_decorator(["POST"], path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self._add_method_decorator(["PUT"], path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self._add_method_decorator(["PATCH"], path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self._add_method_decorator(["DELETE"], path, **kwargs)

    def options(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self._add_method_decorator(["OPTIONS"], path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self._add_method_decorator(["HEAD"], path, **kwargs)

    def _add_method_decorator(
        self, methods: Sequence[str], path: str, **kwargs: Any
    ) -> Callable[[Any], Any]:
        caller_locals: Dict[str, Any] = {}
        frame = inspect.currentframe()
        while frame:
            if frame.f_code.co_name not in (
                "add_api_route",
                "_add_method_decorator",
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "options",
                "head",
            ):
                caller_locals.update(frame.f_locals)
                break
            frame = frame.f_back

        def decorator(func: Any) -> Any:
            try:
                func.__caller_locals__ = dict(caller_locals)
            except Exception:
                pass
            self.add_api_route(path, func, methods, **kwargs)
            return func

        return decorator

    def include_router(
        self,
        router: APIRouter,
        prefix: str = "",
        tags: Optional[List[str]] = None,
        dependencies: Optional[Sequence[Any]] = None,
    ) -> None:
        pfx = f"{self.prefix}{prefix.rstrip('/')}" if self.prefix else prefix.rstrip('/')
        for route in router.routes:
            sub_path = route.path
            merged_path = f"{pfx}{sub_path}" if pfx else sub_path
            merged_tags = list(self.tags) + (tags or []) + route.tags
            merged_deps = list(self.dependencies) + list(dependencies or []) + route.dependencies
            self.routes.append(
                Route(
                    path=merged_path,
                    endpoint=route.endpoint,
                    methods=list(route.methods),
                    status_code=route.status_code,
                    response_model=route.response_model,
                    name=route.name,
                    include_in_schema=route.include_in_schema,
                    tags=merged_tags,
                    dependencies=merged_deps,
                )
            )


# ---------------------------------------------------------------------------
# 6. Dependency & Parameter Resolver
# ---------------------------------------------------------------------------
async def _resolve_dependency(
    dep: Any,
    request: Request,
    injected_response: Response,
    dep_cache: Dict[Any, Any],
    exit_stack: contextlib.AsyncExitStack,
    dependency_overrides: Dict[Any, Any],
    path_params: Dict[str, str],
    bg_tasks: Optional[BackgroundTasks] = None,
) -> Any:
    """Resolves and invokes a Depends marker or callable dependency."""
    dep_obj = dep if isinstance(dep, Depends) else Depends(dep)
    dep_fn = dep_obj.dependency
    if dep_fn is None:
        return None

    target_fn = dependency_overrides.get(dep_fn, dep_fn)
    if dep_obj.use_cache and target_fn in dep_cache:
        return dep_cache[target_fn]

    sub_kwargs = await _resolve_endpoint_kwargs(
        target_fn,
        request,
        injected_response,
        dep_cache,
        exit_stack,
        dependency_overrides,
        path_params,
        bg_tasks=bg_tasks,
    )

    if inspect.isasyncgenfunction(target_fn):
        cm = contextlib.asynccontextmanager(target_fn)(**sub_kwargs)
        dep_val = await exit_stack.enter_async_context(cm)
    elif inspect.isgeneratorfunction(target_fn):
        cm = contextlib.contextmanager(target_fn)(**sub_kwargs)
        dep_val = exit_stack.enter_context(cm)
    elif inspect.iscoroutinefunction(target_fn):
        dep_val = await target_fn(**sub_kwargs)
    else:
        dep_val = target_fn(**sub_kwargs)
        if inspect.isasyncgen(dep_val):
            async def _aiter_wrap(gen=dep_val):
                try:
                    yield await anext(gen)
                finally:
                    await gen.aclose()
            cm = contextlib.asynccontextmanager(_aiter_wrap)()
            dep_val = await exit_stack.enter_async_context(cm)
        elif inspect.isgenerator(dep_val):
            def _iter_wrap(gen=dep_val):
                try:
                    yield next(gen)
                finally:
                    gen.close()
            cm = contextlib.contextmanager(_iter_wrap)()
            dep_val = exit_stack.enter_context(cm)
        elif inspect.isawaitable(dep_val):
            dep_val = await dep_val

    if dep_obj.use_cache:
        dep_cache[target_fn] = dep_val

    return dep_val


async def _resolve_endpoint_kwargs(
    handler: Callable[..., Any],
    request: Request,
    injected_response: Response,
    dep_cache: Dict[Any, Any],
    exit_stack: contextlib.AsyncExitStack,
    dependency_overrides: Dict[Any, Any],
    path_params: Dict[str, str],
    bg_tasks: Optional[BackgroundTasks] = None,
) -> Dict[str, Any]:
    """Resolves parameters for a route handler or dependency function."""
    sig = inspect.signature(handler)
    resolved_kwargs: Dict[str, Any] = {}
    validation_errors: List[Dict[str, Any]] = []

    type_hints: Dict[str, Any] = {}
    localns = getattr(handler, "__caller_locals__", {}) or {}
    if inspect.isroutine(handler) or isinstance(handler, type) or hasattr(handler, "__call__"):
        try:
            type_hints = typing.get_type_hints(handler, localns=localns)
        except Exception:
            try:
                type_hints = typing.get_type_hints(handler)
            except Exception:
                try:
                    if hasattr(handler, "__call__") and not isinstance(handler, type):
                        type_hints = typing.get_type_hints(handler.__call__, localns=localns)
                except Exception:
                    pass
    fn_globals = getattr(handler, "__globals__", {})

    # Parse JSON body once if any parameter expects request body
    body_data = None
    body_parsed = False
    body_json_invalid = False

    for param_name, param in sig.parameters.items():
        annotation = type_hints.get(param_name, param.annotation)
        if isinstance(annotation, str):
            if annotation in localns:
                annotation = localns[annotation]
            elif annotation in fn_globals:
                annotation = fn_globals[annotation]
            elif annotation in ("int", "str", "float", "bool", "bytes"):
                annotation = {"int": int, "str": str, "float": float, "bool": bool, "bytes": bytes}[annotation]
            elif annotation == "UUID":
                annotation = UUID
            elif annotation == "Response":
                annotation = Response
            elif annotation == "Request":
                annotation = Request
            elif annotation == "BackgroundTasks":
                annotation = BackgroundTasks
            else:
                try:
                    annotation = eval(annotation, fn_globals, localns)
                except Exception:
                    pass
        if annotation is inspect._empty:
            annotation = Any
        default = param.default

        # 1. Direct Request injection
        if annotation is Request or (
            annotation is Any
            and default is inspect._empty
            and param_name in ("request", "http_request")
        ):
            resolved_kwargs[param_name] = request
            continue

        # 2. Direct Response injection
        if annotation is Response or (
            annotation is Any
            and default is inspect._empty
            and param_name == "response"
        ):
            resolved_kwargs[param_name] = injected_response
            continue

        # 3. Direct BackgroundTasks injection
        is_bg = (
            annotation is BackgroundTasks
            or (isinstance(annotation, type) and issubclass(annotation, BackgroundTasks))
            or BackgroundTasks in get_args(annotation)
            or isinstance(default, BackgroundTasks)
            or (isinstance(default, type) and default is BackgroundTasks)
        )
        if is_bg:
            resolved_kwargs[param_name] = bg_tasks if bg_tasks is not None else BackgroundTasks()
            continue

        # 4. Depends(...) injection
        if isinstance(default, Depends):
            dep_val = await _resolve_dependency(
                default,
                request,
                injected_response,
                dep_cache,
                exit_stack,
                dependency_overrides,
                path_params,
                bg_tasks=bg_tasks,
            )
            resolved_kwargs[param_name] = dep_val
            continue

        # 5. Path parameter
        if param_name in path_params or isinstance(default, PathParamInfo):
            p_val = path_params.get(param_name)
            p_info = default if isinstance(default, PathParamInfo) else _FieldInfo(default=...)
            if p_val is None:
                if p_info.is_required():
                    validation_errors.append({
                        "loc": ("path", param_name),
                        "msg": "Missing path parameter",
                        "type": "missing",
                    })
                    continue
                resolved_kwargs[param_name] = p_info.get_default()
            else:
                try:
                    coerced = _coerce_value(p_val, annotation, param_name, {})
                    _validate_constraints(coerced, p_info, param_name)
                    resolved_kwargs[param_name] = coerced
                except Exception as exc:
                    validation_errors.append({
                        "loc": ("path", param_name),
                        "msg": str(exc) if isinstance(exc, (ValueError, TypeError)) else "Invalid value",
                        "type": "value_error",
                    })
            continue

        # 6. Header parameter
        if isinstance(default, HeaderParamInfo):
            h_alias = default.alias or param_name.replace("_", "-")
            h_val = request.headers.get(h_alias)
            if h_val is None:
                if default.is_required():
                    validation_errors.append({
                        "loc": ("header", h_alias),
                        "msg": "Header required",
                        "type": "missing",
                    })
                    continue
                resolved_kwargs[param_name] = default.get_default()
            else:
                try:
                    coerced = _coerce_value(h_val, annotation, param_name, {})
                    _validate_constraints(coerced, default, param_name)
                    resolved_kwargs[param_name] = coerced
                except Exception as exc:
                    validation_errors.append({
                        "loc": ("header", h_alias),
                        "msg": str(exc) if isinstance(exc, (ValueError, TypeError)) else "Invalid value",
                        "type": "value_error",
                    })
            continue

        # 7. Cookie parameter
        if isinstance(default, CookieParamInfo):
            c_alias = default.alias or param_name
            c_val = request.cookies.get(c_alias)
            if c_val is None:
                if default.is_required():
                    validation_errors.append({
                        "loc": ("cookie", c_alias),
                        "msg": "Cookie required",
                        "type": "missing",
                    })
                    continue
                resolved_kwargs[param_name] = default.get_default()
            else:
                try:
                    coerced = _coerce_value(c_val, annotation, param_name, {})
                    _validate_constraints(coerced, default, param_name)
                    resolved_kwargs[param_name] = coerced
                except Exception as exc:
                    validation_errors.append({
                        "loc": ("cookie", c_alias),
                        "msg": str(exc) if isinstance(exc, (ValueError, TypeError)) else "Invalid value",
                        "type": "value_error",
                    })
            continue

        # 8. Request Body parameter (BaseModel, Dict, List, Body)
        origin = get_origin(annotation)
        is_body = (
            isinstance(default, BodyParamInfo)
            or (isinstance(annotation, type) and issubclass(annotation, BaseModel))
            or (origin in (dict, Dict) and not isinstance(default, QueryParamInfo))
            or (annotation in (dict, Dict) and not isinstance(default, QueryParamInfo))
        )
        if is_body:
            if not body_parsed:
                raw_body = await request.body()
                if raw_body and raw_body.strip():
                    try:
                        body_data = json.loads(raw_body.decode("utf-8"))
                    except Exception:
                        body_json_invalid = True
                        body_data = None
                else:
                    body_data = None
                body_parsed = True

            if body_json_invalid:
                validation_errors.append({
                    "loc": ("body",),
                    "msg": "Invalid JSON body",
                    "type": "json_invalid",
                })
                continue

            b_info = default if isinstance(default, BodyParamInfo) else _FieldInfo(default=default)

            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                if body_data is None:
                    if b_info.is_required():
                        validation_errors.append({
                            "loc": ("body",),
                            "msg": "Request body required",
                            "type": "missing",
                        })
                    else:
                        resolved_kwargs[param_name] = b_info.get_default()
                else:
                    try:
                        model_inst = annotation.model_validate(body_data)
                        resolved_kwargs[param_name] = model_inst
                    except ValidationError as v_exc:
                        for err in v_exc.errors():
                            validation_errors.append({
                                "loc": ("body", *err.get("loc", ())),
                                "msg": err.get("msg", ""),
                                "type": err.get("type", "value_error"),
                            })
                    except Exception as exc:
                        validation_errors.append({
                            "loc": ("body",),
                            "msg": str(exc) if isinstance(exc, (ValueError, TypeError)) else "Invalid value",
                            "type": "value_error",
                        })
                continue

            # Generic dict / list body (e.g. Dict[str, InterfaceConfig])
            if origin in (dict, Dict, list, List) or isinstance(default, BodyParamInfo):
                if body_data is None:
                    if b_info.is_required():
                        validation_errors.append({
                            "loc": ("body",),
                            "msg": "Request body required",
                            "type": "missing",
                        })
                    else:
                        resolved_kwargs[param_name] = b_info.get_default()
                else:
                    try:
                        coerced = _coerce_value(body_data, annotation, param_name, {})
                        _validate_constraints(coerced, b_info, param_name)
                        resolved_kwargs[param_name] = coerced
                    except ValidationError as v_exc:
                        for err in v_exc.errors():
                            validation_errors.append({
                                "loc": ("body", *err.get("loc", ())),
                                "msg": err.get("msg", ""),
                                "type": err.get("type", "value_error"),
                            })
                    except Exception as exc:
                        validation_errors.append({
                            "loc": ("body",),
                            "msg": str(exc) if isinstance(exc, (ValueError, TypeError)) else "Invalid value",
                            "type": "value_error",
                        })
                continue

        # 9. Query parameter
        # Any other parameter is extracted from query params or falls back to default.
        q_info = default if isinstance(default, QueryParamInfo) else _FieldInfo(default=default)
        alias = q_info.alias or param_name
        origin = get_origin(annotation)
        if origin in (list, List, Sequence):
            raw_q = request.query_params.getlist(alias)
            has_val = bool(raw_q)
        else:
            raw_q = request.query_params.get(alias)
            has_val = raw_q is not None

        if has_val:
            try:
                coerced = _coerce_value(raw_q, annotation, param_name, {})
                _validate_constraints(coerced, q_info, param_name)
                resolved_kwargs[param_name] = coerced
            except Exception as exc:
                validation_errors.append({
                    "loc": ("query", alias),
                    "msg": str(exc) if isinstance(exc, (ValueError, TypeError)) else "Invalid value",
                    "type": "value_error",
                })
        elif not q_info.is_required():
            resolved_kwargs[param_name] = q_info.get_default()
        else:
            validation_errors.append({
                "loc": ("query", alias),
                "msg": f"Field required",
                "type": "missing",
            })
        continue

    if validation_errors:
        raise ValidationError(validation_errors, title="Request Validation")

    return resolved_kwargs


# ---------------------------------------------------------------------------
# 7. Middleware Architecture
# ---------------------------------------------------------------------------
class BaseHTTPMiddleware:
    """Base HTTP middleware wrapper matching Starlette / FastAPI dispatch contract."""

    def __init__(self, app: Any, **options: Any) -> None:
        self.app = app
        self.options = options

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[Dict[str, Any]], Any],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)

        async def call_next(req: Request) -> Response:
            start_event = asyncio.Event()
            body_queue: asyncio.Queue[Union[Tuple[bytes, bool], Exception]] = asyncio.Queue()
            response_status = 200
            response_headers: List[Tuple[bytes, bytes]] = []
            app_exc: Optional[Exception] = None

            async def inner_send(message: Dict[str, Any]) -> None:
                nonlocal response_status, response_headers
                m_type = message.get("type", "")
                if m_type == "http.response.start":
                    response_status = message.get("status", 200)
                    response_headers = list(message.get("headers", []))
                    start_event.set()
                elif m_type == "http.response.body":
                    chunk = message.get("body", b"")
                    more_body = message.get("more_body", False)
                    await body_queue.put((chunk, more_body))

            async def run_app() -> None:
                nonlocal app_exc
                try:
                    await self.app(req.scope, req._receive, inner_send)
                except Exception as exc:
                    app_exc = exc
                    await body_queue.put(exc)
                    start_event.set()

            inner_task = asyncio.create_task(run_app())

            # Wait for response start or early failure
            await start_event.wait()
            if app_exc is not None and not response_headers:
                raise app_exc

            async def body_stream() -> AsyncIterator[bytes]:
                try:
                    while True:
                        item = await body_queue.get()
                        if isinstance(item, Exception):
                            raise item
                        chunk, more_body = item
                        if chunk:
                            yield chunk
                        if not more_body:
                            break
                finally:
                    if not inner_task.done():
                        inner_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await inner_task

            # Create StreamingResponse with the received status & headers
            content_type = None
            for k, v in response_headers:
                if k.lower() == b"content-type":
                    content_type = v.decode("latin-1")
                    break

            resp = StreamingResponse(
                content=body_stream(),
                status_code=response_status,
                media_type=content_type,
            )
            resp.headers = MutableHeaders(raw=response_headers)
            return resp

        res = await self.dispatch(request, call_next)
        await res(scope, receive, send)

    async def dispatch(self, request: Request, call_next: Callable[[Request], Any]) -> Response:
        raise NotImplementedError("Middleware subclasses must implement dispatch()")


class CORSMiddleware:
    """Zero-dependency CORS middleware ensuring cross-origin security."""

    def __init__(
        self,
        app: Any,
        allow_origins: Optional[Sequence[str]] = None,
        allow_credentials: bool = False,
        allow_methods: Optional[Sequence[str]] = None,
        allow_headers: Optional[Sequence[str]] = None,
    ) -> None:
        self.app = app
        self.allow_origins = list(allow_origins or [])
        self.allow_credentials = allow_credentials
        self.allow_methods = [m.upper() for m in (allow_methods or ["GET"])]
        self.allow_headers = [h.lower() for h in (allow_headers or ["*"])]

        if "*" in self.allow_origins and self.allow_credentials:
            raise ValueError(
                "CORS configuration error: allowed_origins cannot contain '*' when allow_credentials=True."
            )

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[Dict[str, Any]], Any],
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)
        origin = request.headers.get("origin")

        # Handle Preflight OPTIONS
        if request.method == "OPTIONS" and origin:
            headers = self._get_cors_headers(origin)
            res = Response(content=b"", status_code=200, headers=headers)
            await res(scope, receive, send)
            return

        async def send_with_cors(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start" and origin:
                cors_headers = self._get_cors_headers(origin)
                raw_headers = list(message.get("headers", []))
                for k, v in cors_headers.items():
                    raw_headers.append((k.encode("latin-1"), v.encode("latin-1")))
                message["headers"] = raw_headers
            await send(message)

        await self.app(scope, receive, send_with_cors)

    def _get_cors_headers(self, origin: str) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if "*" in self.allow_origins:
            headers["Access-Control-Allow-Origin"] = "*"
        elif origin in self.allow_origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"

        if self.allow_credentials and "Access-Control-Allow-Origin" in headers:
            headers["Access-Control-Allow-Credentials"] = "true"

        if self.allow_methods:
            headers["Access-Control-Allow-Methods"] = ", ".join(self.allow_methods)
        if self.allow_headers:
            headers["Access-Control-Allow-Headers"] = (
                "*" if "*" in self.allow_headers else ", ".join(self.allow_headers)
            )
        return headers


# ---------------------------------------------------------------------------
# 8. FastAPI Application Core
# ---------------------------------------------------------------------------
class EphemresApp:
    """ASGI 3.0 Web Application with dependency injection, lifespan, and routing."""

    __test__ = False

    def __init__(
        self,
        title: str = "Ephemres Platform",
        description: str = "",
        version: str = "3.3.2.v",
        docs_url: Optional[str] = "/docs",
        redoc_url: Optional[str] = "/redoc",
        openapi_url: Optional[str] = "/openapi.json",
        lifespan: Optional[Callable[[EphemresApp], Any]] = None,
    ) -> None:
        self.title = title
        self.description = description
        self.version = version
        self.docs_url = docs_url
        self.__test__ = False
        self.redoc_url = redoc_url
        self.openapi_url = openapi_url
        self.lifespan = lifespan

        self.router = APIRouter()
        self.dependency_overrides: Dict[Any, Any] = {}
        self.exception_handlers: Dict[Type[Exception], Callable] = {}
        self.middleware_stack: List[Tuple[Type[Any], Dict[str, Any]]] = []
        self._compiled_app: Optional[Callable] = None
        self._routes_sorted: bool = False

        # Built-in OpenAPI route
        if self.openapi_url:
            self.add_api_route(
                self.openapi_url,
                self.openapi,
                methods=["GET"],
                include_in_schema=False,
            )

    def openapi(self) -> Dict[str, Any]:
        """Generate OpenAPI 3.1.0 schema document."""
        paths: Dict[str, Any] = {}
        for route in self.router.routes:
            if not route.include_in_schema:
                continue
            path_item = paths.setdefault(route.path, {})
            for m in route.methods:
                path_item[m.lower()] = {
                    "summary": route.name,
                    "operationId": f"{m.lower()}_{route.name}",
                    "tags": route.tags,
                    "responses": {
                        str(route.status_code): {"description": "Successful Response"}
                    },
                }
        return {
            "openapi": "3.1.0",
            "info": {
                "title": self.title,
                "description": self.description,
                "version": self.version,
            },
            "paths": paths,
        }

    def add_middleware(self, middleware_class: Type[Any], **options: Any) -> None:
        self.middleware_stack.append((middleware_class, options))
        self._compiled_app = None

    @property
    def user_middleware(self) -> List[Any]:
        return [
            types.SimpleNamespace(cls=m_cls, kwargs=opts)
            for m_cls, opts in self.middleware_stack
        ]

    def add_exception_handler(
        self, exc_class_or_status_code: Union[Type[Exception], int], handler: Callable
    ) -> None:
        self.exception_handlers[exc_class_or_status_code] = handler

    def exception_handler(self, exc_class_or_status_code: Union[Type[Exception], int]) -> Callable:
        def decorator(handler: Callable) -> Callable:
            self.add_exception_handler(exc_class_or_status_code, handler)
            return handler

        return decorator

    def _find_handler(self, exc: Exception) -> Optional[Callable]:
        if isinstance(exc, HTTPException):
            handler = self.exception_handlers.get(exc.status_code)
            if handler is not None:
                return handler

        for cls in type(exc).__mro__:
            handler = self.exception_handlers.get(cls)
            if handler is not None:
                return handler
            if cls is ValidationError:
                handler = self.exception_handlers.get(RequestValidationError)
                if handler is not None:
                    return handler

        return None

    def include_router(
        self,
        router: APIRouter,
        prefix: str = "",
        tags: Optional[List[str]] = None,
        dependencies: Optional[Sequence[Any]] = None,
    ) -> None:
        self.router.include_router(router, prefix=prefix, tags=tags, dependencies=dependencies)
        self._routes_sorted = False

    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        self.router.add_api_route(path, endpoint, **kwargs)
        self._routes_sorted = False

    def get(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self.router.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self.router.post(path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self.router.put(path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self.router.patch(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self.router.delete(path, **kwargs)

    def options(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self.router.options(path, **kwargs)

    def head(self, path: str, **kwargs: Any) -> Callable[[Any], Any]:
        return self.router.head(path, **kwargs)

    def _sort_routes_if_needed(self) -> None:
        if not self._routes_sorted:
            self.router.routes.sort(key=lambda r: r.score)
            self._routes_sorted = True

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[Dict[str, Any]], Any],
    ) -> None:
        # Build middleware stack if not already constructed
        if self._compiled_app is None:
            app_handler: Callable = self._dispatch_request
            for middleware_cls, opts in self.middleware_stack:
                app_handler = middleware_cls(app=app_handler, **opts)
            self._compiled_app = app_handler

        scope_type = scope.get("type", "")
        if scope_type == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return

        if scope_type == "http":
            await self._compiled_app(scope, receive, send)
            return

        raise NotImplementedError(f"Scope type '{scope_type}' not supported")

    async def _handle_lifespan(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[Dict[str, Any]], Any],
    ) -> None:
        while True:
            message = await receive()
            m_type = message.get("type")
            if m_type == "lifespan.startup":
                try:
                    if self.lifespan:
                        self._lifespan_ctx = self.lifespan(self)
                        if hasattr(self._lifespan_ctx, "__aenter__"):
                            await self._lifespan_ctx.__aenter__()
                        elif hasattr(self._lifespan_ctx, "__enter__"):
                            self._lifespan_ctx.__enter__()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as exc:
                    logger.error("Lifespan startup failed: %s", exc, exc_info=True)
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
            elif m_type == "lifespan.shutdown":
                try:
                    if hasattr(self, "_lifespan_ctx"):
                        if hasattr(self._lifespan_ctx, "__aexit__"):
                            await self._lifespan_ctx.__aexit__(None, None, None)
                        elif hasattr(self._lifespan_ctx, "__exit__"):
                            self._lifespan_ctx.__exit__(None, None, None)
                    await send({"type": "lifespan.shutdown.complete"})
                except Exception as exc:
                    logger.error("Lifespan shutdown failed: %s", exc, exc_info=True)
                    await send({"type": "lifespan.shutdown.failed", "message": str(exc)})
                return

    async def _dispatch_request(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[Dict[str, Any]], Any],
    ) -> None:
        self._sort_routes_if_needed()
        request = Request(scope, receive, send)
        path = request.url.path
        method = request.method

        matched_route: Optional[Route] = None
        matched_params: Dict[str, str] = {}
        allowed_methods_for_path: Set[str] = set()

        for route in self.router.routes:
            params = route.matches(path)
            if params is not None:
                allowed_methods_for_path.update(route.methods)
                if method in route.methods or (method == "HEAD" and "GET" in route.methods):
                    matched_route = route
                    matched_params = params
                    break

        if matched_route is None:
            if allowed_methods_for_path:
                http_exc = HTTPException(status_code=405, detail="Method Not Allowed")
                handler = self._find_handler(http_exc)
                if handler is not None:
                    resp = handler(request, http_exc)
                    if inspect.isawaitable(resp):
                        resp = await resp
                    await resp(scope, receive, send)
                    return
                resp = JSONResponse({"detail": "Method Not Allowed"}, status_code=405)
                await resp(scope, receive, send)
                return
            http_exc = HTTPException(status_code=404, detail="Not Found")
            handler = self._find_handler(http_exc)
            if handler is not None:
                resp = handler(request, http_exc)
                if inspect.isawaitable(resp):
                    resp = await resp
                await resp(scope, receive, send)
                return
            resp = JSONResponse({"detail": "Not Found"}, status_code=404)
            await resp(scope, receive, send)
            return

        # Prepare request context
        scope["path_params"] = matched_params
        request.path_params = matched_params
        injected_response = Response()
        dep_cache: Dict[Any, Any] = {}
        bg_tasks = BackgroundTasks()
        final_response: Optional[Response] = None

        response_started = False
        original_send = send

        async def tracked_send(message: Dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await original_send(message)

        send = tracked_send

        try:
            async with contextlib.AsyncExitStack() as exit_stack:
                # 0. Execute route-level and router-level dependencies
                for dep in matched_route.dependencies:
                    await _resolve_dependency(
                        dep,
                        request,
                        injected_response,
                        dep_cache,
                        exit_stack,
                        self.dependency_overrides,
                        matched_params,
                        bg_tasks=bg_tasks,
                    )

                # 1. Resolve handler arguments & execute
                kwargs = await _resolve_endpoint_kwargs(
                    matched_route.endpoint,
                    request,
                    injected_response,
                    dep_cache,
                    exit_stack,
                    self.dependency_overrides,
                    matched_params,
                    bg_tasks=bg_tasks,
                )

                if inspect.iscoroutinefunction(matched_route.endpoint):
                    result = await matched_route.endpoint(**kwargs)
                else:
                    result = matched_route.endpoint(**kwargs)
                    if inspect.isawaitable(result):
                        result = await result

                # 2. Convert result to HTTP Response
                if isinstance(result, Response):
                    final_response = result
                elif result is None and matched_route.status_code == 204:
                    final_response = Response(status_code=204)
                else:
                    if matched_route.response_model is not None:
                        try:
                            result = TypeAdapter(matched_route.response_model).validate_python(result)
                        except ValidationError as resp_val_err:
                            logger.error(
                                "Response validation failed for %s %s: %s",
                                method,
                                path,
                                resp_val_err,
                            )
                            final_response = JSONResponse(
                                content={"detail": "Internal Server Error"},
                                status_code=500,
                            )
                            result = None
                    if final_response is None:
                        eff_code = (
                            injected_response.status_code
                            if injected_response.status_code != 200
                            else matched_route.status_code
                        )
                        final_response = JSONResponse(content=result, status_code=eff_code)

                # Merge injected headers & Set-Cookie headers
                for k, v in injected_response.headers.items():
                    if k.lower() == "set-cookie":
                        final_response.headers.append(k, v)
                    elif k.lower() not in final_response.headers:
                        final_response.headers[k] = v

                # If final_response is a StreamingResponse, send it inside the exit stack
                if isinstance(final_response, StreamingResponse):
                    await final_response(scope, receive, send)

            # Exit stack closed here (deps cleaned up, get_db committed only on success)
            if final_response is not None and not isinstance(final_response, StreamingResponse):
                await final_response(scope, receive, send)

            # Execute background tasks after response return
            if bg_tasks and bg_tasks.tasks:
                try:
                    await bg_tasks()
                except Exception as bg_exc:
                    logger.error("Background task execution error: %s", bg_exc, exc_info=True)

        except HTTPException as http_exc:
            if response_started:
                logger.exception("HTTPException raised after response already started on %s %s: %s", method, path, http_exc)
                return

            handler = self._find_handler(http_exc)
            if handler is not None:
                custom_resp = handler(request, http_exc)
                if inspect.isawaitable(custom_resp):
                    custom_resp = await custom_resp
                await custom_resp(scope, receive, send)
                return

            err_resp = JSONResponse(
                content={"detail": http_exc.detail},
                status_code=http_exc.status_code,
                headers=http_exc.headers,
            )
            await err_resp(scope, receive, send)

        except ValidationError as v_exc:
            if response_started:
                logger.exception("ValidationError raised after response already started on %s %s: %s", method, path, v_exc)
                return

            handler = self._find_handler(v_exc)
            if handler is not None:
                custom_resp = handler(request, v_exc)
                if inspect.isawaitable(custom_resp):
                    custom_resp = await custom_resp
                await custom_resp(scope, receive, send)
                return

            val_resp = JSONResponse(
                content={"detail": v_exc.errors()},
                status_code=422,
            )
            await val_resp(scope, receive, send)

        except Exception as exc:
            if response_started:
                logger.exception("Exception raised after response already started on %s %s: %s", method, path, exc)
                return

            handler = self._find_handler(exc)
            if handler is not None:
                res = handler(request, exc)
                if inspect.isawaitable(res):
                    res = await res
                await res(scope, receive, send)
                return

            logger.error("Unhandled API exception on %s %s: %s", method, path, exc, exc_info=True)
            err_resp = JSONResponse(
                content={"detail": "Internal Server Error"},
                status_code=500,
            )
            await err_resp(scope, receive, send)


FastAPI = EphemresApp


# ---------------------------------------------------------------------------
# 9. Security Shims
# ---------------------------------------------------------------------------
class HTTPAuthorizationCredentials:
    def __init__(self, scheme: str, credentials: str) -> None:
        self.scheme = scheme
        self.credentials = credentials


class HTTPBearer:
    def __init__(self, auto_error: bool = True) -> None:
        self.auto_error = auto_error

    async def __call__(self, request: Request) -> Optional[HTTPAuthorizationCredentials]:
        auth = request.headers.get("authorization")
        if not auth or not auth.startswith("Bearer "):
            if self.auto_error:
                raise HTTPException(status_code=401, detail="Not authenticated")
            return None
        token = auth.split(" ", 1)[1]
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class OAuth2PasswordBearer:
    def __init__(self, tokenUrl: str, auto_error: bool = True) -> None:
        self.tokenUrl = tokenUrl
        self.auto_error = auto_error

    async def __call__(self, request: Request) -> Optional[str]:
        auth = request.headers.get("authorization")
        if not auth or not auth.startswith("Bearer "):
            if self.auto_error:
                raise HTTPException(status_code=401, detail="Not authenticated")
            return None
        return auth.split(" ", 1)[1]


# ---------------------------------------------------------------------------
# 10. Loop-Safe In-Process TestClient
# ---------------------------------------------------------------------------
class TestResponse:
    """Synchronous test response object mirroring requests / httpx."""

    __test__ = False

    def __init__(
        self,
        status_code: int,
        headers: List[Tuple[bytes, bytes]],
        body_chunks: List[bytes],
    ) -> None:
        self.status_code = status_code
        self.headers = MutableHeaders(raw=headers)
        self.content = b"".join(body_chunks)
        self._body_chunks = list(body_chunks)

        # Parse cookies returned
        self.cookies: Dict[str, str] = {}
        for cookie_str in self.headers.getlist("set-cookie"):
            try:
                c = http.cookies.SimpleCookie(cookie_str)
                for k, morsel in c.items():
                    self.cookies[k] = morsel.value
            except Exception:
                pass

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def iter_lines(self) -> Iterator[str]:
        for line in self.text.splitlines(keepends=True):
            yield line

    def iter_bytes(self) -> Iterator[bytes]:
        yield from self._body_chunks

    def iter_text(self) -> Iterator[str]:
        for chunk in self._body_chunks:
            yield chunk.decode("utf-8", errors="replace")


class TestClient:
    """Thread-safe, loop-safe in-process ASGI test client."""

    __test__ = False

    def __init__(
        self,
        app: Any,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
        client: Tuple[str, int] = ("127.0.0.1", 50000),
    ) -> None:
        self.app = app
        self.base_url = base_url.rstrip("/")
        self.raise_server_exceptions = raise_server_exceptions
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
        self.client_addr = client
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._lifespan_ctx: Optional[Any] = None

    def __enter__(self) -> TestClient:
        # Run lifespan startup if configured on the application
        if getattr(self.app, "lifespan", None):
            try:
                self._lifespan_ctx = self.app.lifespan(self.app)
                if hasattr(self._lifespan_ctx, "__aenter__"):
                    self._run_async(self._lifespan_ctx.__aenter__())
                elif hasattr(self._lifespan_ctx, "__enter__"):
                    self._lifespan_ctx.__enter__()
            except Exception as exc:
                logger.error("TestClient lifespan startup failed: %s", exc)
                raise
        return self

    def __exit__(self, *args: Any) -> None:
        # Run lifespan shutdown
        try:
            if self._lifespan_ctx is not None:
                if hasattr(self._lifespan_ctx, "__aexit__"):
                    self._run_async(self._lifespan_ctx.__aexit__(None, None, None))
                elif hasattr(self._lifespan_ctx, "__exit__"):
                    self._lifespan_ctx.__exit__(None, None, None)
        finally:
            self._executor.shutdown(wait=False)

    def _run_async(self, coro: Any) -> Any:
        """Executes an async coroutine safely, even when called from inside a running loop."""
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False

        if not in_loop:
            return asyncio.run(coro)

        # Running inside existing loop (e.g. async test) -> execute on background thread
        future = self._executor.submit(asyncio.run, coro)
        return future.result()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Any] = None,
        json: Optional[Any] = None,
        content: Optional[Union[bytes, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        cookies: Optional[Mapping[str, str]] = None,
        follow_redirects: bool = True,
        **kwargs: Any,
    ) -> TestResponse:
        return self._run_async(
            self._async_request(
                method,
                url,
                params=params,
                data=data,
                json=json,
                content=content,
                headers=headers,
                cookies=cookies,
                follow_redirects=follow_redirects,
            )
        )

    async def _async_request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Any] = None,
        json: Optional[Any] = None,
        content: Optional[Union[bytes, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        cookies: Optional[Mapping[str, str]] = None,
        follow_redirects: bool = True,
    ) -> TestResponse:
        # URL normalization
        full_url = url if "://" in url else f"{self.base_url}{url if url.startswith('/') else '/' + url}"
        parsed = urllib.parse.urlparse(full_url)
        path = parsed.path or "/"

        # Query string
        q_dict: Dict[str, Any] = {}
        if parsed.query:
            q_dict.update(urllib.parse.parse_qs(parsed.query))
        if params:
            for k, v in params.items():
                q_dict[k] = v
        query_string = urllib.parse.urlencode(q_dict, doseq=True).encode("ascii")

        # Body serialization
        body_bytes = b""
        req_headers = MutableHeaders(headers=self.headers)
        if headers:
            for k, v in headers.items():
                req_headers[k] = str(v)

        if json is not None:
            body_bytes = _json_serialize_bytes(json)
            req_headers["content-type"] = "application/json"
        elif data is not None:
            if isinstance(data, dict):
                body_bytes = urllib.parse.urlencode(data).encode("utf-8")
                req_headers["content-type"] = "application/x-www-form-urlencoded"
            elif isinstance(data, (bytes, str)):
                body_bytes = data.encode("utf-8") if isinstance(data, str) else data
        elif content is not None:
            body_bytes = content.encode("utf-8") if isinstance(content, str) else content

        if body_bytes and "content-length" not in req_headers:
            req_headers["content-length"] = str(len(body_bytes))

        # Cookies
        merged_cookies = dict(self.cookies)
        if cookies:
            merged_cookies.update(cookies)
        if merged_cookies:
            cookie_hdr = "; ".join(f"{k}={v}" for k, v in merged_cookies.items())
            req_headers["cookie"] = cookie_hdr

        # Build ASGI scope
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": parsed.scheme or "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": req_headers.raw,
            "client": self.client_addr,
            "server": ("testserver", 80),
        }

        # Receive mock
        body_sent = False
        async def mock_receive() -> Dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body_bytes, "more_body": False}
            return {"type": "http.disconnect"}

        # Send mock
        status_code = 200
        resp_headers: List[Tuple[bytes, bytes]] = []
        body_chunks: List[bytes] = []

        async def mock_send(message: Dict[str, Any]) -> None:
            nonlocal status_code, resp_headers, body_chunks
            m_type = message.get("type", "")
            if m_type == "http.response.start":
                status_code = message.get("status", 200)
                resp_headers = list(message.get("headers", []))
            elif m_type == "http.response.body":
                b = message.get("body", b"")
                if b:
                    body_chunks.append(b)

        await self.app(scope, mock_receive, mock_send)

        response = TestResponse(status_code, resp_headers, body_chunks)

        # Update persistent cookie jar
        for k, v in response.cookies.items():
            self.cookies[k] = v

        return response

    def get(self, url: str, **kwargs: Any) -> TestResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> TestResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> TestResponse:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> TestResponse:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> TestResponse:
        return self.request("DELETE", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> TestResponse:
        return self.request("OPTIONS", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> TestResponse:
        return self.request("HEAD", url, **kwargs)

    @contextlib.contextmanager
    def stream(self, method: str, url: str, **kwargs: Any) -> Iterator[TestResponse]:
        yield self.request(method, url, **kwargs)


def _json_serialize_bytes(obj: Any) -> bytes:
    serialized = _json_serialize(obj)
    return json.dumps(serialized, default=str).encode("utf-8")


__all__ = [
    "EphemresApp",
    "FastAPI",
    "APIRouter",
    "Request",
    "Response",
    "JSONResponse",
    "HTMLResponse",
    "PlainTextResponse",
    "StreamingResponse",
    "RedirectResponse",
    "HTTPException",
    "Depends",
    "Query",
    "Path",
    "Body",
    "Header",
    "Cookie",
    "BackgroundTasks",
    "CORSMiddleware",
    "BaseHTTPMiddleware",
    "status",
    "TestClient",
    "HTTPAuthorizationCredentials",
    "HTTPBearer",
]
