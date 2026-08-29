// ringleader: archive a session marked `finish on exit` when pi quits.
// Installed into ~/.pi/agent/extensions/ by `rl install hooks`.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";

export default function (pi: ExtensionAPI) {
  pi.on("session_shutdown", async (event, ctx) => {
    if (event.reason !== "quit") return;
    // ctx.sessionManager.getSessionId(): string — confirmed against pi's extension
    // types (ReadonlySessionManager) and the same call used in docs/extensions.md
    // to tag outbound request headers.
    const id = ctx.sessionManager.getSessionId();
    if (!id) return;
    await new Promise((resolve) => {
      const child = spawn("rl", ["session", "sweep", "--session", String(id)], {
        stdio: "ignore",
        detached: false,
      });
      child.on("error", () => resolve(null));
      child.on("close", () => resolve(null));
    });
  });
}
