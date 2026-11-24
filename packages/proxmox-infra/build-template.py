#!/usr/bin/env python3
"""Build and verify the Proxmox sandbox template; configuration and flow in README.md."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

from image_scripts import (
    FAILED_MARKER,
    OK_MARKER,
    PROXMOX_FILES,
    SHARED_FILES,
    STATUS_PATH,
    build_script,
    verify_script,
)
from pve_api import PveApi, PveApiError
from seed_iso import build_seed_iso, meta_data

ROOT = Path(__file__).resolve().parents[2]
INFRA = Path(__file__).resolve().parent
EC2_INFRA = ROOT / "packages/ec2-infra"
sys.path.insert(0, str(ROOT / "packages/sandbox-images/src"))

from sandbox_images.bundle import pack_bundle  # noqa: E402
from sandbox_images.native import write_build_result  # noqa: E402

TEMPLATE_NAME_PREFIX = "openinspect-sandbox"
BUILD_HASH_NAME_CHARS = 16
BUILD_MEMORY_MIB = 4096
BUILD_CORES = 2
ROOT_DISK_SIZE = "48G"
VERIFY_BOOTSTRAP_URL = "https://openinspect.invalid/sessions/verify/sandbox-bootstrap"
VERIFY_BOOTSTRAP_TOKEN = "template-verification-token"

POLL_INTERVAL_SECONDS = 15
AGENT_TIMEOUT_SECONDS = 15 * 60
BUILD_TIMEOUT_SECONDS = 60 * 60
VERIFY_TIMEOUT_SECONDS = 20 * 60


class ImageBuildError(RuntimeError):
    """A template build or verification step did not succeed."""


def smbios_serial(url: str, token: str) -> str:
    """The SMBIOS serial a session clone carries; parsed by seed-bootstrap.py."""
    payload = json.dumps({"url": url, "token": token}, separators=(",", ":")).encode()
    return "oi1:" + base64.b64encode(payload).decode()


def smbios1_with_serial(current: str | None, serial: str) -> str:
    """Set the serial on an smbios1 option, keeping the clone's generated UUID."""
    uuid = re.search(r"(?:^|,)uuid=([0-9a-fA-F-]+)", current or "")
    parts = [f"uuid={uuid.group(1)}"] if uuid else []
    parts += [f"serial={base64.b64encode(serial.encode()).decode()}", "base64=1"]
    return ",".join(parts)


def bundle_archive(bundle_directory: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(bundle_directory, arcname=".")
        for name in SHARED_FILES:
            tar.add(EC2_INFRA / name, arcname=f"vm/{name}")
        for name in PROXMOX_FILES:
            tar.add(INFRA / name, arcname=f"vm/{name}")
    return buffer.getvalue()


class ProxmoxTemplateBuilder:
    def __init__(self, api: PveApi, settings: dict[str, Any], sleep: Any = time.sleep):
        self.api = api
        self.settings = settings
        self.sleep = sleep

    # --- lookup ----------------------------------------------------------------

    def find_template(self, name: str) -> dict[str, Any] | None:
        for vm in self.api.pool_vms(self.settings["pool"]):
            if vm.get("name") == name:
                return vm
        return None

    def free_vmid(self) -> int:
        for vmid in range(self.settings["vmid_min"], self.settings["vmid_max"] + 1):
            if self.api.is_free_vmid(vmid):
                return vmid
        raise ImageBuildError(
            f"no free VMID in {self.settings['vmid_min']}-{self.settings['vmid_max']}"
        )

    # --- steps -------------------------------------------------------------------

    def network(self) -> str:
        net = f"virtio,bridge={self.settings['bridge']}"
        if self.settings.get("vlan"):
            net += f",tag={self.settings['vlan']}"
        return net

    def build_template(self, name: str, bundle_directory: Path) -> int:
        iso = build_seed_iso(
            {
                "user-data": build_script().encode(),
                "meta-data": meta_data(f"{name}-build", name),
                "bundle.tar.gz": bundle_archive(bundle_directory),
            }
        )
        iso_volume = self.api.upload_iso(self.settings["image_storage"], f"{name}-build.iso", iso)
        vmid = self.free_vmid()
        print(f"{name}: build VM {vmid}", file=sys.stderr)
        try:
            self.api.create_vm(
                vmid=vmid,
                name=name,
                pool=self.settings["pool"],
                ostype="l26",
                memory=BUILD_MEMORY_MIB,
                cores=BUILD_CORES,
                cpu="host",
                scsihw="virtio-scsi-single",
                scsi0=(
                    f"{self.settings['sandbox_storage']}:0,"
                    f"import-from={self.settings['base_image']},discard=on,iothread=1,"
                    # Linked clones inherit it: sandboxes never go to vzdump jobs.
                    "backup=0"
                ),
                ide2=f"{iso_volume},media=cdrom",
                net0=self.network(),
                serial0="socket",
                vga="serial0",
                agent="enabled=1",
                boot="order=scsi0",
                description=f"Open-Inspect sandbox template ({name})",
            )
            self.api.resize_disk(vmid, "scsi0", ROOT_DISK_SIZE)
            self.run_step(vmid, BUILD_TIMEOUT_SECONDS)
            self.api.shutdown_vm(vmid)
            self.api.update_vm(vmid, delete="ide2")
            self.api.convert_to_template(vmid)
        except BaseException:
            self.discard(vmid)
            raise
        finally:
            self.api.delete_volume(iso_volume)
        return vmid

    def verify_template(self, template: int, name: str, runtime_env: dict[str, str]) -> None:
        expected = (
            f"OPENINSPECT_BOOTSTRAP_URL={VERIFY_BOOTSTRAP_URL}\n"
            f"OPENINSPECT_BOOTSTRAP_TOKEN={VERIFY_BOOTSTRAP_TOKEN}\n"
        )
        iso = build_seed_iso(
            {
                "user-data": verify_script(runtime_env, expected).encode(),
                "meta-data": meta_data(f"{name}-verify", f"{name}-verify"),
            }
        )
        iso_volume = self.api.upload_iso(self.settings["image_storage"], f"{name}-verify.iso", iso)
        vmid = self.free_vmid()
        print(f"{name}: verify clone {vmid} of {template}", file=sys.stderr)
        try:
            self.api.linked_clone(template, vmid, f"{name}-verify", self.settings["pool"])
            current = self.api.get(f"{self.api.vm_path(vmid)}/config").get("smbios1")
            self.api.update_vm(
                vmid,
                ide2=f"{iso_volume},media=cdrom",
                smbios1=smbios1_with_serial(
                    current, smbios_serial(VERIFY_BOOTSTRAP_URL, VERIFY_BOOTSTRAP_TOKEN)
                ),
            )
            self.run_step(vmid, VERIFY_TIMEOUT_SECONDS)
        finally:
            self.discard(vmid, keep_on_failure=False)
            self.api.delete_volume(iso_volume)

    def run_step(self, vmid: int, timeout_seconds: int) -> None:
        """Start the VM and wait for its script to report through the guest agent."""
        self.api.start_vm(vmid)
        started = time.monotonic()
        deadline = started + timeout_seconds
        agent_seen = False
        while time.monotonic() < deadline:
            self.sleep(POLL_INTERVAL_SECONDS)
            status = self.api.agent_read_file(vmid, STATUS_PATH)
            if status is not None:
                agent_seen = True
            elif not agent_seen and time.monotonic() - started > AGENT_TIMEOUT_SECONDS:
                raise ImageBuildError(
                    f"VM {vmid}: guest agent never answered; check its serial console"
                )
            if status and FAILED_MARKER in status:
                log = self.api.agent_read_file(vmid, "/var/log/openinspect-image.log") or ""
                raise ImageBuildError(f"VM {vmid}: {status.strip()}\n{log[-4000:]}")
            if status and OK_MARKER in status:
                return
            if self.api.vm_status(vmid) == "stopped":
                raise ImageBuildError(f"VM {vmid} stopped without reporting success")
        raise ImageBuildError(f"VM {vmid} did not finish within {timeout_seconds}s")

    def discard(self, vmid: int, keep_on_failure: bool | None = None) -> None:
        keep = self.settings["keep_failed"] if keep_on_failure is None else keep_on_failure
        if keep and sys.exc_info()[0] is not None:
            print(f"keeping VM {vmid} for inspection (PVE_KEEP_FAILED)", file=sys.stderr)
            return
        try:
            if self.api.vm_status(vmid) != "stopped":
                self.api.stop_vm(vmid)
            self.api.destroy_vm(vmid)
        except PveApiError as error:  # Never mask the build's own outcome.
            print(f"could not remove VM {vmid}: {error}", file=sys.stderr)


def read_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def read_settings() -> dict[str, Any]:
    return {
        "pool": read_required_env("PVE_POOL"),
        "sandbox_storage": read_required_env("PVE_SANDBOX_STORAGE"),
        "image_storage": read_required_env("PVE_IMAGE_STORAGE"),
        "base_image": read_required_env("PVE_BASE_IMAGE"),
        "bridge": read_required_env("PVE_SANDBOX_BRIDGE"),
        "vlan": os.environ.get("PVE_SANDBOX_VLAN", "").strip() or None,
        "vmid_min": int(os.environ.get("PVE_TEMPLATE_VMID_MIN", "9000")),
        "vmid_max": int(os.environ.get("PVE_TEMPLATE_VMID_MAX", "9099")),
        "keep_failed": os.environ.get("PVE_KEEP_FAILED", "").lower() in ("1", "true"),
    }


def main() -> None:
    api = PveApi(
        read_required_env("PVE_API_URL"),
        read_required_env("PVE_API_TOKEN_ID"),
        read_required_env("PVE_API_TOKEN_SECRET"),
        read_required_env("PVE_NODE"),
        verify=os.environ.get("PVE_TLS_CA_FILE", "").strip() or True,
    )
    builder = ProxmoxTemplateBuilder(api, read_settings())
    bundle = pack_bundle(ROOT, "proxmox", ROOT / ".cache/sandbox-images")
    build_hash = bundle.plan["buildHash"]
    name = f"{TEMPLATE_NAME_PREFIX}-{build_hash[:BUILD_HASH_NAME_CHARS]}"

    existing = builder.find_template(name)
    if existing and not existing.get("template"):
        raise ImageBuildError(f"VM {existing['vmid']} named {name} is not a template; remove it")
    if existing:
        template = int(existing["vmid"])
        print(f"{name}: re-verifying existing template {template}", file=sys.stderr)
    else:
        template = builder.build_template(name, bundle.directory)

    builder.verify_template(template, name, bundle.plan["runtimeEnv"])
    write_build_result(str(template))


if __name__ == "__main__":
    main()
