"""Small JSON HTTP transport with normalized provider failures."""

from __future__ import annotations

import json
import socket
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import ProviderError, ProviderTimeout


class UrllibJsonTransport:
    def post(self, url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
        request = Request(url, data=json.dumps(body).encode(), headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return json.load(response)
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderTimeout() from exc
        except HTTPError as exc:
            raise ProviderError(f"provider returned HTTP {exc.code}", retryable=exc.code == 429 or exc.code >= 500) from exc
        except URLError as exc:
            raise ProviderError("provider transport failed", retryable=True) from exc
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("provider returned an invalid response") from exc
