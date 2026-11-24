import { describe, expect, it, vi } from "vitest";
import type { SandboxBootstrapRecord, SandboxBootstrapStore } from "../bootstrap";
import { SandboxProviderError, type CreateSandboxConfig } from "../provider";
import {
  ProxmoxApiError,
  type ProxmoxCloneConfig,
  type ProxmoxCloneRequest,
  type ProxmoxSandboxApi,
  type ProxmoxVm,
} from "../proxmox-api";
import {
  PROXMOX_SANDBOX_TAG,
  ProxmoxSandboxProvider,
  proxmoxVmName,
  renderProxmoxBootstrapSerial,
  type ProxmoxProviderConfig,
} from "./proxmox-provider";

const NOW = 1_700_000_000_000;

function vm(vmid: number, overrides: Partial<ProxmoxVm> = {}): ProxmoxVm {
  return {
    vmid,
    name: `oi-x-${vmid}`,
    status: "running",
    uptimeSeconds: 0,
    template: false,
    ...overrides,
  };
}

function fakeApi(pool: ProxmoxVm[] = [], getVm: Array<ProxmoxVm | null> = []) {
  const vmQueue = [...getVm];
  return {
    listPoolVms: vi.fn(async () => pool),
    isVmidFree: vi.fn(async (vmid: number) => !pool.some((p) => p.vmid === vmid)),
    cloneVm: vi.fn(async (_request: ProxmoxCloneRequest) => {}),
    configureClone: vi.fn(async (_vmid: number, _config: ProxmoxCloneConfig) => {}),
    getVm: vi.fn(async (vmid: number) => {
      if (vmQueue.length > 1) return vmQueue.shift()!;
      return vmQueue.length === 1 ? vmQueue[0] : vm(vmid, { status: "stopped" });
    }),
    startVm: vi.fn(async (_vmid: number) => {}),
    requestShutdown: vi.fn(
      async (_vmid: number, _timeoutSeconds: number, _signal?: AbortSignal) => {}
    ),
    stopVm: vi.fn(async (_vmid: number, _signal?: AbortSignal) => {}),
    destroyVm: vi.fn(async (_vmid: number, _signal?: AbortSignal) => {}),
  } satisfies ProxmoxSandboxApi;
}

function fakeStore() {
  const puts: Array<{ record: SandboxBootstrapRecord; token: string; expiresAtMs: number }> = [];
  const store: SandboxBootstrapStore = {
    put: vi.fn(async (record, token, expiresAtMs) => {
      puts.push({ record, token, expiresAtMs });
    }),
    consume: vi.fn(async () => null),
  };
  return { store, puts };
}

const CONFIG: ProxmoxProviderConfig = {
  scmProvider: "github",
  templateVmid: 9000,
  pool: "openinspect",
  vmidMin: 8000,
  vmidMax: 8009,
  maxRunning: 2,
  cores: 2,
  memoryMib: 3072,
  deploymentName: "PVE Lab",
};

function createProvider(api: ProxmoxSandboxApi, store: SandboxBootstrapStore, overrides = {}) {
  return new ProxmoxSandboxProvider(api, store, { ...CONFIG, ...overrides }, () => NOW);
}

const createConfig: CreateSandboxConfig = {
  sessionId: "session-1",
  sandboxId: "sandbox-acme-web-app-1700000000000",
  repoOwner: "acme",
  repoName: "web-app",
  controlPlaneUrl: "https://control.example.com/",
  sandboxAuthToken: "sandbox-auth-secret",
  harness: "opencode",
  provider: "self-hosted",
  model: "default",
  userEnvVars: { MY_SECRET: "repo-secret" },
  timeoutSeconds: 3600,
  generationCreatedAtMs: NOW - 1000,
  codeServerEnabled: true,
};

function decodeSerial(serial: string): { url: string; token: string } {
  expect(serial.startsWith("oi1:")).toBe(true);
  return JSON.parse(atob(serial.slice(4)));
}

describe("ProxmoxSandboxProvider.createSandbox", () => {
  it("stores the environment behind a token and gives the clone only the token", async () => {
    const api = fakeApi();
    const { store, puts } = fakeStore();

    const result = await createProvider(api, store).createSandbox(createConfig);

    expect(result.providerObjectId).toBe("8000");
    expect(result.lifetime).toMatchObject({ kind: "finite", expiresAtMs: NOW - 1000 + 3_600_000 });
    expect(puts).toHaveLength(1);
    expect(puts[0].record.sandboxId).toBe(createConfig.sandboxId);
    expect(puts[0].record.env.MY_SECRET).toBe("repo-secret");

    expect(api.cloneVm).toHaveBeenCalledWith(
      expect.objectContaining({ templateVmid: 9000, newVmid: 8000, pool: "openinspect" })
    );
    const configure = api.configureClone.mock.calls[0][1];
    expect(decodeSerial(configure.smbiosSerial)).toEqual({
      url: "https://control.example.com/sessions/session-1/sandbox-bootstrap",
      token: puts[0].token,
    });
    expect(configure.smbiosSerial).not.toContain("repo-secret");
    expect(configure.smbiosSerial).not.toContain("sandbox-auth-secret");
    expect(configure).toMatchObject({
      tags: [PROXMOX_SANDBOX_TAG, "pve-lab"],
      cores: 2,
      memoryMib: 3072,
      excludeFromBackup: true,
    });
    expect(api.startVm).toHaveBeenCalledWith(8000);
  });

  it("skips VMIDs the pool or the cluster already uses", async () => {
    const api = fakeApi([vm(8000, { status: "stopped" })]);
    api.isVmidFree.mockImplementation(async (vmid: number) => vmid !== 8001 && vmid !== 8000);
    const { store } = fakeStore();

    const result = await createProvider(api, store).createSandbox(createConfig);

    expect(result.providerObjectId).toBe("8002");
  });

  it("moves to the next VMID when a racing launch took it", async () => {
    const api = fakeApi();
    api.cloneVm.mockRejectedValueOnce(new ProxmoxApiError("VM 8000 already exists", 500, false));
    const { store } = fakeStore();

    const result = await createProvider(api, store).createSandbox(createConfig);

    expect(result.providerObjectId).toBe("8001");
    expect(api.cloneVm).toHaveBeenCalledTimes(2);
  });

  it("finishes a generation whose clone already exists instead of cloning again", async () => {
    const name = proxmoxVmName(createConfig.sandboxId);
    const api = fakeApi([vm(8005, { name, status: "stopped" })]);
    const { store } = fakeStore();

    const result = await createProvider(api, store).createSandbox(createConfig);

    expect(result.providerObjectId).toBe("8005");
    expect(api.cloneVm).not.toHaveBeenCalled();
    expect(api.startVm).toHaveBeenCalledWith(8005);
  });

  it("refuses a launch past the running cap as transient", async () => {
    const api = fakeApi([
      vm(8000),
      vm(8001),
      vm(9000, { name: "openinspect-sandbox-x", template: true }),
    ]);
    const { store } = fakeStore();

    const error = await createProvider(api, store)
      .createSandbox(createConfig)
      .catch((e: unknown) => e);

    expect(error).toBeInstanceOf(SandboxProviderError);
    expect((error as SandboxProviderError).errorType).toBe("transient");
    expect(api.cloneVm).not.toHaveBeenCalled();
  });

  it("does not count stopped sessions, templates or foreign VMs against the cap", async () => {
    const api = fakeApi([
      vm(8000, { status: "stopped" }),
      vm(8001),
      vm(9000, { name: "oi-template", status: "running", template: true }),
      vm(8500, { name: "someone-elses-vm" }),
    ]);
    const { store } = fakeStore();

    await createProvider(api, store).createSandbox(createConfig);

    expect(api.cloneVm).toHaveBeenCalled();
  });

  it("destroys the clone when it cannot be configured or started", async () => {
    const api = fakeApi();
    api.startVm.mockRejectedValueOnce(new ProxmoxApiError("start failed", 500, false));
    const { store } = fakeStore();

    await expect(createProvider(api, store).createSandbox(createConfig)).rejects.toThrow(
      "start failed"
    );
    expect(api.destroyVm).toHaveBeenCalledWith(8000);
  });

  it("refuses a plain-http control plane", async () => {
    const api = fakeApi();
    const { store } = fakeStore();

    await expect(
      createProvider(api, store).createSandbox({
        ...createConfig,
        controlPlaneUrl: "http://control.example.com",
      })
    ).rejects.toThrow("https://");
    expect(api.cloneVm).not.toHaveBeenCalled();
  });
});

describe("ProxmoxSandboxProvider.resumeSandbox", () => {
  const resume = {
    providerObjectId: "8000",
    sessionId: "session-1",
    sandboxId: "sb",
    timeoutSeconds: 600,
  };

  it("starts a stopped VM", async () => {
    const api = fakeApi([], [vm(8000, { status: "stopped" })]);
    const result = await createProvider(api, fakeStore().store).resumeSandbox(resume);

    expect(result.success).toBe(true);
    expect(api.startVm).toHaveBeenCalledWith(8000);
    expect(result.success && result.lifetime).toMatchObject({ expiresAtMs: NOW + 600_000 });
  });

  it("measures a running VM's lifetime from its boot", async () => {
    const api = fakeApi([], [vm(8000, { status: "running", uptimeSeconds: 100 })]);
    const result = await createProvider(api, fakeStore().store).resumeSandbox(resume);

    expect(api.startVm).not.toHaveBeenCalled();
    expect(result.success && result.lifetime).toMatchObject({
      expiresAtMs: NOW - 100_000 + 600_000,
    });
  });

  it("asks for a fresh spawn when the VM is gone", async () => {
    const api = fakeApi([], [null]);
    const result = await createProvider(api, fakeStore().store).resumeSandbox(resume);

    expect(result).toMatchObject({ success: false, shouldSpawnFresh: true });
  });
});

describe("ProxmoxSandboxProvider.stopSandbox", () => {
  const stop = { providerObjectId: "8000", sessionId: "session-1", reason: "idle" };

  it("requests a graceful shutdown and keeps the disk", async () => {
    const api = fakeApi([], [vm(8000)]);
    const result = await createProvider(api, fakeStore().store).stopSandbox({
      ...stop,
      intent: "preserve",
    });

    expect(result.success).toBe(true);
    expect(api.requestShutdown).toHaveBeenCalledWith(8000, 60, undefined);
    expect(api.destroyVm).not.toHaveBeenCalled();
  });

  it("stops and destroys on destroy", async () => {
    const api = fakeApi([], [vm(8000)]);
    const result = await createProvider(api, fakeStore().store).stopSandbox({
      ...stop,
      intent: "destroy",
    });

    expect(result.success).toBe(true);
    expect(api.stopVm).toHaveBeenCalled();
    expect(api.destroyVm).toHaveBeenCalled();
  });

  it("treats an already-destroyed VM as destroyed", async () => {
    const api = fakeApi([], [null]);
    const result = await createProvider(api, fakeStore().store).stopSandbox({
      ...stop,
      intent: "destroy",
    });

    expect(result.success).toBe(true);
    expect(api.destroyVm).not.toHaveBeenCalled();
  });

  it("never destroys a template, even by reference", async () => {
    const api = fakeApi([], [vm(9000, { template: true, status: "stopped" })]);
    await createProvider(api, fakeStore().store).stopSandbox({
      ...stop,
      providerObjectId: "9000",
      intent: "destroy",
    });

    expect(api.destroyVm).not.toHaveBeenCalled();
  });
});

describe("Proxmox helpers", () => {
  it("names VMs as DNS labels", () => {
    expect(proxmoxVmName("Sandbox_Acme/Web App-1700000000000")).toBe(
      "oi-sandbox-acme-web-app-1700000000000"
    );
    expect(proxmoxVmName("x".repeat(100))).toHaveLength(63);
  });

  it("rejects values that do not belong in a serial", () => {
    expect(() => renderProxmoxBootstrapSerial("https://a.example/x y", "t")).toThrow();
    expect(() => renderProxmoxBootstrapSerial("https://a.example/x", "t\nINJECT=1")).toThrow();
  });
});
