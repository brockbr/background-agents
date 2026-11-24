import { describe, expect, it, vi } from "vitest";
import {
  createProxmoxSandboxApi,
  ProxmoxApiError,
  smbios1WithSerial,
  withBackupDisabled,
} from "./proxmox-api";

type Handler = (
  method: string,
  url: URL,
  body: URLSearchParams | null
) => [number, unknown, string?];

function fakeFetch(handler: Handler) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const body = init?.body ? new URLSearchParams(String(init.body)) : null;
    const [status, data, statusText] = handler(init?.method ?? "GET", url, body);
    return new Response(JSON.stringify(status < 400 ? { data } : (data ?? {})), {
      status,
      statusText: statusText ?? (status < 400 ? "OK" : "Error"),
    });
  });
}

function api(handler: Handler) {
  const fetchMock = fakeFetch(handler);
  return {
    fetchMock,
    client: createProxmoxSandboxApi({
      url: "https://pve.example.com:8006/",
      tokenId: "oi@pve!cp",
      tokenSecret: "s3cret",
      node: "pve",
      fetch: fetchMock as unknown as typeof fetch,
      sleep: async () => {},
    }),
  };
}

const TASK_OK: Handler = (_m, url) =>
  url.pathname.includes("/tasks/")
    ? [200, { status: "stopped", exitstatus: "OK" }]
    : [200, "UPID:pve:1:"];

describe("createProxmoxSandboxApi", () => {
  it("authenticates with the API token", async () => {
    const { client, fetchMock } = api(() => [200, { members: [] }]);
    await client.listPoolVms("openinspect");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("https://pve.example.com:8006/api2/json/pools/openinspect");
    expect((init?.headers as Record<string, string>).Authorization).toBe(
      "PVEAPIToken=oi@pve!cp=s3cret"
    );
  });

  it("lists only the pool's VMs", async () => {
    const { client } = api(() => [
      200,
      {
        members: [
          { type: "qemu", vmid: 8000, name: "oi-a", status: "running", uptime: 12 },
          { type: "qemu", vmid: 9000, name: "tpl", status: "stopped", template: 1 },
          { type: "storage", storage: "local-lvm" },
        ],
      },
    ]);
    expect(await client.listPoolVms("openinspect")).toEqual([
      { vmid: 8000, name: "oi-a", status: "running", uptimeSeconds: 12, template: false },
      { vmid: 9000, name: "tpl", status: "stopped", uptimeSeconds: 0, template: true },
    ]);
  });

  it("reports a missing VM as null rather than an error", async () => {
    const { client } = api(() => [
      500,
      {},
      "Configuration file 'nodes/pve/qemu-server/8000.conf' does not exist",
    ]);
    expect(await client.getVm(8000)).toBeNull();
  });

  it("reports a VM outside the token's pool (destroyed) as null", async () => {
    const { client } = api(() => [403, {}, "Permission check failed (/vms/8099, VM.Audit)"]);
    expect(await client.getVm(8099)).toBeNull();
  });

  it("still surfaces other permission failures", async () => {
    const { client } = api(() => [
      403,
      {},
      "Permission check failed (/pool/openinspect, Pool.Audit)",
    ]);
    await expect(client.listPoolVms("openinspect")).rejects.toThrow("Permission check failed");
  });

  it("clones linked and waits for the task", async () => {
    const calls: Array<[string, string, Record<string, string>]> = [];
    const { client } = api((method, url, body) => {
      calls.push([method, url.pathname, Object.fromEntries(body ?? [])]);
      return TASK_OK(method, url, body);
    });
    await client.cloneVm({
      templateVmid: 9000,
      newVmid: 8001,
      name: "oi-x",
      pool: "openinspect",
      description: "d",
    });
    expect(calls[0]).toEqual([
      "POST",
      "/api2/json/nodes/pve/qemu/9000/clone",
      { newid: "8001", name: "oi-x", pool: "openinspect", description: "d", full: "0" },
    ]);
    expect(calls[1][1]).toContain("/tasks/UPID%3Apve%3A1%3A/status");
  });

  it("surfaces a failed task", async () => {
    const { client } = api((_m, url) =>
      url.pathname.includes("/tasks/")
        ? [200, { status: "stopped", exitstatus: "clone failed: no space" }]
        : [200, "UPID:pve:2:"]
    );
    await expect(client.startVm(8001)).rejects.toThrow("no space");
  });

  it("configures the clone: serial with its UUID kept, tags, size, disks out of backups", async () => {
    let posted: Record<string, string> = {};
    const { client } = api((method, url, body) => {
      if (method === "GET" && url.pathname.endsWith("/config")) {
        return [
          200,
          {
            smbios1: "uuid=abcd-1234",
            scsi0: "local-lvm:base-9000-disk-0/vm-8001-disk-0,discard=on,size=48G",
            ide2: "local-lvm:vm-8001-cloudinit,media=cdrom",
            net0: "virtio=BC:24:11:00:00:01,bridge=vmbr0,tag=80",
          },
        ];
      }
      if (method === "POST") posted = Object.fromEntries(body ?? []);
      return [200, null];
    });
    await client.configureClone(8001, {
      smbiosSerial: "oi1:abc",
      tags: ["openinspect-sandbox", "lab"],
      cores: 2,
      memoryMib: 3072,
      excludeFromBackup: true,
    });
    expect(posted).toEqual({
      smbios1: `uuid=abcd-1234,serial=${btoa("oi1:abc")},base64=1`,
      tags: "openinspect-sandbox;lab",
      cores: "2",
      memory: "3072",
      scsi0: "local-lvm:base-9000-disk-0/vm-8001-disk-0,discard=on,size=48G,backup=0",
    });
  });

  it("treats a taken VMID as not free and a network failure as an error", async () => {
    const taken = api(() => [400, { errors: { vmid: "VM 8000 already exists" } }]);
    expect(await taken.client.isVmidFree(8000)).toBe(false);

    const down = createProxmoxSandboxApi({
      url: "https://pve.example.com:8006",
      tokenId: "t",
      tokenSecret: "s",
      node: "pve",
      fetch: (async () => {
        throw new TypeError("fetch failed");
      }) as unknown as typeof fetch,
    });
    const error = await down.isVmidFree(8000).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ProxmoxApiError);
    expect((error as ProxmoxApiError).isRetryable).toBe(true);
  });

  it("does not wait on a shutdown request", async () => {
    const { client, fetchMock } = api(() => [200, "UPID:pve:3:"]);
    await client.requestShutdown(8001, 60);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = new URLSearchParams(String(fetchMock.mock.calls[0][1]?.body));
    expect(Object.fromEntries(body)).toEqual({ timeout: "60", forceStop: "1" });
  });
});

describe("Proxmox option helpers", () => {
  it("keeps the generated UUID when setting the serial", () => {
    expect(smbios1WithSerial("uuid=1234,manufacturer=eA==", "oi1:x")).toBe(
      `uuid=1234,serial=${btoa("oi1:x")},base64=1`
    );
    expect(smbios1WithSerial(undefined, "s")).toBe(`serial=${btoa("s")},base64=1`);
  });

  it("disables backups on a disk once", () => {
    expect(withBackupDisabled("local-lvm:vm-1-disk-0,size=8G")).toBe(
      "local-lvm:vm-1-disk-0,size=8G,backup=0"
    );
    expect(withBackupDisabled("local-lvm:vm-1-disk-0,backup=0")).toBe(
      "local-lvm:vm-1-disk-0,backup=0"
    );
    expect(withBackupDisabled("local-lvm:vm-1-disk-0,backup=1,size=8G")).toBe(
      "local-lvm:vm-1-disk-0,size=8G,backup=0"
    );
  });
});
