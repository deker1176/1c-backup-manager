# 1C Backup Manager v4.8

Windows backup manager for **1C:Enterprise 8.x**, **Microsoft SQL Server**, and **files/directories**.

**Russian documentation:** [README_RU.md](README_RU.md) · **Contact:** [Telegram @dek76](https://t.me/dek76)

> **Release status:** This is the source code distribution. Windows binaries are built by the GitHub Actions workflow in this repository. A released EXE is a build artifact, not evidence of a successful production backup: test each job and verify that it can be restored.

## Features

- 1C `.dt` backups with session restriction, waiting for users, optional session termination, and release of restrictions after the run.
- MS SQL Server backup (`.bak`) and file/folder backup jobs.
- Optional ZIP / 7z compression, local retention, FTP / FTPS upload.
- Windows service scheduler and desktop GUI with a system tray icon.
- Scheduled jobs and on-demand runs; status and logs.
- Notifications via SMTP, Telegram Bot API, and MAX Bot API.
- Import infobases from `ibases.v8i`; Russian/English GUI.
- Optional 1C 8.x test/repair and external `.epf` processing jobs. **Not for 1C 7.7.**

## Download for Windows

Open **Releases** and download `1CBackupManager.exe` from a published release. If there is not yet a release, open **Actions → Build Windows EXE**, select the latest successful run, and download its artifact.

The binary is produced on a Windows GitHub runner with PyInstaller; **do not try to run a Linux-built binary on Windows**.

## Building locally

1. Install Python 3.11–3.13 for Windows.
2. Run `build_windows.bat` (or `build_windows_folder.bat`).
3. Find the executable at `dist\1CBackupManager.exe`.
4. Back up your existing `config.json`, `secrets.dat`, `scheduler_state.json`, old EXE, and stop `OneCBackupManager` before updating.
5. Replace **only** the EXE, then check the Windows service account and perform a controlled test of every configured job.

Secrets use **Windows DPAPI**, so the service must run under the same Windows identity used to save credentials.

## GitHub Actions

The [Build Windows EXE](.github/workflows/build-windows.yml) workflow:

- runs the source tests on Linux with a virtual display;
- builds `1CBackupManager.exe` on `windows-latest`;
- calculates SHA-256 of the binary;
- saves `1CBackupManager-windows` as an Actions artifact on each push to `main`;
- publishes binaries to **Releases** when triggered by tag `v*` or manually with **Publish GitHub Release = true**.

To publish v4.8.0 manually: **Actions → Build Windows EXE → Run workflow → Publish GitHub Release → Version tag `v4.8.0`**. The release will be published only after the checks and Windows build succeed. GitHub Actions needs permission to write repository contents for creating Releases.

## Security

Never commit live `config.json`, `secrets.dat`, `.dt`, `.bak`, tokens, credentials, logs, or customer databases. These are ignored by `.gitignore`. `config.example.json` contains only blank/example values. Keep the repository **private** unless you've audited the code and explicitly want to open source it.

The `README_RU.md` contains more detailed 1C-specific caveats, including backup/repair workflows and limitations. Backup file size and checksum checks do not replace a real restore test.
