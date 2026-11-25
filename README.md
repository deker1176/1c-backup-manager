# 1C Backup Manager v5.5.0

Windows backup manager for **1C:Enterprise 8.x**, **Microsoft SQL Server**, and **files/directories**.

**Russian documentation:** [README_RU.md](README_RU.md) · **Contact:** [Telegram @dek76](https://t.me/dek76)

> **Release status:** This is the source code distribution. Windows binaries are built by the GitHub Actions workflow in this repository. A released EXE is a build artifact, not evidence of a successful production backup: test each job and verify that it can be restored.

## v5.4.1: verified IIS backup run (restore still pending)

[Latest release v5.5.0](https://github.com/deker1176/1c-backup-manager/releases/tag/v5.4.1) · [Download Windows EXE](https://github.com/deker1176/1c-backup-manager/releases/download/v5.4.1/1CBackupManager.exe)

On November 23, 2026, a real Windows file-based 1C infobase (C:\Base\unf) was backed up through an IIS publication sharing DefaultAppPool. The manager detected the publication, registered emergency restoration, confirmed the pool was Stopped in 45 seconds, copied the infobase while holding an exclusive file handle, restarted IIS **before** DumpIB, and produced a .dt with SHA-256 and journal (result: ok). The user confirmed the web service was working again afterward.

**Not yet verified:** restoration of this .dt into a separate empty 1C infobase. File size, SHA-256, and a successful DumpIB do not substitute for a restore test. Read the detailed [Russian field-test report and isolated restore checklist](README_RU.md#проверка-реального-резервного-копирования--23112026-v541). This run does not validate Apache, SMB, RAS, or other deployments.

Shared IIS pool or Apache service shutdown must be enabled explicitly, and temporarily interrupts all hosted sites. Unsaved client operations can be lost.

## v5.5.0: queue, archive catalog, isolated restore

[Windows x64 release v5.5.0](https://github.com/deker1176/1c-backup-manager/releases/tag/v5.5.0) · [Download EXE](https://github.com/deker1176/1c-backup-manager/releases/download/v5.5.0/1CBackupManager.exe)

Manual backup requests now queue in the GUI, while the Windows service executes same-time scheduled jobs serially and waits for the cross-process lock when necessary. The **Backups** tab scans each job's actual destination (including accessible UNC shares), shows full paths, and allows SHA-256 verification. For .dt it can create a **new empty test file infobase** using the documented 1C CREATEINFOBASE /UseTemplate syntax; production infobases are never chosen as restore targets. For detailed setup and limits see [README_RU.md](README_RU.md).

**Note:** daily schedules require the Windows service to be running. Backup/restore jobs are serialized, not truly parallel. Test a restored infobase before relying on backups.

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

To publish a future release manually: **Actions → Build Windows EXE → Run workflow → Publish GitHub Release**, and choose the new version tag. The release will be published only after the checks and Windows build succeed. GitHub Actions needs permission to write repository contents for creating Releases.

## Security

Never commit live `config.json`, `secrets.dat`, `.dt`, `.bak`, tokens, credentials, logs, or customer databases. These are ignored by `.gitignore`. `config.example.json` contains only blank/example values. Keep the repository **private** unless you've audited the code and explicitly want to open source it.

The `README_RU.md` contains more detailed 1C-specific caveats, including backup/repair workflows and limitations. Backup file size and checksum checks do not replace a real restore test.
