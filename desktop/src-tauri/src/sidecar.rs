//! Sidecar management for the Cato Python daemon.
//!
//! Spawns `cato start --channel webchat` as a child process and monitors its health.
//! Gracefully shuts down on app exit.

use std::process::Stdio;
use tokio::process::{Child, Command};
use tokio::time::{sleep, Duration};

/// Manages the Cato daemon sidecar process.
pub struct SidecarManager {
    child: Option<Child>,
    http_port: u16,
    ws_port: u16,
}

impl SidecarManager {
    pub fn new(http_port: u16, ws_port: u16) -> Self {
        Self {
            child: None,
            http_port,
            ws_port,
        }
    }

    pub fn http_port(&self) -> u16 {
        self.http_port
    }

    pub fn ws_port(&self) -> u16 {
        self.ws_port
    }

    /// Check if the daemon process is actually still running.
    /// Uses try_wait() to detect crashed processes.
    pub async fn is_running(&mut self) -> bool {
        if let Some(ref mut child) = self.child {
            match child.try_wait() {
                Ok(Some(_status)) => {
                    // Process has exited — clean up
                    self.child = None;
                    false
                }
                Ok(None) => true,   // Still running
                Err(_) => {
                    self.child = None;
                    false
                }
            }
        } else {
            false
        }
    }

    /// Start the Cato daemon as a child process.
    ///
    /// Tries the bundled sidecar binary first, falls back to system `cato`.
    pub async fn start(&mut self) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        // Check if already running (handle crashed state)
        if self.is_running().await {
            return Ok(());
        }

        let cato_bin = Self::find_cato_binary();

        log::info!("Starting Cato daemon: {} start --channel webchat", cato_bin);

        let child = Command::new(&cato_bin)
            .args(["start", "--channel", "webchat"])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true)
            .spawn()
            .map_err(|e| {
                format!("Failed to spawn Cato daemon ({}): {}", cato_bin, e)
            })?;

        self.child = Some(child);

        // Wait for the daemon to become healthy (up to 15 seconds)
        self.wait_for_health(15).await?;

        log::info!("Cato daemon is healthy on port {}", self.http_port);
        Ok(())
    }

    /// Stop the Cato daemon gracefully.
    pub async fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            log::info!("Stopping Cato daemon...");

            // Try graceful shutdown via `cato stop`
            let cato_bin = Self::find_cato_binary();
            let _ = Command::new(&cato_bin)
                .args(["stop"])
                .output()
                .await;

            // Wait up to 5 seconds for the process to exit
            let timeout = sleep(Duration::from_secs(5));
            tokio::pin!(timeout);

            tokio::select! {
                _ = child.wait() => {
                    log::info!("Cato daemon stopped gracefully");
                }
                _ = &mut timeout => {
                    log::warn!("Cato daemon did not stop in time, killing...");
                    let _ = child.kill().await;
                }
            }
        }
    }

    /// Poll the health endpoint until the daemon is ready.
    async fn wait_for_health(
        &self,
        timeout_secs: u64,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let url = format!("http://127.0.0.1:{}/health", self.http_port);
        let client = reqwest::Client::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(timeout_secs);

        loop {
            if tokio::time::Instant::now() >= deadline {
                return Err("Cato daemon health check timed out".into());
            }

            match client.get(&url).timeout(Duration::from_secs(2)).send().await {
                Ok(resp) if resp.status().is_success() => return Ok(()),
                _ => {}
            }

            sleep(Duration::from_millis(500)).await;
        }
    }

    /// Find the cato binary — bundled sidecar → known install paths → PATH (dev only).
    ///
    /// Security: avoids resolving bare "cato" from CWD on Windows (PATH hijacking).
    /// Checks absolute locations before falling back to PATH resolution.
    fn find_cato_binary() -> String {
        // 1. Bundled alongside the app binary
        if let Ok(exe) = std::env::current_exe() {
            let sidecar = exe.parent().unwrap_or(exe.as_path()).join("cato");
            if sidecar.exists() {
                return sidecar.to_string_lossy().to_string();
            }
        }

        // 2. Known absolute install locations (Windows editable pip install)
        let known_paths = [
            r"C:\Users\Administrator\.local\bin\cato.exe",
            r"C:\Users\Administrator\AppData\Local\Programs\Python\Python312\Scripts\cato.exe",
            r"C:\Program Files\Python312\Scripts\cato.exe",
        ];
        for path in &known_paths {
            if std::path::Path::new(path).exists() {
                return path.to_string();
            }
        }

        // 3. Resolve via PATH using `where` (Windows) to get the full absolute path.
        //    This is safer than passing a bare name to Command::new which would
        //    search CWD first on some Windows configurations.
        if let Ok(output) = std::process::Command::new("where").arg("cato").output() {
            if let Ok(stdout) = std::str::from_utf8(&output.stdout) {
                if let Some(line) = stdout.lines().next() {
                    let abs_path = line.trim().to_string();
                    if !abs_path.is_empty() {
                        log::info!("Resolved cato via PATH: {}", abs_path);
                        return abs_path;
                    }
                }
            }
        }

        // Final fallback — development only
        log::warn!("cato binary not found at known locations; falling back to bare 'cato'");
        "cato".to_string()
    }
}

impl Drop for SidecarManager {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _: Result<(), std::io::Error> = child.start_kill();
        }
    }
}
