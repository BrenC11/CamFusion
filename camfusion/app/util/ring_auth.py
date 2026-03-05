from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

import aiohttp
from ring_doorbell import Auth, AuthenticationError, Requires2FAError, Ring


def ring_user_agent() -> str:
    """Build a stable user-agent shown in Ring authorized devices."""
    return "CamFusion/ring-source"


class RingAuthError(RuntimeError):
    """Raised when Ring account actions fail."""


class RingRequires2FA(RingAuthError):
    """Raised when Ring requires an OTP code for login."""


class RingAuthStore:
    """Persist Ring account tokens and provide login/device helpers."""

    def __init__(self, *, path: str = "/data/ring_accounts.json", logger: logging.Logger | None = None) -> None:
        self.path = Path(path)
        self.logger = logger or logging.getLogger(__name__)
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"accounts": {}}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return {"accounts": {}}
            accounts = raw.get("accounts")
            if not isinstance(accounts, dict):
                return {"accounts": {}}
            return {"accounts": accounts}
        except Exception:
            return {"accounts": {}}

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def get_account(self, account: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._load_unlocked()
            account_data = data.get("accounts", {}).get(account)
            if isinstance(account_data, dict):
                return account_data
            return None

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._load_unlocked()
            accounts = data.get("accounts", {})
            results: list[dict[str, Any]] = []
            for account_name, account_data in accounts.items():
                if not isinstance(account_data, dict):
                    continue
                results.append(
                    {
                        "account": account_name,
                        "username": account_data.get("username", ""),
                        "hardware_id": account_data.get("hardware_id", ""),
                        "updated_at": account_data.get("updated_at", 0),
                    }
                )
            return sorted(results, key=lambda item: item["account"])

    def token_updater(self, account: str):
        def _update(token: dict[str, Any]) -> None:
            with self._lock:
                data = self._load_unlocked()
                accounts = data.setdefault("accounts", {})
                existing = accounts.get(account, {}) if isinstance(accounts.get(account), dict) else {}
                existing["token"] = token
                existing["updated_at"] = int(time.time())
                accounts[account] = existing
                self._save_unlocked(data)

        return _update

    async def login(
        self,
        *,
        account: str,
        username: str,
        password: str,
        twofa_code: str | None = None,
    ) -> dict[str, Any]:
        account = account.strip() or "default"
        username = username.strip()
        if not username:
            raise RingAuthError("username is required")
        if not password:
            raise RingAuthError("password is required")

        existing = self.get_account(account)
        hardware_id = str(existing.get("hardware_id")) if isinstance(existing, dict) and existing.get("hardware_id") else str(uuid.uuid4())

        session = aiohttp.ClientSession()
        auth = Auth(
            ring_user_agent(),
            hardware_id=hardware_id,
            http_client_session=session,
        )

        try:
            token = await auth.async_fetch_token(username, password, twofa_code)
        except Requires2FAError as err:
            raise RingRequires2FA("ring account requires a 2FA code") from err
        except AuthenticationError as err:
            raise RingAuthError("ring authentication failed") from err
        except Exception as err:
            raise RingAuthError(f"ring login failed: {err}") from err
        finally:
            await session.close()

        with self._lock:
            data = self._load_unlocked()
            accounts = data.setdefault("accounts", {})
            accounts[account] = {
                "username": username,
                "hardware_id": hardware_id,
                "token": token,
                "updated_at": int(time.time()),
            }
            self._save_unlocked(data)

        return {"account": account, "username": username, "hardware_id": hardware_id}

    async def list_devices(self, *, account: str) -> list[dict[str, Any]]:
        account = account.strip() or "default"
        account_data = self.get_account(account)
        if not account_data:
            raise RingAuthError(f"ring account '{account}' is not logged in")

        token = account_data.get("token")
        hardware_id = str(account_data.get("hardware_id", "")).strip()
        if not isinstance(token, dict) or not token:
            raise RingAuthError(f"ring account '{account}' does not have a token")
        if not hardware_id:
            raise RingAuthError(f"ring account '{account}' is missing a hardware_id")

        session = aiohttp.ClientSession()
        auth = Auth(
            ring_user_agent(),
            token=token,
            token_updater=self.token_updater(account),
            hardware_id=hardware_id,
            http_client_session=session,
        )
        ring = Ring(auth)

        try:
            await ring.async_update_data()
            devices = []
            for device in ring.video_devices():
                devices.append(
                    {
                        "id": getattr(device, "id", None),
                        "device_api_id": getattr(device, "device_api_id", None),
                        "name": getattr(device, "name", "unknown"),
                        "model": getattr(device, "model", "unknown"),
                    }
                )
            return devices
        except Exception as err:
            raise RingAuthError(f"failed to query ring devices: {err}") from err
        finally:
            await session.close()
