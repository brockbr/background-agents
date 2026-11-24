"""The Proxmox template: SMBIOS bootstrap seeding, the builder's scripts, and its API flow."""

import base64
import json
import runpy
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INFRA = ROOT / "packages/proxmox-infra"
SEED = runpy.run_path(str(INFRA / "seed-bootstrap.py"))
SCRIPTS = runpy.run_path(str(INFRA / "image_scripts.py"))

URL = "https://agents.example.com/sessions/s-1/sandbox-bootstrap"
TOKEN = "tok_abc-123"


def serial_for(payload: object) -> str:
    return "oi1:" + base64.b64encode(json.dumps(payload).encode()).decode()


# --- seed-bootstrap.py --------------------------------------------------------


def test_seeds_the_bootstrap_file_from_the_serial(tmp_path):
    serial = tmp_path / "serial"
    serial.write_text(serial_for({"url": URL, "token": TOKEN}) + "\n")
    etc = tmp_path / "etc"

    assert SEED["main"](["--serial", str(serial), "--etc", str(etc)]) == 0

    bootstrap = etc / "bootstrap"
    assert bootstrap.read_text() == (
        f"OPENINSPECT_BOOTSTRAP_URL={URL}\nOPENINSPECT_BOOTSTRAP_TOKEN={TOKEN}\n"
    )
    assert bootstrap.stat().st_mode & 0o777 == 0o600


def test_never_logs_the_token(tmp_path, capsys):
    serial = tmp_path / "serial"
    serial.write_text(serial_for({"url": URL, "token": TOKEN}))

    SEED["main"](["--serial", str(serial), "--etc", str(tmp_path / "etc")])

    captured = capsys.readouterr()
    assert TOKEN not in captured.err + captured.out


@pytest.mark.parametrize(
    "serial",
    [
        "",
        "To Be Filled By O.E.M.",
        "oi1:not-base64!",
        "oi1:" + base64.b64encode(b"[]").decode(),
        serial_for({"url": URL}),
        serial_for({"url": "http://plain.example/x", "token": TOKEN}),
        serial_for({"url": URL, "token": ""}),
        serial_for({"url": URL, "token": "two words"}),
        serial_for({"url": URL + "\nOPENINSPECT_BOOTSTRAP_TOKEN=forged", "token": TOKEN}),
    ],
)
def test_ignores_serials_that_are_not_a_bootstrap(tmp_path, serial):
    path = tmp_path / "serial"
    path.write_text(serial)

    SEED["main"](["--serial", str(path), "--etc", str(tmp_path / "etc")])

    assert not (tmp_path / "etc/bootstrap").exists()


def test_a_kept_session_is_never_reseeded(tmp_path):
    serial = tmp_path / "serial"
    serial.write_text(serial_for({"url": URL, "token": TOKEN}))
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "session.json").write_text("{}")

    SEED["main"](["--serial", str(serial), "--etc", str(etc)])

    assert not (etc / "bootstrap").exists()


def test_a_missing_serial_file_is_not_an_error(tmp_path):
    assert SEED["main"](["--serial", str(tmp_path / "nope"), "--etc", str(tmp_path)]) == 0


# --- image_scripts.py -----------------------------------------------------------


def test_every_file_the_image_installs_exists():
    for name in SCRIPTS["SHARED_FILES"]:
        assert (ROOT / "packages/ec2-infra" / name).is_file()
    for name in SCRIPTS["PROXMOX_FILES"]:
        assert (INFRA / name).is_file()


def test_build_script_installs_the_agent_first_and_seals_the_image():
    script = SCRIPTS["build_script"]()
    assert script.index("qemu-guest-agent") < script.index("install.sh")
    assert "systemctl enable openinspect-seed-bootstrap.service" in script
    assert "cloud-init clean" in script
    assert "truncate -s 0 /etc/machine-id" in script
    assert script.rstrip().endswith("sync")
    assert "poweroff" not in script  # the builder shuts the VM down once it has read the result


def test_verify_script_keeps_hostile_values_one_shell_word():
    hostile = {"PATH": "/x; rm -rf /", "HOME": "$(id)"}
    script = SCRIPTS["verify_script"](hostile, "OPENINSPECT_BOOTSTRAP_URL=a'b\n")
    line = next(line for line in script.splitlines() if "smoke_test.py" in line)
    words = shlex.split(line)
    assert "PATH=/x; rm -rf /" in words
    assert "HOME=$(id)" in words
    expected = "OPENINSPECT_BOOTSTRAP_URL=a'b\n"
    assert f"diff <(printf '%s' {shlex.quote(expected)}) /etc/openinspect/bootstrap" in script


# --- build-template.py (needs the builder's own dependencies) -------------------


@pytest.fixture(scope="module")
def builder_module():
    pytest.importorskip("requests")
    pytest.importorskip("pycdlib")
    sys.path.insert(0, str(INFRA))
    try:
        yield runpy.run_path(str(INFRA / "build-template.py"), run_name="proxmox_builder")
    finally:
        sys.path.remove(str(INFRA))


def test_smbios_serial_round_trips_through_the_seed(builder_module):
    serial = builder_module["smbios_serial"](URL, TOKEN)
    assert SEED["parse_serial"](serial) == (URL, TOKEN)


def test_smbios1_keeps_the_clone_uuid(builder_module):
    value = builder_module["smbios1_with_serial"]("uuid=1234-abcd,manufacturer=eA==", "oi1:xyz")
    assert value == "uuid=1234-abcd,serial=b2kxOnh5eg==,base64=1"
    assert builder_module["smbios1_with_serial"](None, "s") == "serial=cw==,base64=1"


def test_seed_iso_carries_real_file_names(builder_module):
    import io

    import pycdlib

    iso_bytes = builder_module["build_seed_iso"](
        {"user-data": b"#!/bin/bash\n", "meta-data": b"instance-id: x\n", "bundle.tar.gz": b"x"}
    )
    iso = pycdlib.PyCdlib()
    iso.open_fp(io.BytesIO(iso_bytes))
    try:
        names = {
            child.rock_ridge.name().decode()
            for child in iso.list_children(rr_path="/")
            if child.rock_ridge
        }
        assert {"user-data", "meta-data", "bundle.tar.gz"} <= names
        assert iso.pvd.volume_identifier.strip() == b"CIDATA"
    finally:
        iso.close()


class FakeApi:
    """Records the builder's calls; statuses come from a queue per VM."""

    def __init__(self, statuses):
        self.calls = []
        self.statuses = list(statuses)
        self.free = set(range(9000, 9010))

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "upload_iso":
                return f"{args[0]}:iso/{args[1]}"
            if name == "is_free_vmid":
                ok = args[0] in self.free
                self.free.discard(args[0])
                return ok
            if name == "agent_read_file":
                return self.statuses.pop(0) if self.statuses else None
            if name == "vm_status":
                return "running"
            if name == "get":
                return {"smbios1": "uuid=11111111-2222"}
            if name == "vm_path":
                return f"/nodes/pve/qemu/{args[0]}"
            return None

        return record

    def names(self):
        return [name for name, _, _ in self.calls]


SETTINGS = {
    "pool": "openinspect",
    "sandbox_storage": "local-lvm",
    "image_storage": "local",
    "base_image": "local:import/noble.qcow2",
    "bridge": "vmbr0",
    "vlan": "80",
    "vmid_min": 9000,
    "vmid_max": 9009,
    "keep_failed": False,
}


def make_builder(builder_module, statuses):
    api = FakeApi(statuses)
    builder = builder_module["ProxmoxTemplateBuilder"](api, SETTINGS, sleep=lambda _s: None)
    return builder, api


def test_build_creates_converts_and_cleans_up(builder_module, tmp_path):
    builder, api = make_builder(builder_module, [None, "OPENINSPECT_IMAGE_STEP_OK\n"])
    (tmp_path / "bundle").mkdir()

    vmid = builder.build_template("openinspect-sandbox-abc", tmp_path / "bundle")

    assert vmid == 9000
    names = api.names()
    for step in ("create_vm", "resize_disk", "start_vm", "shutdown_vm", "convert_to_template"):
        assert step in names
    assert names.index("shutdown_vm") < names.index("convert_to_template")
    create = next(kwargs for name, _, kwargs in api.calls if name == "create_vm")
    assert create["net0"] == "virtio,bridge=vmbr0,tag=80"
    assert "import-from=local:import/noble.qcow2" in create["scsi0"]
    assert create["scsi0"].endswith(",backup=0")
    assert create["pool"] == "openinspect"
    assert ("delete_volume", ("local:iso/openinspect-sandbox-abc-build.iso",), {}) in api.calls
    assert "destroy_vm" not in names


def test_a_failed_build_destroys_the_vm_and_reports_the_log(builder_module, tmp_path):
    builder, api = make_builder(
        builder_module, ["OPENINSPECT_IMAGE_STEP_FAILED: line 12\n", "apt exploded"]
    )
    (tmp_path / "bundle").mkdir()

    with pytest.raises(builder_module["ImageBuildError"], match="line 12"):
        builder.build_template("openinspect-sandbox-abc", tmp_path / "bundle")

    names = api.names()
    assert "convert_to_template" not in names
    assert "destroy_vm" in names
    assert "delete_volume" in names


def test_verify_seeds_a_fake_bootstrap_and_always_destroys_the_clone(builder_module):
    builder, api = make_builder(builder_module, ["OPENINSPECT_IMAGE_STEP_OK\n"])

    builder.verify_template(9000, "openinspect-sandbox-abc", {"PATH": "/usr/bin"})

    clone = next(args for name, args, _ in api.calls if name == "linked_clone")
    assert clone[0] == 9000
    update = next(kwargs for name, _, kwargs in api.calls if name == "update_vm")
    assert update["smbios1"].startswith("uuid=11111111-2222,serial=")
    assert "destroy_vm" in api.names()
