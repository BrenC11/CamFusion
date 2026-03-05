from __future__ import annotations

import logging

import aiohttp
from ring_doorbell import Auth, Ring

from app.util.ring_auth import RingAuthStore, ring_user_agent

from .base import BaseCameraSource


class RingCameraSource(BaseCameraSource):
    """Ring camera source backed by ring-doorbell auth and snapshots."""

    def __init__(self, name: str, config: dict, logger: logging.Logger, auth_store: RingAuthStore) -> None:
        super().__init__(name=name, config=config, logger=logger)
        self.auth_store = auth_store

        self.account = str(config.get("account", "default") or "default").strip()
        self.device_id = config.get("device_id")
        self.device_name = str(config.get("device_name", "") or "").strip().lower()
        self.snapshot_retries = max(1, int(config.get("snapshot_retries", 3) or 3))
        self.snapshot_delay = max(1, int(config.get("snapshot_delay", 1) or 1))

        self._session: aiohttp.ClientSession | None = None
        self._ring: Ring | None = None
        self._device = None

    async def start(self) -> None:
        # Best effort warmup; login may happen later via /ring/login.
        try:
            await self._ensure_device()
        except Exception as err:
            self.logger.info("%s not ready yet: %s", self.name, err)

    async def stop(self) -> None:
        await self._reset_client()

    async def snapshot_frame(self) -> bytes:
        last_err: Exception | None = None

        for attempt in range(2):
            try:
                await self._ensure_device()
                if self._device is None:
                    raise RuntimeError(f"{self.name}: ring device is not initialized")

                image = await self._device.async_get_snapshot(
                    retries=self.snapshot_retries,
                    delay=self.snapshot_delay,
                )
                if not image:
                    raise RuntimeError(f"{self.name}: ring snapshot returned empty bytes")

                self.mark_ok()
                return image
            except Exception as err:  # pragma: no cover
                last_err = err
                await self._reset_client()
                if attempt == 0:
                    continue

        assert last_err is not None
        self.mark_failed(last_err)
        raise last_err

    async def _ensure_device(self) -> None:
        if self._device is not None and self._ring is not None and self._session is not None:
            return

        account_data = self.auth_store.get_account(self.account)
        if not account_data:
            raise RuntimeError(
                f"{self.name}: ring account '{self.account}' not logged in. "
                "Use POST /ring/login to authenticate first."
            )

        token = account_data.get("token")
        hardware_id = str(account_data.get("hardware_id", "")).strip()
        if not isinstance(token, dict) or not token:
            raise RuntimeError(f"{self.name}: ring account '{self.account}' has no stored token")
        if not hardware_id:
            raise RuntimeError(f"{self.name}: ring account '{self.account}' is missing hardware_id")

        self._session = aiohttp.ClientSession()
        auth = Auth(
            ring_user_agent(),
            token=token,
            token_updater=self.auth_store.token_updater(self.account),
            hardware_id=hardware_id,
            http_client_session=self._session,
        )
        self._ring = Ring(auth)

        await self._ring.async_update_data()
        devices = list(self._ring.video_devices())
        if not devices:
            raise RuntimeError(f"{self.name}: no Ring video devices found for account '{self.account}'")

        self._device = self._select_device(devices)
        if self._device is None:
            configured = self.device_name or str(self.device_id)
            raise RuntimeError(f"{self.name}: configured ring device '{configured}' was not found")

    def _select_device(self, devices: list) -> object | None:
        if self.device_id is not None:
            target = str(self.device_id)
            for device in devices:
                if target in {
                    str(getattr(device, "id", "")),
                    str(getattr(device, "device_api_id", "")),
                }:
                    return device

        if self.device_name:
            for device in devices:
                if str(getattr(device, "name", "")).strip().lower() == self.device_name:
                    return device

        return devices[0] if devices else None

    async def _reset_client(self) -> None:
        self._device = None
        self._ring = None
        if self._session is not None:
            await self._session.close()
            self._session = None
