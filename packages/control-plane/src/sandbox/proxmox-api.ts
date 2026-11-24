/**
 * The slice of the Proxmox VE API the `proxmox` sandbox provider uses, and a
 * `fetch` implementation of it authenticated with an API token.
 *
 * Every mutating call returns once Proxmox has accepted it and, where it
 * starts a task, once that task has finished, except where noted: a graceful
 * shutdown is requested and not awaited, like EC2's StopInstances.
 */

export type ProxmoxVmStatus = "running" | "stopped" | "paused" | "unknown";

export interface ProxmoxVm {
  vmid: number;
  name: string;
  status: ProxmoxVmStatus;
  /** Seconds since the VM last started; 0 when stopped. */
  uptimeSeconds: number;
  template: boolean;
}

export interface ProxmoxCloneRequest {
  templateVmid: number;
  newVmid: number;
  name: string;
  pool: string;
  description: string;
}

export interface ProxmoxCloneConfig {
  /** Raw SMBIOS type-1 serial (encoded by the client for Proxmox). */
  smbiosSerial: string;
  tags: string[];
  cores?: number;
  memoryMib?: number;
  /** Keep the clone's disks out of vzdump jobs. */
  excludeFromBackup: boolean;
}

export class ProxmoxApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly isRetryable: boolean,
    cause?: unknown
  ) {
    super(message, cause === undefined ? undefined : { cause });
    this.name = "ProxmoxApiError";
  }

  /**
   * Proxmox answers a missing VM with 500 and "does not exist", not 404. A token
   * scoped to a pool gets 403 for a VMID outside it, which is what a destroyed
   * session VM becomes: for this provider, not ours means gone.
   */
  get isNotFound(): boolean {
    return (
      this.status === 404 ||
      /does not exist/i.test(this.message) ||
      (this.status === 403 && /Permission check failed \(\/vms\/\d+,/.test(this.message))
    );
  }

  get isAlreadyExists(): boolean {
    return /already exists/i.test(this.message);
  }
}

export interface ProxmoxSandboxApi {
  listPoolVms(pool: string, signal?: AbortSignal): Promise<ProxmoxVm[]>;
  isVmidFree(vmid: number, signal?: AbortSignal): Promise<boolean>;
  cloneVm(request: ProxmoxCloneRequest, signal?: AbortSignal): Promise<void>;
  configureClone(vmid: number, config: ProxmoxCloneConfig, signal?: AbortSignal): Promise<void>;
  getVm(vmid: number, signal?: AbortSignal): Promise<ProxmoxVm | null>;
  startVm(vmid: number, signal?: AbortSignal): Promise<void>;
  /** Request an ACPI shutdown that turns into a hard stop after `timeoutSeconds`; not awaited. */
  requestShutdown(vmid: number, timeoutSeconds: number, signal?: AbortSignal): Promise<void>;
  stopVm(vmid: number, signal?: AbortSignal): Promise<void>;
  destroyVm(vmid: number, signal?: AbortSignal): Promise<void>;
}

export interface ProxmoxApiConfig {
  /** e.g. https://pve.example.com:8006 */
  url: string;
  /** e.g. openinspect@pve!control-plane */
  tokenId: string;
  tokenSecret: string;
  node: string;
  fetch?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  /** Upper bound for waiting on a Proxmox task (clone, start, stop, destroy). */
  taskTimeoutMs?: number;
}

const TASK_POLL_INTERVAL_MS = 1_000;
const DEFAULT_TASK_TIMEOUT_MS = 5 * 60_000;
const REQUEST_TIMEOUT_MS = 30_000;

type Params = Record<string, string | number | undefined>;

function encodeParams(params: Params): URLSearchParams {
  const encoded = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) encoded.set(key, String(value));
  }
  return encoded;
}

function toVm(raw: Record<string, unknown>): ProxmoxVm {
  const status = raw.status;
  return {
    vmid: Number(raw.vmid),
    name: typeof raw.name === "string" ? raw.name : "",
    status:
      status === "running" || status === "stopped" || status === "paused" ? status : "unknown",
    uptimeSeconds: typeof raw.uptime === "number" ? raw.uptime : 0,
    template: raw.template === 1 || raw.template === true,
  };
}

/** Keep the generated UUID; replace any serial with ours. */
export function smbios1WithSerial(current: string | undefined, serial: string): string {
  const uuid = /(?:^|,)uuid=([0-9a-fA-F-]+)/.exec(current ?? "")?.[1];
  const encoded = btoa(String.fromCharCode(...new TextEncoder().encode(serial)));
  return [uuid ? `uuid=${uuid}` : undefined, `serial=${encoded}`, "base64=1"]
    .filter(Boolean)
    .join(",");
}

/** Append `backup=0` to a disk option string unless it already says so. */
export function withBackupDisabled(disk: string): string {
  return /(?:^|,)backup=0(?:,|$)/.test(disk)
    ? disk
    : `${disk.replace(/(?:^|,)backup=1(?=,|$)/, "")},backup=0`;
}

const DISK_KEY = /^(?:scsi|virtio|sata|ide)\d+$/;

export function createProxmoxSandboxApi(config: ProxmoxApiConfig): ProxmoxSandboxApi {
  const base = `${config.url.replace(/\/+$/, "")}/api2/json`;
  const doFetch = config.fetch ?? fetch;
  const sleep = config.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  const taskTimeoutMs = config.taskTimeoutMs ?? DEFAULT_TASK_TIMEOUT_MS;
  const authorization = `PVEAPIToken=${config.tokenId}=${config.tokenSecret}`;
  const vmPath = (vmid: number) => `/nodes/${encodeURIComponent(config.node)}/qemu/${vmid}`;

  async function request(
    method: "GET" | "POST" | "PUT" | "DELETE",
    path: string,
    params: Params = {},
    signal?: AbortSignal
  ): Promise<unknown> {
    const body = method === "POST" || method === "PUT" ? encodeParams(params) : undefined;
    const query = body ? "" : `?${encodeParams(params).toString()}`;
    const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
    let response: Response;
    try {
      response = await doFetch(`${base}${path}${query === "?" ? "" : query}`, {
        method,
        headers: {
          Authorization: authorization,
          ...(body ? { "Content-Type": "application/x-www-form-urlencoded" } : {}),
        },
        body,
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
    } catch (error) {
      throw new ProxmoxApiError(`${method} ${path}: ${String(error)}`, 0, true, error);
    }
    const text = await response.text();
    if (!response.ok) {
      // Proxmox puts the reason in the status text, field errors in the body.
      let detail = response.statusText;
      try {
        const errors = (JSON.parse(text) as { errors?: unknown }).errors;
        if (errors) detail = `${detail}: ${JSON.stringify(errors)}`;
      } catch {
        // Not JSON; the status text is all there is.
      }
      const retryable = response.status === 429 || response.status >= 502;
      throw new ProxmoxApiError(
        `${method} ${path}: HTTP ${response.status} ${detail}`,
        response.status,
        retryable
      );
    }
    return (JSON.parse(text) as { data?: unknown }).data;
  }

  async function waitTask(upid: unknown, signal?: AbortSignal): Promise<void> {
    if (typeof upid !== "string" || !upid.startsWith("UPID:")) return;
    const deadline = Date.now() + taskTimeoutMs;
    const path = `/nodes/${encodeURIComponent(config.node)}/tasks/${encodeURIComponent(upid)}/status`;
    for (;;) {
      const status = (await request("GET", path, {}, signal)) as {
        status?: string;
        exitstatus?: string;
      };
      if (status?.status === "stopped") {
        if (status.exitstatus !== "OK") {
          throw new ProxmoxApiError(`task ${upid} failed: ${status.exitstatus}`, 500, false);
        }
        return;
      }
      if (Date.now() >= deadline) {
        throw new ProxmoxApiError(`task ${upid} did not finish in ${taskTimeoutMs} ms`, 0, true);
      }
      signal?.throwIfAborted();
      await sleep(TASK_POLL_INTERVAL_MS);
    }
  }

  return {
    async listPoolVms(pool, signal) {
      const data = (await request("GET", `/pools/${encodeURIComponent(pool)}`, {}, signal)) as {
        members?: Array<Record<string, unknown>>;
      };
      return (data?.members ?? []).filter((m) => m.type === "qemu").map(toVm);
    },

    async isVmidFree(vmid, signal) {
      try {
        await request("GET", "/cluster/nextid", { vmid }, signal);
        return true;
      } catch (error) {
        if (error instanceof ProxmoxApiError && !error.isRetryable) return false;
        throw error;
      }
    },

    async cloneVm(clone, signal) {
      const upid = await request(
        "POST",
        `${vmPath(clone.templateVmid)}/clone`,
        {
          newid: clone.newVmid,
          name: clone.name,
          pool: clone.pool,
          description: clone.description,
          full: 0,
        },
        signal
      );
      await waitTask(upid, signal);
    },

    async configureClone(vmid, clone, signal) {
      const current = (await request("GET", `${vmPath(vmid)}/config`, {}, signal)) as Record<
        string,
        unknown
      >;
      const params: Params = {
        smbios1: smbios1WithSerial(
          typeof current.smbios1 === "string" ? current.smbios1 : undefined,
          clone.smbiosSerial
        ),
        tags: clone.tags.join(";"),
        cores: clone.cores,
        memory: clone.memoryMib,
      };
      if (clone.excludeFromBackup) {
        for (const [key, value] of Object.entries(current)) {
          if (DISK_KEY.test(key) && typeof value === "string" && !value.includes("media=cdrom")) {
            params[key] = withBackupDisabled(value);
          }
        }
      }
      await waitTask(await request("POST", `${vmPath(vmid)}/config`, params, signal), signal);
    },

    async getVm(vmid, signal) {
      try {
        const data = (await request("GET", `${vmPath(vmid)}/status/current`, {}, signal)) as Record<
          string,
          unknown
        >;
        return toVm({ ...data, vmid });
      } catch (error) {
        if (error instanceof ProxmoxApiError && error.isNotFound) return null;
        throw error;
      }
    },

    async startVm(vmid, signal) {
      await waitTask(await request("POST", `${vmPath(vmid)}/status/start`, {}, signal), signal);
    },

    async requestShutdown(vmid, timeoutSeconds, signal) {
      await request(
        "POST",
        `${vmPath(vmid)}/status/shutdown`,
        { timeout: timeoutSeconds, forceStop: 1 },
        signal
      );
    },

    async stopVm(vmid, signal) {
      await waitTask(await request("POST", `${vmPath(vmid)}/status/stop`, {}, signal), signal);
    },

    async destroyVm(vmid, signal) {
      await waitTask(
        await request(
          "DELETE",
          vmPath(vmid),
          { purge: 1, "destroy-unreferenced-disks": 1 },
          signal
        ),
        signal
      );
    },
  };
}
