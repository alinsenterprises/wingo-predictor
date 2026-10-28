"""Signed client for the dmfirst1.com web API.

Every call is a JSON POST that carries:
    language = 0 (en), random = uuid4 hex, timestamp = unix seconds
    signature = MD5(canonical_json).hexdigest().upper()[:32]

where canonical_json is the payload (minus signature/track/xosoBettingData/
timestamp, minus null and empty-string values) with its keys sorted - which is
exactly what the site's own bundle does in its axios interceptor.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

import requests

from .config import Config
from .errors import ApiError

log = logging.getLogger("wingo.api")

_EXCLUDED = ("signature", "track", "xosoBettingData", "timestamp")


class DmFirstClient:
    def __init__(self, cfg: Config, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json;charset=UTF-8",
            "Ar-Origin": cfg.origin,
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        })
        self._logged_in = False

    # ------------------------------------------------------------------ #
    @staticmethod
    def sign(payload: dict) -> dict:
        base = dict(payload)
        base["language"] = 0
        base["random"] = uuid.uuid4().hex
        canonical = {
            key: value
            for key, value in base.items()
            if key not in _EXCLUDED and value is not None and value != ""
        }
        ordered = {key: canonical[key] for key in sorted(canonical)}
        digest = hashlib.md5(
            json.dumps(ordered, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest().upper()[:32]
        base["signature"] = digest
        base["timestamp"] = int(time.time())
        return base

    # ------------------------------------------------------------------ #
    def post(self, path: str, payload: dict, *, retries: int = 4) -> dict:
        body = self.sign(payload)
        url = path if path.startswith("http") else self.cfg.base_url + path.lstrip("/")
        last_error: Exception | None = None

        for attempt in range(retries):
            if attempt:
                time.sleep(min(15, 2 ** (attempt - 1)))
            try:
                response = self.session.post(url, data=json.dumps(body), timeout=20)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("network error on %s (try %s): %s", path, attempt + 1, exc)
                continue

            if response.status_code >= 500:
                last_error = ApiError(f"HTTP {response.status_code} from {path}")
                continue

            try:
                data = response.json()
            except ValueError:
                last_error = ApiError(f"non-JSON response from {path}", retryable=False)
                continue

            if isinstance(data, dict) and "data" in data and data.get("data") is not None:
                return data

            message = (data or {}).get("msg") or f"unexpected payload from {path}"
            retryable = (data or {}).get("code") not in (3, 4, 5)
            error = ApiError(f"{path}: {message}", retryable=retryable, payload=data)
            if not retryable:
                raise error
            last_error = error

        raise last_error if isinstance(last_error, ApiError) else ApiError(
            f"{path} failed after {retries} tries: {last_error}"
        )

    # ------------------------------------------------------------------ #
    def game_issue(self, type_id: int) -> dict:
        """Current live issue: number, start/end time, server clock."""
        data = self.post("GetGameIssue", {"typeId": type_id})
        return data.get("data") or {}

    def history(self, type_id: int, page: int = 1, size: int | None = None) -> list[dict]:
        size = size or self.cfg.results_page_size
        data = self.post(
            "GetNoaverageEmerdList",
            {"typeId": type_id, "pageNo": page, "pageSize": size},
        )
        listing = (data.get("data") or {}).get("list") or []
        return [row for row in listing if isinstance(row, dict) and row.get("issueNumber")]

    def login(self) -> bool:
        """Best effort: history is public, so a failed login must never block."""
        if self._logged_in:
            return True
        if not self.cfg.do_login or not self.cfg.username or not self.cfg.password:
            self._logged_in = True
            return True
        payload = {
            "username": self.cfg.username,
            "pwd": self.cfg.password,
            "logintype": "password",
            "phonetype": 2,
            "deviceId": "web",
            "language": 0,
        }
        try:
            data = self.post("Login", payload, retries=1)
        except ApiError as exc:
            log.warning("login failed, continuing with public history: %s", exc)
            return False
        token = ((data.get("data") or {}).get("token")) or ""
        if token:
            self.session.headers["Authorization"] = "Bearer " + token
        self._logged_in = True
        return True

    def ping(self) -> tuple[bool, str]:
        """Cheap reachability probe used by /status and the CLI `check`."""
        try:
            self.game_issue(1)
            return True, "ok"
        except ApiError as exc:
            return False, str(exc)
