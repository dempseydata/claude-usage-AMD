import * as vscode from "vscode";
import * as path from "node:path";
import { locatePython } from "./python-locator";
import { resolveInstallMode, dashboardSpawnArgs, InstallMode } from "./install-mode";
import { resolvePort } from "./port-allocator";
import { ServerManager, OutputSink } from "./server-manager";
import { DashboardSidebar } from "./sidebar";

/**
 * Lifecycle owner for the extension. Held as a module-level singleton so
 * deactivate() can find it.
 */
class Extension {
  private context: vscode.ExtensionContext;
  private output: vscode.OutputChannel;
  private sidebar: DashboardSidebar;
  private server: ServerManager | undefined;
  /**
   * In-flight startup. Subsequent openDashboard() calls await this one
   * instead of spawning a second ServerManager. Cleared on resolve/reject.
   * Prevents the double-click orphaned-process race Codex flagged.
   */
  private startupInFlight: Promise<void> | undefined;

  constructor(context: vscode.ExtensionContext) {
    this.context = context;
    this.output = vscode.window.createOutputChannel("Claude Usage");
    // The sidebar invokes onShow when VS Code reveals the webview — that's
    // when the user clicked the activity-bar icon, so it's the right moment
    // to spawn the server. openDashboard() coalesces repeat calls.
    this.sidebar = new DashboardSidebar(() => {
      void this.openDashboard();
    }, context.extensionUri);

    context.subscriptions.push(
      this.output,
      vscode.window.registerWebviewViewProvider(DashboardSidebar.viewId, this.sidebar),
      vscode.commands.registerCommand("claudeUsage.open", () => this.openDashboard()),
      vscode.commands.registerCommand("claudeUsage.rescan", () => this.rescan()),
      vscode.commands.registerCommand("claudeUsage.restart", () => this.restart()),
      vscode.commands.registerCommand("claudeUsage.showLogs", () => this.output.show()),
    );
  }

  /**
   * Start (or focus) the dashboard. If the server isn't running yet, this
   * resolves Python + install mode + port, spawns the server, then points
   * the sidebar at it.
   */
  async openDashboard(): Promise<void> {
    await vscode.commands.executeCommand("workbench.view.extension.claudeUsageSidebar");

    if (this.server && this.server.status === "ready") {
      this.sidebar.refresh();
      return;
    }
    // Coalesce concurrent calls onto a single in-flight startup so we don't
    // spawn two Python processes and overwrite this.server.
    if (this.startupInFlight) {
      return this.startupInFlight;
    }
    this.startupInFlight = this.doStartup().finally(() => {
      this.startupInFlight = undefined;
    });
    return this.startupInFlight;
  }

  private async doStartup(): Promise<void> {
    const config = vscode.workspace.getConfiguration("claudeUsage");
    const configuredPython = config.get<string>("pythonPath", "");
    const configuredCli = config.get<string>("cliPath", "");
    // Hardcoded to localhost. We previously exposed a `host` setting but
    // 0.0.0.0 would have made the user's usage data visible on the LAN.
    // The Python dashboard accepts HOST/PORT env vars directly if someone
    // really needs to bind elsewhere; that's an out-of-extension config.
    const host = "127.0.0.1";
    const configuredPort = config.get<number>("port", 0);

    const workspaceFolders = (vscode.workspace.workspaceFolders ?? []).map((f) => f.uri.fsPath);
    const extensionDir = this.context.extensionUri.fsPath;
    // Bundled python sources live at <extensionDir>/python/cli.py — copied
    // there from the repo root by scripts/copy-python.js at package time.
    const bundledCliPath = path.join(extensionDir, "python", "cli.py");
    const mode = resolveInstallMode({
      configuredCliPath: configuredCli,
      bundledCliPath,
      extensionDir,
      workspaceFolders,
    });
    if (mode.kind === "none") {
      const msg = noInstallMessage();
      this.output.appendLine(msg);
      this.sidebar.setStatus(msg);
      vscode.window.showErrorMessage(msg);
      return;
    }

    const python = mode.kind === "clone" ? locatePython(configuredPython) : undefined;
    if (mode.kind === "clone" && !python) {
      const msg = noPythonMessage();
      this.output.appendLine(msg);
      this.sidebar.setStatus(msg);
      vscode.window.showErrorMessage(
        "Claude Usage needs Python 3.8+ on PATH. See the dashboard panel for install links.",
      );
      return;
    }

    const port = await resolvePort(configuredPort, host);
    const url = `http://${host}:${port}/`;
    // Probe a dashboard-specific endpoint so we don't get fooled by some
    // other localhost service listening on the same port.
    const probeUrl = `http://${host}:${port}/api/data`;
    // --no-browser: the dashboard is embedded in the webview, so the bundled
    // cli.py must not also pop a system browser (it does by default for CLI users).
    // --surface vscode: tells the dashboard it's embedded so its footer shows the
    // version only — no "get the extension" promo (we're already in it) and no
    // GitHub update check (VS Code updates the extension itself).
    const spawnArgs = dashboardSpawnArgs(mode, python, ["--no-browser", "--host", host, "--port", String(port), "--surface", "vscode"]);
    if (!spawnArgs) {
      const msg = "Could not assemble a valid command to spawn the dashboard.";
      this.output.appendLine(msg);
      this.sidebar.setStatus(msg);
      return;
    }

    this.sidebar.setStatus(`Starting dashboard at ${url}…`);
    this.output.appendLine(`[ext] install mode: ${describeMode(mode)}`);
    // Capture the manager into a local so the catch block can't dispose
    // a *different* manager that was created by a concurrent call.
    const manager = new ServerManager({
      command: spawnArgs.command,
      args: spawnArgs.args,
      url: probeUrl,
      output: this.toSink(),
    });
    this.server = manager;
    try {
      await manager.start();
      this.sidebar.setUrl(url);
    } catch (err) {
      const msg = `Failed to start dashboard: ${(err as Error).message}`;
      this.output.appendLine(msg);
      this.sidebar.setStatus(msg);
      vscode.window.showErrorMessage(msg);
      manager.dispose();
      if (this.server === manager) this.server = undefined;
    }
  }

  /**
   * Trigger a rescan against the running server, then refresh the iframe.
   * Currently just refreshes — the existing Python dashboard has a Rescan
   * button inside the UI; this is a placeholder for future host-driven
   * rescan if we add a POST endpoint dedicated to it.
   */
  rescan(): void {
    this.sidebar.refresh();
  }

  async restart(): Promise<void> {
    // If a startup is in flight, wait for it to settle so we don't dispose a
    // manager mid-spawn and leave an orphaned Python process.
    if (this.startupInFlight) {
      try { await this.startupInFlight; } catch { /* ignored — about to restart */ }
    }
    if (this.server) {
      this.server.dispose();
      this.server = undefined;
    }
    this.sidebar.setUrl(null);
    await this.openDashboard();
  }

  dispose(): void {
    if (this.server) {
      this.server.dispose();
      this.server = undefined;
    }
  }

  private toSink(): OutputSink {
    return { appendLine: (line) => this.output.appendLine(line) };
  }
}

function describeMode(mode: InstallMode): string {
  if (mode.kind === "brew") return `brew (${mode.binary})`;
  if (mode.kind === "clone") return `clone (${mode.cliPy})`;
  return "none";
}

/**
 * Friendly "no install found" message. With the bundled Python sources this
 * should be virtually unreachable in a packaged extension — only fires when
 * the user explicitly sets claudeUsage.cliPath to a path that doesn't exist
 * AND no PATH/workspace/sibling fallback succeeds.
 */
export function noInstallMessage(): string {
  return [
    "Could not find a claude-usage install. This is unexpected for a marketplace install.",
    "Check your claudeUsage.cliPath setting (clear it to fall back to the bundled sources),",
    "and use Claude Usage: Show Logs to see what was tried.",
  ].join("\n");
}

/**
 * Friendly "no Python found" message. This is the most likely failure for
 * a fresh marketplace install on a machine without Python — common on
 * Windows. Points the user at python.org with concrete next steps.
 */
export function noPythonMessage(platform: NodeJS.Platform = process.platform): string {
  const installHint =
    platform === "win32"
      ? "Install Python 3.8+ from https://www.python.org/downloads/windows/ (make sure to check 'Add Python to PATH' during install)."
      : platform === "darwin"
      ? "Install Python 3.8+ with: brew install python  (or from https://www.python.org/downloads/macos/)."
      : "Install Python 3.8+ via your distro's package manager (e.g. apt install python3).";
  return [
    "Claude Usage needs Python 3.8 or newer on your PATH.",
    "",
    installHint,
    "",
    "After installing, reload this VS Code window (Cmd/Ctrl+Shift+P → Developer: Reload Window).",
    "If Python is already installed in a non-standard location, set claudeUsage.pythonPath in settings.",
  ].join("\n");
}

let extension: Extension | undefined;

export function activate(context: vscode.ExtensionContext): void {
  extension = new Extension(context);
}

export function deactivate(): void {
  extension?.dispose();
  extension = undefined;
}
