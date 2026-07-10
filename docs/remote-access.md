# Remote Access Policy

## Supported setup

Install Tailscale on the Mac mini, Windows workstation, and iPhone under the same MFA-protected account. Approve only personal devices. Tailscale is the private network boundary; do not open public ports on the router or Mac.

Use RustDesk over that private network for graphical desktop control from Windows. On the Mac, grant the RustDesk host only the macOS Screen Recording and Accessibility permissions it needs. Configure unattended access with a strong unique password stored in a password manager, not in this repository. Never use it to operate production trading services.

Enable SSH on the Mac only for the development account and reach it only through Tailscale. Use SSH keys, disable password login when configured, and do not expose SSH or VNC directly to the internet. Do not open public ports for SSH, VNC, RustDesk, or the application.

## Mac resilience

Enable FileVault, disable automatic login, apply macOS updates, and configure power settings so the Mac is reachable after normal idle periods. Keep a local display and keyboard available for first-time permissions, OS updates, and recovery after a restart.

## Phone use

Use iPhone remote access for emergencies: inspect status, reconnect Tailscale, or use an SSH client for a small corrective command. Do not use a phone for extended coding, bulk editing, handling secrets, or approving live-trading actions.

## Incident response

If a device is lost or access is suspicious, remove the device from Tailscale, revoke remote-desktop credentials, revoke GitHub sessions if appropriate, and rotate any development-only secrets stored on that device.
