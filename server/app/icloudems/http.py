"""HTTP backend selection + uniform session wrapper.

curl_cffi impersonates a real browser's TLS fingerprint and is required
to get past some Cloudflare/WAF checks. If it isn't installed we fall
back to plain requests -- the app still works but may receive 403s on
some endpoints.
"""
import json
import os
import sys

from ..config import PROXY_URL

_HTTP_BACKEND = None

try:
    from curl_cffi import requests as cffi_requests
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    cffi_requests = None

try:
    import requests as _plain_requests
except ImportError:
    _plain_requests = None

if _HTTP_BACKEND is None and _plain_requests is None:
    print("Install either curl_cffi or requests: pip install curl_cffi",
          file=sys.stderr)
    sys.exit(1)


class HTTPError(Exception):
    def __init__(self, status, reason, method, url, body):
        self.status = status
        self.reason = reason
        self.method = method
        self.url = url
        self.body = body
        super().__init__(
            f"{status} {reason} from {method} {url}\n"
            f"Response body: {body[:800] if body else '(empty)'}"
        )


class HttpResponse:
    def __init__(self, status, reason, text, raw, method=""):
        self.status_code = status
        self.reason = reason
        self.text = text
        self._raw = raw
        self.method = method

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if not self.ok:
            raise HTTPError(
                self.status_code, self.reason, self.method,
                getattr(self._raw, "url", "?"), self.text
            )


class HttpSession:
    """Wraps either curl_cffi.Session or requests.Session uniformly."""

    def __init__(self, proxy_url=None):
        proxy = proxy_url or PROXY_URL or None
        if _HTTP_BACKEND == "curl_cffi":
            kwargs = {"impersonate": "chrome"}
            if proxy:
                kwargs["proxy"] = proxy
            self._s = cffi_requests.Session(**kwargs)
        else:
            self._s = _plain_requests.Session()
            if proxy:
                self._s.proxies = {"http": proxy, "https": proxy}
        self._headers = {}
        if proxy:
            import logging
            logging.getLogger("icloudems").info(f"HttpSession using proxy: {proxy}")

    def update_headers(self, headers):
        self._headers.update(headers)
        try:
            self._s.headers.update(headers)
        except Exception:
            pass

    def _wrap(self, raw, method=""):
        text = ""
        try:
            text = raw.text
        except Exception:
            try:
                text = raw.content.decode("utf-8", "replace")
            except Exception:
                text = ""
        reason = getattr(raw, "reason", "") or ""
        return HttpResponse(raw.status_code, reason, text, raw, method=method)

    def post(self, url, json=None, data=None, files=None, headers=None, timeout=30):
        merged = dict(self._headers)
        if headers:
            merged.update(headers)
        try:
            raw = self._s.post(url, json=json, data=data, files=files,
                               headers=merged, timeout=timeout)
        except Exception as e:
            raise HTTPError(0, type(e).__name__, "POST", url, str(e))
        return self._wrap(raw, method="POST")

    def get(self, url, headers=None, timeout=15):
        merged = dict(self._headers)
        if headers:
            merged.update(headers)
        try:
            raw = self._s.get(url, headers=merged, timeout=timeout)
        except Exception as e:
            raise HTTPError(0, type(e).__name__, "GET", url, str(e))
        return self._wrap(raw, method="GET")

    def cookies_dict(self):
        try:
            return dict(self._s.cookies)
        except Exception:
            return {}


class AsyncHttpSession:
    """Async wrapper using curl_cffi.AsyncSession for non-blocking I/O."""

    def __init__(self, proxy_url=None):
        proxy = proxy_url or PROXY_URL or None
        if _HTTP_BACKEND == "curl_cffi":
            kwargs = {"impersonate": "chrome"}
            if proxy:
                kwargs["proxy"] = proxy
            self._s = cffi_requests.AsyncSession(**kwargs)
        else:
            self._s = None
        self._headers = {}

    def update_headers(self, headers):
        self._headers.update(headers)

    def _wrap(self, raw, method=""):
        text = ""
        try:
            text = raw.text
        except Exception:
            try:
                text = raw.content.decode("utf-8", "replace")
            except Exception:
                text = ""
        reason = getattr(raw, "reason", "") or ""
        return HttpResponse(raw.status_code, reason, text, raw, method=method)

    async def post(self, url, json=None, data=None, files=None, headers=None, timeout=30):
        if self._s is None:
            raise HTTPError(0, "NoAsyncBackend", "POST", url,
                            "curl_cffi not installed")
        merged = dict(self._headers)
        if headers:
            merged.update(headers)
        try:
            raw = await self._s.post(url, json=json, data=data, files=files,
                                     headers=merged, timeout=timeout)
        except Exception as e:
            raise HTTPError(0, type(e).__name__, "POST", url, str(e))
        return self._wrap(raw, method="POST")

    async def get(self, url, headers=None, timeout=15):
        if self._s is None:
            raise HTTPError(0, "NoAsyncBackend", "GET", url,
                            "curl_cffi not installed")
        merged = dict(self._headers)
        if headers:
            merged.update(headers)
        try:
            raw = await self._s.get(url, headers=merged, timeout=timeout)
        except Exception as e:
            raise HTTPError(0, type(e).__name__, "GET", url, str(e))
        return self._wrap(raw, method="GET")

    def cookies_dict(self):
        if self._s is None:
            return {}
        try:
            return dict(self._s.cookies)
        except Exception:
            return {}

    async def close(self):
        if self._s is not None:
            try:
                await self._s.close()
            except Exception:
                pass
