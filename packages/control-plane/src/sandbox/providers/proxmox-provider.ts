import { supportsConfigurableSandboxTimeout } from "@open-inspect/shared/types/integrations";
import { createLogger } from "../../logger";
import type { SourceControlProviderName } from "../../source-control";
import {
  generateSandboxBootstrapToken,
  SANDBOX_BOOTSTRAP_TOKEN_TTL_MS,
  type SandboxBootstrapStore,
} from "../bootstrap";
import {
  DEFAULT_SANDBOX_TIMEOUT_SECONDS,
  SandboxProviderError,
  signalUntilDeadline,
  type CreateSandboxConfig,
  type CreateSandboxResult,
  type ResumeConfig,
  type ResumeResult,
  type SandboxLifetime,
  type SandboxProvider,
  type SandboxProviderCapabilities,
  type StopConfig,
  type StopResult,
} from "../provider";
import { ProxmoxApiError, type ProxmoxSandboxApi, type ProxmoxVm } from "../proxmox-api";
import {
  buildSandboxEnvVars,
  DEFERRED_START_ENV_VAR,
  IMAGE_BUILD_MODE_ENV_VAR,
  scmCloneIdentity,
} from "../sandbox-env";
import { resolveSandboxPortPlan } from "./port-resolution";

const log = createLogger("proxmox-provider");

/** Every session VM's name starts with this; the pool may hold templates and other VMs too. */
export const PROXMOX_VM_NAME_PREFIX = "oi-";
export const PROXMOX_SANDBOX_TAG = "openinspect-sandbox";
const PROXMOX_VM_NAME_MAX_LENGTH = 63;
const BOOTSTRAP_PATH_SUFFIX = "/sandbox-bootstrap";
/** Seconds the guest gets to shut down cleanly before Proxmox stops it. */
const SHUTDOWN_TIMEOUT_SECONDS = 60;
/** Two launches racing for the same VMID: the loser moves to the next one. */
const CLONE_ATTEMPTS = 5;
const MS_PER_SECOND = 1000;

// The bootstrap travels in the SMBIOS serial: keep it to characters a URL and a token need.
const SERIAL_VALUE_PATTERN = /^[A-Za-z0-9._~:/?#[\]@!$&()*+,;=%-]+$/;

export interface ProxmoxProviderConfig {
  scmProvider: SourceControlProviderName;
  templateVmid: number;
  pool: string;
  vmidMin: number;
  vmidMax: number;
  /** Running session VMs allowed at once; 0 means no limit. */
  maxRunning: number;
  cores?: number;
  memoryMib?: number;
  deploymentName: string;
}

/**
 * The SMBIOS serial packages/proxmox-infra/seed-bootstrap.py turns into
 * /etc/openinspect/bootstrap: the Proxmox counterpart of EC2 user data.
 */
export function renderProxmoxBootstrapSerial(bootstrapUrl: string, token: string): string {
  for (const value of [bootstrapUrl, token]) {
    if (!SERIAL_VALUE_PATTERN.test(value)) {
      throw new SandboxProviderError("Proxmox bootstrap value is not serial safe", "permanent");
    }
  }
  if (new URL(bootstrapUrl).protocol !== "https:") {
    throw new SandboxProviderError(
      "Proxmox sandboxes need an https:// control plane URL to receive their environment",
      "permanent"
    );
  }
  const payload = JSON.stringify({ url: bootstrapUrl, token });
  return `oi1:${btoa(payload)}`;
}

/** A DNS-label-safe VM name that identifies the sandbox generation. */
export function proxmoxVmName(sandboxId: string): string {
  const slug = sandboxId.toLowerCase().replace(/[^a-z0-9-]+/g, "-");
  return `${PROXMOX_VM_NAME_PREFIX}${slug}`.slice(0, PROXMOX_VM_NAME_MAX_LENGTH).replace(/-+$/, "");
}

/** Proxmox tags: lowercase letters, digits and `-_+.`. */
function tagValue(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9_+.-]+/g, "-")
      .slice(0, 64) || "default"
  );
}

export class ProxmoxSandboxProvider implements SandboxProvider {
  readonly name = "proxmox";

  readonly capabilities: SandboxProviderCapabilities = {
    supportsSandboxTimeout: supportsConfigurableSandboxTimeout(this.name),
    supportsSnapshots: false,
    supportsRestore: false,
    supportsPersistentResume: true,
    supportsExplicitStop: true,
  };

  constructor(
    private readonly api: ProxmoxSandboxApi,
    private readonly bootstrapStore: SandboxBootstrapStore,
    private readonly config: ProxmoxProviderConfig,
    private readonly now: () => number = Date.now
  ) {}

  async createSandbox(config: CreateSandboxConfig): Promise<CreateSandboxResult> {
    const bootstrapUrl = `${config.controlPlaneUrl.replace(/\/+$/, "")}/sessions/${encodeURIComponent(
      config.sessionId
    )}${BOOTSTRAP_PATH_SUFFIX}`;
    const token = generateSandboxBootstrapToken();
    const serial = renderProxmoxBootstrapSerial(bootstrapUrl, token);
    const name = proxmoxVmName(config.sandboxId);

    // Stored before the clone starts: a booting VM redeems it immediately.
    await this.bootstrapStore.put(
      { sandboxId: config.sandboxId, env: this.buildEnvVars(config) },
      token,
      this.now() + SANDBOX_BOOTSTRAP_TOKEN_TTL_MS
    );

    try {
      const vms = await this.api.listPoolVms(this.config.pool);
      // A launch whose response was lost already cloned this generation: finish it, don't clone twice.
      let vmid = vms.find((vm) => vm.name === name && !vm.template)?.vmid;
      if (vmid === undefined) {
        this.assertCapacity(vms);
        vmid = await this.cloneWithFreeVmid(vms, name, config);
      }
      try {
        await this.api.configureClone(vmid, {
          smbiosSerial: serial,
          tags: [PROXMOX_SANDBOX_TAG, tagValue(this.config.deploymentName)],
          cores: this.config.cores,
          memoryMib: this.config.memoryMib,
          excludeFromBackup: true,
        });
        const vm = await this.api.getVm(vmid);
        if (vm?.status !== "running") await this.api.startVm(vmid);
      } catch (error) {
        await this.destroyQuietly(vmid);
        throw error;
      }
      log.info("proxmox.vm_launched", {
        session_id: config.sessionId,
        sandbox_id: config.sandboxId,
        vmid,
      });
      const createdAt = this.now();
      return {
        sandboxId: config.sandboxId,
        providerObjectId: String(vmid),
        createdAt,
        lifetime: this.computeLifetime(
          config.generationCreatedAtMs ?? createdAt,
          config.timeoutSeconds
        ),
      };
    } catch (error) {
      throw classifyProxmoxError("Failed to launch Proxmox sandbox", error);
    }
  }

  async resumeSandbox(config: ResumeConfig): Promise<ResumeResult> {
    try {
      const vmid = parseVmid(config.providerObjectId);
      const vm = vmid === null ? null : await this.api.getVm(vmid);
      if (vmid === null || !vm || vm.template) {
        return { success: false, error: "Proxmox VM no longer exists", shouldSpawnFresh: true };
      }
      let startedAtMs = this.now();
      if (vm.status === "stopped") {
        await this.api.startVm(vmid);
      } else if (vm.status === "running") {
        // Still running: its backstop was armed when it booted, not now.
        startedAtMs = this.now() - vm.uptimeSeconds * MS_PER_SECOND;
      } else {
        throw new SandboxProviderError(`Proxmox VM is ${vm.status}`, "transient");
      }
      return {
        success: true,
        providerObjectId: config.providerObjectId,
        lifetime: this.computeLifetime(startedAtMs, config.timeoutSeconds),
      };
    } catch (error) {
      throw classifyProxmoxError("Failed to resume Proxmox sandbox", error);
    }
  }

  async stopSandbox(config: StopConfig): Promise<StopResult> {
    const signal = signalUntilDeadline(config.deadlineAtMs, config.signal);
    const vmid = parseVmid(config.providerObjectId);
    try {
      if (vmid === null) {
        return config.intent === "destroy"
          ? { success: true }
          : { success: false, error: "Not a Proxmox VM reference" };
      }
      const vm = await this.api.getVm(vmid, signal);
      if (config.intent === "destroy") {
        if (!vm || vm.template) return { success: true };
        if (vm.status !== "stopped") await this.api.stopVm(vmid, signal);
        await this.destroyIfKnown(vmid, signal);
        return { success: true };
      }
      if (!vm) {
        return {
          success: false,
          error: "Sandbox disappeared before graceful shutdown was verified",
        };
      }
      if (vm.status === "running" || vm.status === "paused") {
        await this.api.requestShutdown(vmid, SHUTDOWN_TIMEOUT_SECONDS, signal);
      }
      return { success: true };
    } catch (error) {
      throw classifyProxmoxError(
        `Failed to ${config.intent === "destroy" ? "destroy" : "stop"} Proxmox sandbox`,
        error
      );
    }
  }

  private assertCapacity(vms: ProxmoxVm[]): void {
    if (this.config.maxRunning <= 0) return;
    const running = vms.filter(
      (vm) => !vm.template && vm.name.startsWith(PROXMOX_VM_NAME_PREFIX) && vm.status === "running"
    ).length;
    if (running >= this.config.maxRunning) {
      throw new SandboxProviderError(
        `Proxmox sandbox capacity reached (${running}/${this.config.maxRunning} running)`,
        "transient"
      );
    }
  }

  private async cloneWithFreeVmid(
    vms: ProxmoxVm[],
    name: string,
    config: CreateSandboxConfig
  ): Promise<number> {
    const taken = new Set(vms.map((vm) => vm.vmid));
    let attempts = 0;
    for (let vmid = this.config.vmidMin; vmid <= this.config.vmidMax; vmid++) {
      if (taken.has(vmid) || !(await this.api.isVmidFree(vmid))) continue;
      try {
        await this.api.cloneVm({
          templateVmid: this.config.templateVmid,
          newVmid: vmid,
          name,
          pool: this.config.pool,
          description: `Open-Inspect session ${config.sessionId}, sandbox ${config.sandboxId}`,
        });
        return vmid;
      } catch (error) {
        if (error instanceof ProxmoxApiError && error.isAlreadyExists) {
          if (++attempts >= CLONE_ATTEMPTS) throw error;
          continue;
        }
        throw error;
      }
    }
    throw new SandboxProviderError(
      `No free VMID in ${this.config.vmidMin}-${this.config.vmidMax}`,
      "transient"
    );
  }

  private async destroyIfKnown(vmid: number, signal?: AbortSignal): Promise<void> {
    try {
      await this.api.destroyVm(vmid, signal);
    } catch (error) {
      if (error instanceof ProxmoxApiError && error.isNotFound) return;
      throw error;
    }
  }

  private async destroyQuietly(vmid: number): Promise<void> {
    try {
      const vm = await this.api.getVm(vmid);
      if (vm && vm.status !== "stopped") await this.api.stopVm(vmid);
      if (vm) await this.api.destroyVm(vmid);
    } catch (error) {
      log.warn("proxmox.cleanup_failed", { vmid, error: String(error) });
    }
  }

  private buildEnvVars(config: CreateSandboxConfig): Record<string, string> {
    const envVars = buildSandboxEnvVars(config, {
      scmIdentity: scmCloneIdentity(this.config.scmProvider),
      portPlan: resolveSandboxPortPlan({ codeServer: false, terminal: false, vnc: false }, {}),
      emitDisabledTerminalEnv: true,
    });
    Object.assign(envVars, {
      [DEFERRED_START_ENV_VAR]: "false",
      [IMAGE_BUILD_MODE_ENV_VAR]: "false",
      RESTORED_FROM_SNAPSHOT: "false",
      FROM_REPO_IMAGE: "false",
    });
    return envVars;
  }

  // Must stay before the launcher's backstop (lifetime plus grace, from boot; ec2-infra/launcher.sh).
  private computeLifetime(
    startAtMs: number,
    timeoutSeconds: number | undefined
  ): Extract<SandboxLifetime, { kind: "finite" }> {
    return {
      kind: "finite",
      expiresAtMs: startAtMs + (timeoutSeconds ?? DEFAULT_SANDBOX_TIMEOUT_SECONDS) * MS_PER_SECOND,
      observedAtMs: this.now(),
      source: "conservative_start_bound",
    };
  }
}

function parseVmid(reference: string): number | null {
  return /^\d+$/.test(reference) ? Number(reference) : null;
}

function classifyProxmoxError(message: string, error: unknown): SandboxProviderError {
  if (error instanceof SandboxProviderError) return error;
  if (error instanceof ProxmoxApiError) {
    return new SandboxProviderError(
      `${message}: ${error.message}`,
      error.isRetryable ? "transient" : "permanent",
      error
    );
  }
  return SandboxProviderError.fromFetchError(`${message}: ${String(error)}`, error);
}

export function createProxmoxProvider(
  api: ProxmoxSandboxApi,
  bootstrapStore: SandboxBootstrapStore,
  config: ProxmoxProviderConfig
): ProxmoxSandboxProvider {
  return new ProxmoxSandboxProvider(api, bootstrapStore, config);
}
