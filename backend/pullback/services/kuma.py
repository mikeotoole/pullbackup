"""Uptime Kuma push-monitor management via Socket.IO.

Kuma has no REST mutation API — Socket.IO is the only path for create/edit/delete.
We open a short-lived connection per operation; this is not high-throughput.
"""
import asyncio
import logging
from typing import Optional
from croniter import croniter
from datetime import datetime, timezone
import socketio
from ..config import settings

log = logging.getLogger(__name__)


def cron_interval_seconds(cron: str, default: int = 3600) -> int:
    """Approximate the heartbeat interval from a cron expression by looking at the
    gap between the next two firings. Used to size the Kuma push monitor's interval.
    """
    try:
        it = croniter(cron, datetime.now(timezone.utc))
        a = it.get_next(datetime)
        b = it.get_next(datetime)
        delta = int((b - a).total_seconds())
        return max(delta, 60)
    except Exception:
        return default


class KumaClient:
    def __init__(self) -> None:
        self.url = settings.uptime_kuma_url
        self.user = settings.uptime_kuma_username
        self.password = settings.uptime_kuma_password

    async def _call(self, event: str, *args, timeout: float = 10.0):
        sio = socketio.AsyncClient(reconnection=False)
        await sio.connect(self.url, transports=["websocket"])
        try:
            login = await sio.call("login", {"username": self.user, "password": self.password, "token": ""}, timeout=timeout)
            if not login or not login.get("ok"):
                raise RuntimeError(f"kuma login failed: {login}")
            return await sio.call(event, *args, timeout=timeout)
        finally:
            await sio.disconnect()

    async def create_push_monitor(self, name: str, interval_s: int, tag: str = "pullback") -> tuple[int, str]:
        """Create a push monitor and return (monitor_id, push_token)."""
        payload = {
            "type": "push",
            "name": name,
            "interval": max(interval_s, 60),
            "maxretries": 0,
            "retryInterval": 60,
            "notificationIDList": {},
            "active": True,
        }
        res = await self._call("add", payload)
        if not res or not res.get("ok"):
            raise RuntimeError(f"kuma add failed: {res}")
        monitor_id = res["monitorID"]
        # fetch the monitor back to grab the pushToken
        info = await self._call("getMonitor", monitor_id)
        token = info.get("monitor", {}).get("pushToken", "")
        return monitor_id, token

    async def update_interval(self, monitor_id: int, interval_s: int) -> None:
        info = await self._call("getMonitor", monitor_id)
        mon = info.get("monitor") or {}
        mon["interval"] = max(interval_s, 60)
        await self._call("editMonitor", mon)

    async def set_active(self, monitor_id: int, active: bool) -> None:
        op = "resumeMonitor" if active else "pauseMonitor"
        await self._call(op, monitor_id)

    async def delete(self, monitor_id: int) -> None:
        await self._call("deleteMonitor", monitor_id)


def client() -> Optional[KumaClient]:
    return KumaClient() if settings.kuma_enabled else None
