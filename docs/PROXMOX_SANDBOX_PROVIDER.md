# Proxmox VE Sandbox Provider

Open-Inspect can run each session's sandbox as a virtual machine on your own Proxmox VE cluster: a
linked clone of a template that [packages/proxmox-infra](../packages/proxmox-infra/README.md)
builds. The sandboxes, and the checkouts and secrets they hold, stay on your hardware. Like any
provider's sandbox, a Proxmox sandbox talks to your source-control host, your model provider and any
MCP servers you configure, so give its network outbound internet access and nothing else.

`SANDBOX_PROVIDER=proxmox` is meant for the **Node-hosted control plane** (`docker compose`, see
[CONTROL_PLANE_CONTAINER.md](CONTROL_PLANE_CONTAINER.md)) on a network that reaches the Proxmox API.
The control plane calls the API with a token scoped to one resource pool; sandboxes never see it.

## How it works

The flow is the [EC2 provider](EC2_SANDBOX_PROVIDER.md)'s, with Proxmox calls:

| Step    | What happens                                                                                                                                                                                                                                                                                                                                              |
| ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Launch  | The control plane stores the session environment behind a single-use bootstrap token, makes a linked clone of the template in the pool, and puts the redeem URL and token in the clone's **SMBIOS serial** (`smbios1`), the counterpart of EC2 user data. It sets the clone's size, tags it, keeps its disk out of vzdump jobs (`backup=0`) and starts it |
| Boot    | A unit in the template writes `/etc/openinspect/bootstrap` from the serial; the launcher redeems it once over HTTPS and starts the runtime, which connects back over a WebSocket                                                                                                                                                                          |
| Idle    | The VM gets an ACPI shutdown (a hard stop after 60 s). Its disk keeps the checkout, the agent's state and the fetched environment                                                                                                                                                                                                                         |
| Resume  | The same VM starts again and resumes on the kept checkout, without re-running `setup.sh`                                                                                                                                                                                                                                                                  |
| Archive | Nothing is destroyed: an archived session can be restored, so its VM stays stopped with its disk. A session that is cancelled has its VM stopped and destroyed                                                                                                                                                                                            |

The token is single-use, expires after `SANDBOX_BOOTSTRAP_TOKEN_TTL_MS`, and works only for the
session's current sandbox generation. The serial is readable by anyone allowed to read the VM's
configuration (`VM.Audit`) until it is redeemed, so keep that to administrators. The launcher powers
the VM off at the session lifetime plus a grace period, counted from each boot.

A launch whose response was lost is finished rather than repeated: the clone's name identifies the
sandbox generation, and a clone that already exists is configured and started. Two launches racing
for one VMID move on to the next free one.

### Cleaning up

Session VMs are never removed on their own, since sessions are archived rather than deleted and an
archived session can be restored. Their disks are copy-on-write clones and grow only with what the
session wrote. To reclaim space, destroy the VMs of archived sessions you will not restore
(`qm destroy <vmid> --purge 1`); a restored session whose VM is gone starts a fresh one. A template
can be removed once no clone uses it.

### Capacity

Every clone holds its own copy-on-write disk and, while running, its memory. Set
`PROXMOX_SANDBOX_MAX_RUNNING` so that number of running sandboxes fits beside the cluster's other
guests. A launch past the cap fails as a transient error and is retried. Stopped sessions do not
count.

### Not supported (yet)

- Preview URLs: code-server, the web terminal, VNC and tunnel ports.
- Prebuilt repository images; every session clones and runs `setup.sh` on its first boot.
- Docker inside the sandbox.
- Per-session CPU and memory settings; one size per deployment.
- Multi-node clusters: the template and the clones live on `PROXMOX_NODE`.

## Setup

1. **Network.** A bridge or VLAN for sandboxes with outbound internet and no access to the rest of
   your network, except the control plane's HTTPS port and your model server if you run one. The
   control plane's URL (`WORKER_URL`) must be `https://` and resolve from that network.
2. **Pool and template.** Create a resource pool and build the template as described in
   [packages/proxmox-infra](../packages/proxmox-infra/README.md). The build prints the template's
   VMID.
3. **API token** for the control plane: a user with `privsep=1` token, and for both the user and the
   token:

   | Path                                      | Privileges                                                                                                                                                                       |
   | ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `/pool/<pool>`                            | `VM.Allocate VM.Audit VM.Clone VM.Config.Options VM.Config.HWType VM.Config.Disk VM.Config.CPU VM.Config.Memory VM.PowerMgmt Datastore.AllocateSpace Datastore.Audit Pool.Audit` |
   | `/storage/<template storage>`             | `Datastore.AllocateSpace Datastore.Audit`                                                                                                                                        |
   | `/sdn/zones/localnetwork/<bridge>/<vlan>` | `SDN.Use`                                                                                                                                                                        |

4. **Configure** the Node host's `.env` (below), set `SANDBOX_PROVIDER=proxmox`, and restart. Build
   the web app with `NEXT_PUBLIC_SANDBOX_PROVIDER=proxmox`.

## Model keys

The provider passes no deployment-wide model key to sandboxes. Add the keys for the models you plan
to use as **global secrets** in Settings › Secrets. For a model server you run yourself, see the
Self-hosted model in [AVAILABLE_MODELS.md](AVAILABLE_MODELS.md).

## Configuration reference

| Variable                        | Required | Meaning                                                        |
| ------------------------------- | -------- | -------------------------------------------------------------- |
| `SANDBOX_PROVIDER`              | yes      | `proxmox`                                                      |
| `PROXMOX_API_URL`               | yes      | API base, e.g. `https://pve.example.com:8006`                  |
| `PROXMOX_API_TOKEN_ID`          | yes      | e.g. `openinspect@pve!control-plane`                           |
| `PROXMOX_API_TOKEN_SECRET`      | yes      | The token's secret                                             |
| `PROXMOX_NODE`                  | yes      | Node that holds the template and runs session VMs              |
| `PROXMOX_SANDBOX_TEMPLATE_VMID` | yes      | Template built by `packages/proxmox-infra`                     |
| `PROXMOX_SANDBOX_POOL`          | yes      | Pool session VMs are cloned into                               |
| `PROXMOX_SANDBOX_VMID_MIN`      | no       | First VMID for session VMs (default 8000)                      |
| `PROXMOX_SANDBOX_VMID_MAX`      | no       | Last VMID for session VMs (default 8999)                       |
| `PROXMOX_SANDBOX_MAX_RUNNING`   | no       | Running session VMs allowed at once; unset or `0` for no limit |
| `PROXMOX_SANDBOX_CORES`         | no       | vCPUs per session VM; default: the template's                  |
| `PROXMOX_SANDBOX_MEMORY_MIB`    | no       | Memory per session VM; default: the template's                 |

For an API certificate that is not publicly trusted, point the Node host's `NODE_EXTRA_CA_CERTS` at
its CA.

To rebuild the template after `packages/sandbox-images`, `packages/sandbox-runtime` or
`packages/proxmox-infra` change, run the build again and update `PROXMOX_SANDBOX_TEMPLATE_VMID`.
Running sessions keep the template they were cloned from, and Proxmox will not remove a template
that linked clones still use.
