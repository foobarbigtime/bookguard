# Getting started on Unraid

BookGuard runs beside an existing Bindery installation. Begin with its default
audit and preview settings; enable write workflows only when you need them.

## Prerequisites

- Git and Docker with Docker Compose on the host.
- Bindery's database and library folders, readable by the BookGuard container.
- Dedicated writable folders for BookGuard's database and quarantine.
- A password for BookGuard and a choice of localhost or LAN access.

The supplied container runs as Unraid's `nobody:users` (`99:100`). If you choose
another UID/GID, configure `BOOKGUARD_UID` and `BOOKGUARD_GID` and give that
identity access to the mounted folders. The container does not bypass host
filesystem permissions.

## 1. Get the source

```bash
cd /mnt/cache/appdata
git clone https://github.com/foobarbigtime/bookguard.git
cd bookguard
cp .env.example .env
```

Keep the Git checkout separate from BookGuard's writable configuration folder.

## 2. Configure access and paths

Edit `.env` before starting. Set a long password and, for access from another
LAN device, replace the example address with your Unraid server's address:

```env
BOOKGUARD_AUTH_USERNAME=bookguard
BOOKGUARD_AUTH_PASSWORD=replace-with-a-long-random-password
BOOKGUARD_BIND_ADDRESS=192.168.1.10
BOOKGUARD_UID=99
BOOKGUARD_GID=100
BOOKGUARD_CONFIG_HOST_PATH=/mnt/cache/appdata/bookguard-config
```

Leave `BOOKGUARD_BIND_ADDRESS=127.0.0.1` for host-local access. Use HTTPS through
a reverse proxy or a VPN for remote access; HTTP Basic authentication does not
encrypt traffic. See [access controls](safety.md#application-access).

Review [`compose.yaml`](../compose.yaml) and adjust its host-side paths to match
your installation. Its defaults are:

| Host path | Container path | Access |
|---|---|---|
| `/mnt/cache/appdata/bookguard-config` | `/config` | Writable BookGuard state |
| `/mnt/cache/appdata/bindery` | `/bindery` | Read-only Bindery database directory |
| `/mnt/user/data/media/audiobooks` | `/audiobooks` | Read-only library |
| `/mnt/user/data/media/books` | `/books` | Read-only library |
| `/mnt/user/data/bookguard-quarantine` | `/quarantine` | Writable quarantine |

Create the writable directories and make them writable by the selected UID/GID.
Keep the ordinary library and Bindery mounts read-only. The default database
path inside the container is `/bindery/bindery.db`; verify the actual filename
and set `BINDERY_DB` if needed. Set `BINDERY_URL` for API-backed actions when
you enable them.

Bindery's stored paths and BookGuard's mounted paths may differ. The defaults
translate `/data/media/books` to `/books` and `/data/media/audiobooks` to
`/audiobooks`. Set `EBOOK_BINDERY_PREFIX` and `AUDIOBOOK_BINDERY_PREFIX` if your
Bindery paths differ. The roots and prefixes are also visible in Settings.

## 3. Build and start

For the base deployment:

```bash
bash scripts/build-with-provenance.sh
docker compose up -d --no-build
```

The build helper records the application version, exact Git revision, and
source repository on the image. It refuses tracked uncommitted changes. Use
the helper for rebuilds, followed by `--no-build` when deploying the image.

The base deployment does not enable ClamAV. To install the private scanner
overlay instead, follow [ClamAV configuration](configuration.md). It streams
bytes to the scanner and does not mount your library into the scanner.

## 4. Check health and run the first scan

Open `http://<unraid-ip>:8788` and sign in with the configured credentials.
Check **System** for unreadable paths or missing dependencies, then run
**Scan library** from Home. Open **Review** when the scan finishes.

A library scan and content verification are separate operations. Verify an
undecided item to gather stronger identity and safety evidence. Scans and
verification record BookGuard state but do not alter library files or Bindery
registrations.

See the [user guide](user-guide.md) for review actions. Before using the
[guarded upgrade command](operations.md#updating), explicitly opt into the
[ClamAV topology](configuration.md) and check scanner health. The helper always
deploys the ClamAV overlay and enables malware scanning; it has no base-only
mode and will change a base-only installation's topology. Allow for the
scanner's resource use and signature initialization: verification fails closed
while the scanner is unavailable.
