"""Cloud-init user data for the template builder's VMs; free of HTTP code so tests load it.

The build VM and the verify clone each boot with a NoCloud ``cidata`` ISO
(see ``seed_iso.py``) carrying one of these scripts as ``user-data``. Progress
goes to the serial console and ``LOG_PATH``; the outcome goes to
``STATUS_PATH``, which ``build-template.py`` reads through the QEMU guest agent.
"""

from __future__ import annotations

import shlex

OK_MARKER = "OPENINSPECT_IMAGE_STEP_OK"
FAILED_MARKER = "OPENINSPECT_IMAGE_STEP_FAILED"
LOG_PATH = "/var/log/openinspect-image.log"
STATUS_PATH = "/var/log/openinspect-image.status"
SEED_MOUNT = "/mnt/openinspect-seed"
BUNDLE_NAME = "bundle.tar.gz"

# The EC2 launcher and runtime exec wrapper are reused unchanged; only how the
# bootstrap file arrives differs (SMBIOS serial instead of EC2 user data).
SHARED_FILES = {
    "launcher.sh": ("/opt/openinspect/ec2/launcher.sh", "0755"),
    "exec-runtime.py": ("/opt/openinspect/ec2/exec-runtime.py", "0755"),
    "openinspect-sandbox.service": ("/etc/systemd/system/openinspect-sandbox.service", "0644"),
}
PROXMOX_FILES = {
    "seed-bootstrap.py": ("/opt/openinspect/proxmox/seed-bootstrap.py", "0755"),
    "openinspect-seed-bootstrap.service": (
        "/etc/systemd/system/openinspect-seed-bootstrap.service",
        "0644",
    ),
    "openinspect-sandbox-seed.conf": (
        "/etc/systemd/system/openinspect-sandbox.service.d/10-proxmox-seed.conf",
        "0644",
    ),
    "99-openinspect-datasource.cfg": (
        "/etc/cloud/cloud.cfg.d/99-openinspect-datasource.cfg",
        "0644",
    ),
    "60-openinspect-userns.conf": ("/etc/sysctl.d/60-openinspect-userns.conf", "0644"),
}

SCRIPT_PRELUDE = f"""#!/bin/bash
set -Eeuo pipefail
exec > >(tee -a {LOG_PATH} >/dev/console) 2>&1
fail() {{
  echo "{FAILED_MARKER}: line $1"
  echo "{FAILED_MARKER}: line $1" >{STATUS_PATH}
  sync
}}
trap 'fail $LINENO' ERR
export DEBIAN_FRONTEND=noninteractive
rm -f {STATUS_PATH}
"""

# The builder can only read results once the guest agent runs, so it is installed first.
AGENT_SETUP = """
apt-get update
apt-get install -y --no-install-recommends qemu-guest-agent ca-certificates curl sudo
systemctl start qemu-guest-agent
"""

SUCCESS = f"""
echo "{OK_MARKER}"
echo "{OK_MARKER}" >{STATUS_PATH}
sync
"""


def _install_lines(files: dict[str, tuple[str, str]], source_dir: str) -> str:
    return "\n".join(
        f"install -D -m {mode} {shlex.quote(f'{source_dir}/{name}')} {shlex.quote(path)}"
        for name, (path, mode) in files.items()
    )


def build_script() -> str:
    """Install the sandbox bundle on a fresh Ubuntu cloud image and seal it for templating."""
    work = "/tmp/openinspect-image"
    return (
        SCRIPT_PRELUDE
        + AGENT_SETUP
        + f"""
mkdir -p {SEED_MOUNT} {work}
mount -o ro LABEL=CIDATA {SEED_MOUNT} || mount -o ro /dev/sr0 {SEED_MOUNT}
tar -xzf {SEED_MOUNT}/{BUNDLE_NAME} -C {work}
umount {SEED_MOUNT}
bash {work}/packages/sandbox-images/install/install.sh
{_install_lines(SHARED_FILES, f"{work}/vm")}
{_install_lines(PROXMOX_FILES, f"{work}/vm")}
printf '%s\\n' 'user ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/90-openinspect-user
chmod 0440 /etc/sudoers.d/90-openinspect-user
systemctl daemon-reload
systemctl enable openinspect-seed-bootstrap.service openinspect-sandbox.service qemu-guest-agent.service
rm -rf {work}
apt-get clean
cloud-init clean --logs --seed
rm -f /etc/ssh/ssh_host_* /var/lib/dhcp/* /etc/netplan/50-cloud-init.yaml
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
"""
        + SUCCESS
    )


def verify_script(runtime_env: dict[str, str], expected_bootstrap: str) -> str:
    """Check a linked clone of the template: units, SMBIOS seeding, then the image smoke test."""
    assignments = " ".join(shlex.quote(f"{key}={value}") for key, value in runtime_env.items())
    return (
        SCRIPT_PRELUDE
        + f"""
# The clone was given a fake bootstrap in its SMBIOS serial: it must have been seeded,
# and the launcher must not get in the way of the smoke test.
systemctl stop openinspect-sandbox.service || true
test -x /opt/openinspect/ec2/launcher.sh
test -x /opt/openinspect/proxmox/seed-bootstrap.py
systemctl is-enabled openinspect-seed-bootstrap.service
systemctl is-enabled openinspect-sandbox.service
systemctl is-enabled qemu-guest-agent.service
test "$(stat -c %a /etc/openinspect/bootstrap)" = 600
diff <(printf '%s' {shlex.quote(expected_bootstrap)}) /etc/openinspect/bootstrap
rm -f /etc/openinspect/bootstrap
env {assignments} /opt/openinspect/python/bin/python /app/verify/smoke_test.py verify
"""
        + SUCCESS
    )
