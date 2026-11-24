"""A minimal Proxmox VE API client for the template builder (API token auth)."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import requests

TASK_POLL_SECONDS = 2


class PveApiError(RuntimeError):
    """The Proxmox API refused a request or a task it started failed."""


class PveApi:
    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        node: str,
        *,
        verify: bool | str = True,
        session: Any = None,
        sleep: Any = time.sleep,
    ):
        self.base = url.rstrip("/") + "/api2/json"
        self.node = node
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"
        self.session.verify = verify
        self.sleep = sleep

    # --- transport ---------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, self.base + path, timeout=60, **kwargs)
        if response.status_code >= 400:
            # PVE puts the reason in the status line and sometimes in "errors".
            detail = response.reason
            try:
                errors = response.json().get("errors")
                if errors:
                    detail = f"{detail}: {errors}"
            except ValueError:
                pass
            raise PveApiError(f"{method} {path}: HTTP {response.status_code} {detail}")
        return response.json().get("data")

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, **data: Any) -> Any:
        return self.request("POST", path, data=data)

    def put(self, path: str, **data: Any) -> Any:
        return self.request("PUT", path, data=data)

    def delete(self, path: str, **params: Any) -> Any:
        return self.request("DELETE", path, params=params)

    def wait_task(self, upid: str, timeout_seconds: int = 600) -> None:
        deadline = time.monotonic() + timeout_seconds
        path = f"/nodes/{self.node}/tasks/{quote(upid, safe='')}/status"
        while True:
            status = self.get(path)
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise PveApiError(f"task {upid} failed: {status.get('exitstatus')}")
                return
            if time.monotonic() >= deadline:
                raise PveApiError(f"task {upid} did not finish within {timeout_seconds}s")
            self.sleep(TASK_POLL_SECONDS)

    # --- VMs ---------------------------------------------------------------

    def vm_path(self, vmid: int) -> str:
        return f"/nodes/{self.node}/qemu/{vmid}"

    def pool_vms(self, pool: str) -> list[dict[str, Any]]:
        members = self.get(f"/pools/{quote(pool, safe='')}").get("members", [])
        return [m for m in members if m.get("type") == "qemu"]

    def is_free_vmid(self, vmid: int) -> bool:
        try:
            self.get("/cluster/nextid", vmid=vmid)
        except PveApiError:
            return False
        return True

    def create_vm(self, **config: Any) -> None:
        self.wait_task(self.post(f"/nodes/{self.node}/qemu", **config))

    def update_vm(self, vmid: int, **config: Any) -> None:
        result = self.post(f"{self.vm_path(vmid)}/config", **config)
        if isinstance(result, str) and result.startswith("UPID:"):
            self.wait_task(result)

    def resize_disk(self, vmid: int, disk: str, size: str) -> None:
        result = self.put(f"{self.vm_path(vmid)}/resize", disk=disk, size=size)
        if isinstance(result, str) and result.startswith("UPID:"):
            self.wait_task(result)

    def vm_status(self, vmid: int) -> str:
        return self.get(f"{self.vm_path(vmid)}/status/current")["status"]

    def start_vm(self, vmid: int) -> None:
        self.wait_task(self.post(f"{self.vm_path(vmid)}/status/start"))

    def shutdown_vm(self, vmid: int, timeout_seconds: int = 120) -> None:
        self.wait_task(
            self.post(
                f"{self.vm_path(vmid)}/status/shutdown", timeout=timeout_seconds, forceStop=1
            ),
            timeout_seconds + 60,
        )

    def stop_vm(self, vmid: int) -> None:
        self.wait_task(self.post(f"{self.vm_path(vmid)}/status/stop"))

    def destroy_vm(self, vmid: int) -> None:
        self.wait_task(
            self.delete(self.vm_path(vmid), purge=1, **{"destroy-unreferenced-disks": 1})
        )

    def convert_to_template(self, vmid: int) -> None:
        result = self.post(f"{self.vm_path(vmid)}/template")
        if isinstance(result, str) and result.startswith("UPID:"):
            self.wait_task(result)

    def linked_clone(self, template: int, newid: int, name: str, pool: str) -> None:
        self.wait_task(
            self.post(f"{self.vm_path(template)}/clone", newid=newid, name=name, pool=pool, full=0)
        )

    def agent_read_file(self, vmid: int, path: str) -> str | None:
        """Read a guest file through the QEMU agent; None while the agent is not answering."""
        try:
            data = self.get(f"{self.vm_path(vmid)}/agent/file-read", file=path)
        except PveApiError:
            return None
        return data.get("content", "") if isinstance(data, dict) else None

    # --- storage -------------------------------------------------------------

    def upload_iso(self, storage: str, filename: str, content: bytes) -> str:
        upid = self.request(
            "POST",
            f"/nodes/{self.node}/storage/{storage}/upload",
            data={"content": "iso"},
            files={"filename": (filename, content, "application/octet-stream")},
        )
        self.wait_task(upid)
        return f"{storage}:iso/{filename}"

    def delete_volume(self, volid: str) -> None:
        storage = volid.split(":", 1)[0]
        result = self.delete(
            f"/nodes/{self.node}/storage/{storage}/content/{quote(volid, safe='')}"
        )
        if isinstance(result, str) and result.startswith("UPID:"):
            self.wait_task(result)
