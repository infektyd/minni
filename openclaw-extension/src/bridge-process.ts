/**
 * bridge-process.ts — Process supervisor for sovrd daemon
 * Spawns, monitors, and manages the Python HTTP daemon lifecycle.
 */

import { spawn, ChildProcess } from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

const SOCKET_PATH = "/tmp/sovereign.sock";
const PYTHON_BIN = `${os.homedir()}/.openclaw/sovereign-memory-v3.1/venv/bin/python`;
const SOVRD_PATH = path.join(__dirname, "..", "sovrd.py");

export interface ProcessStatus {
  running: boolean;
  pid?: number;
  socketExists: boolean;
}

let childProcess: ChildProcess | null = null;
let isStarting = false;

/**
 * Check current process status.
 */
export function getStatus(): ProcessStatus {
  const running = childProcess !== null && !childProcess.killed && childProcess.exitCode === null;
  return {
    running,
    pid: childProcess?.pid,
    socketExists: fs.existsSync(SOCKET_PATH),
  };
}

/**
 * Start the sovrd daemon.
 */
export function start(): Promise<ProcessStatus> {
  return new Promise((resolve, reject) => {
    if (isStarting) {
      reject(new Error("Already starting"));
      return;
    }

    // Clean up existing socket
    if (fs.existsSync(SOCKET_PATH)) {
      try {
        fs.unlinkSync(SOCKET_PATH);
      } catch {
        // Ignore if can't remove
      }
    }

    // Check if already running
    if (childProcess && !childProcess.killed) {
      resolve(getStatus());
      return;
    }

    isStarting = true;

    childProcess = spawn(PYTHON_BIN, [SOVRD_PATH], {
      detached: false,
      stdio: ["ignore", "pipe", "pipe"],
    });

    childProcess.stdout?.on("data", (data) => {
      console.log(`[sovrd] ${data.toString().trim()}`);
    });

    childProcess.stderr?.on("data", (data) => {
      console.error(`[sovrd:err] ${data.toString().trim()}`);
    });

    childProcess.on("error", (err) => {
      isStarting = false;
      reject(err);
    });

    childProcess.on("exit", (code, signal) => {
      console.log(`[sovrd] exited with code ${code}, signal ${signal}`);
      childProcess = null;
      isStarting = false;
    });

    // Wait for socket to appear (poll with timeout)
    const timeout = 10000;
    const interval = 200;
    let elapsed = 0;

    const checkSocket = () => {
      if (fs.existsSync(SOCKET_PATH)) {
        isStarting = false;
        resolve(getStatus());
        return;
      }

      if (elapsed >= timeout) {
        isStarting = false;
        reject(new Error("Timeout waiting for socket"));
        return;
      }

      elapsed += interval;
      setTimeout(checkSocket, interval);
    };

    // Give process a moment to start
    setTimeout(checkSocket, 500);
  });
}

/**
 * Stop the sovrd daemon.
 */
export function stop(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (!childProcess || childProcess.killed) {
      // Clean up socket file
      if (fs.existsSync(SOCKET_PATH)) {
        try {
          fs.unlinkSync(SOCKET_PATH);
        } catch {
          // Ignore
        }
      }
      resolve();
      return;
    }

    const timeout = setTimeout(() => {
      childProcess?.kill("SIGKILL");
      reject(new Error("Force killed after timeout"));
    }, 5000);

    childProcess.once("exit", () => {
      clearTimeout(timeout);
      // Clean up socket file
      if (fs.existsSync(SOCKET_PATH)) {
        try {
          fs.unlinkSync(SOCKET_PATH);
        } catch {
          // Ignore
        }
      }
      resolve();
    });

    childProcess.kill("SIGTERM");
  });
}

/**
 * Restart the daemon.
 */
export async function restart(): Promise<ProcessStatus> {
  await stop();
  return start();
}

/**
 * Ensure the daemon is running (start if not).
 */
export async function ensureRunning(): Promise<ProcessStatus> {
  const status = getStatus();
  if (status.running && status.socketExists) {
    return status;
  }
  return start();
}

// If run directly, start the daemon
if (require.main === module) {
  (async () => {
    try {
      console.log("Starting sovrd daemon...");
      const status = await start();
      console.log("Daemon status:", JSON.stringify(status, null, 2));
      console.log("Press Ctrl+C to stop.");

      // Keep process alive
      process.on("SIGINT", async () => {
        console.log("\nShutting down...");
        await stop();
        process.exit(0);
      });
    } catch (err) {
      console.error("Failed to start daemon:", err);
      process.exit(1);
    }
  })();
}
