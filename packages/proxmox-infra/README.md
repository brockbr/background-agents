# Proxmox VE sandbox template

Builds the template VM the `proxmox` sandbox provider clones session VMs from, and holds what that
template runs at boot. Modeled on [packages/ec2-infra](../ec2-infra/README.md), whose launcher and
runtime wrapper it reuses unchanged.

| File                                                                         | Installed at                                                             | Role                                                                                               |
| ---------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `build-template.py`                                                          | (build host)                                                             | Builds and verifies the template; run through `npm run sandbox:images -- build --provider proxmox` |
| `pve_api.py`                                                                 | (build host)                                                             | Minimal Proxmox VE API client (API token auth)                                                     |
| `seed_iso.py`                                                                | (build host)                                                             | Builds the NoCloud `cidata` ISOs the build and verify VMs boot with                                |
| `image_scripts.py`                                                           | (build host)                                                             | The cloud-init user data for those VMs                                                             |
| `seed-bootstrap.py`                                                          | `/opt/openinspect/proxmox/seed-bootstrap.py`                             | Turns the clone's SMBIOS serial into `/etc/openinspect/bootstrap`                                  |
| `openinspect-seed-bootstrap.service`                                         | `/etc/systemd/system/openinspect-seed-bootstrap.service`                 | Runs the seeder before the launcher on every boot                                                  |
| `openinspect-sandbox-seed.conf`                                              | `/etc/systemd/system/openinspect-sandbox.service.d/10-proxmox-seed.conf` | Orders the EC2 launcher unit after the seeder                                                      |
| `60-openinspect-userns.conf`                                                 | `/etc/sysctl.d/60-openinspect-userns.conf`                               | Lifts Ubuntu 24.04's AppArmor user-namespace restriction so Chrome's sandbox works as `user`       |
| `99-openinspect-datasource.cfg`                                              | `/etc/cloud/cloud.cfg.d/99-openinspect-datasource.cfg`                   | Limits cloud-init to NoCloud and None, so clones don't probe cloud metadata services               |
| `../ec2-infra/launcher.sh`, `exec-runtime.py`, `openinspect-sandbox.service` | as in ec2-infra                                                          | Redeem the bootstrap, arm the lifetime backstop, start the runtime                                 |

## Prerequisites (once per cluster)

- A **resource pool** for templates and session VMs, e.g. `openinspect`.
- An **Ubuntu 24.04 cloud image** in a storage with the `import` content type:

  ```bash
  pvesh create /nodes/<node>/storage/local/download-url --content import \
    --filename noble-server-cloudimg-amd64.qcow2 \
    --url https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img \
    --checksum <sha256 from SHA256SUMS> --checksum-algorithm sha256
  ```

- A **network for sandboxes** with outbound internet (the build VM installs packages too), no access
  to anything else on the LAN except the control plane's HTTPS port.
- An **API token** (`privsep=1`) whose user and token both hold:

  | Path                                      | Privileges                                                                                                                                                                                                                                                                        |
  | ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | `/pool/<pool>`                            | `VM.Allocate VM.Audit VM.Clone VM.Config.CDROM VM.Config.CPU VM.Config.Cloudinit VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.PowerMgmt VM.GuestAgent.Audit VM.GuestAgent.FileRead Datastore.AllocateSpace Datastore.Audit Pool.Audit` |
  | `/storage/<sandbox storage>`              | `Datastore.AllocateSpace Datastore.Audit`                                                                                                                                                                                                                                         |
  | `/storage/<image storage>`                | `Datastore.AllocateTemplate Datastore.Allocate Datastore.Audit` (upload and remove the seed ISOs, read the base image)                                                                                                                                                            |
  | `/sdn/zones/localnetwork/<bridge>/<vlan>` | `SDN.Use`                                                                                                                                                                                                                                                                         |

## Build

```bash
export PVE_API_URL=https://pve.example.com:8006
export PVE_API_TOKEN_ID='openinspect@pve!builder'
export PVE_API_TOKEN_SECRET=...
export PVE_NODE=pve
export PVE_POOL=openinspect
export PVE_SANDBOX_STORAGE=local-lvm                               # needs linked-clone support
export PVE_IMAGE_STORAGE=local                                     # iso content
export PVE_BASE_IMAGE=local:import/noble-server-cloudimg-amd64.qcow2
export PVE_SANDBOX_BRIDGE=vmbr0
export PVE_SANDBOX_VLAN=80                                         # optional
export PVE_TEMPLATE_VMID_MIN=9000 PVE_TEMPLATE_VMID_MAX=9099       # optional, defaults shown
# PVE_TLS_CA_FILE=/path/ca.pem   when the API certificate is not publicly trusted
# PVE_KEEP_FAILED=1              keep a failed build VM for inspection
npm run sandbox:images -- build --provider proxmox --output /tmp/proxmox-image.json
cat /tmp/proxmox-image.json   # {"reference":"9000"}  — the template's VMID
```

1. The shared sandbox bundle is packed and wrapped, with the build script as `user-data`, in a
   NoCloud `cidata` ISO that is uploaded to `PVE_IMAGE_STORAGE`.
2. A VM is created in the pool from `PVE_BASE_IMAGE` (`import-from`), with the ISO as its CD-ROM, on
   the sandbox network. Its script installs the QEMU guest agent first, so the builder can read
   progress through the API, then runs the bundle's `install.sh`, installs the files above, and
   seals the disk (`cloud-init clean`, SSH host keys, machine ID).
3. The builder reads `OPENINSPECT_IMAGE_STEP_OK` from `/var/log/openinspect-image.status`, shuts the
   VM down, removes the CD-ROM and converts it to a template named
   `openinspect-sandbox-<16 hex build hash>`.
4. A **linked clone** boots with a verify ISO and a fake bootstrap in its SMBIOS serial. Its script
   checks the seeder wrote `/etc/openinspect/bootstrap` (mode 600, exact contents) and runs the
   image smoke test. The clone and both ISOs are removed, including on failure.

A template whose name already exists is re-verified instead of rebuilt.

## Boot sequence of a session clone

1. The provider clones the template and sets the clone's `smbios1` serial to `oi1:` plus the base64
   of `{"url": <redeem URL>, "token": <single-use token>}` — the Proxmox counterpart of EC2 user
   data. Anyone who may read the VM's configuration (`VM.Audit`) can read it until it is redeemed.
2. `openinspect-seed-bootstrap.service` writes `/etc/openinspect/bootstrap` from it, unless a
   session environment is already kept on the disk.
3. From there the boot is the EC2 one: `launcher.sh` redeems the token, keeps the environment, arms
   the lifetime backstop and starts the runtime as `user`. After a stop and start, the kept
   environment is used and the (spent) serial is ignored.
