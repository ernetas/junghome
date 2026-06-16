# JUNG HOME Gateway — System Analysis

Analysis of the root partition microSD image (`disk_dump/jung/sdc2/`).

**Platform:** Raspberry Pi Zero (armhf), Raspberry Pi OS Debian 13 Trixie
**Firmware:** v2.1.3 Release (build 2840), built 2026-04-28 from GitLab CI (`lb-connect-gateway/lbc-gw-integration`)
**Device serial:** `0000000084fb4b1b` · **MAC:** `00:22:d1:05:96:02`
**mDNS hostname:** `junghome-0022d1059602.local`

---

## Software Stack Architecture

The gateway is a multi-process Node.js stack orchestrated by a single C binary (`board_ctrl`). Nine GitLab CI projects assemble into this image:

| CI Project | Version | What it is |
|---|---|---|
| `lbc-gw-integration` | 2.1.3 | Top-level build orchestrator |
| `lbc-gw-middleware` | 2.1.3 | BT Mesh Node.js app |
| `lbc-gw-api-server` | 2.1.1 | REST API + WebSocket + Web UI |
| `lbc-gw-jungremote-client` | 2.1.0 | JUNG Cloud proxy (Socket.io) |
| `lbc-gw-bt-tunnel` | 1.2.21.1 | UART↔NCP bridge (C binary) |
| `lbc-gw-nginx` | 2.1.0 | nginx + WAF + SSL gen scripts |
| `lbc-gw-node` | v22.22.0 | Node.js runtime (not from apt) |
| `lbc-gw-system` | 2.1.0 | Board controller binary |
| `lbc-gw-rootfs` | RASPIOS_13_PI32 | Base OS customization |

---

## /opt — Custom Application Directory

### board_ctrl

`/opt/board_ctrl/board_ctrl` — 192 KB stripped PIE ARM ELF. The *only* systemd service (`lbc-gw.service`). Runs as root with OOM score -999. This is the system orchestrator — it spawns and manages the Node.js processes. Restarts after 90 s on failure.

### middleware

`/opt/middleware/` — Node.js TypeScript app (`package: "bluetooth"` v1.0.0, author: Marius Biller). Handles all Bluetooth Mesh logic and talks to the Silicon Labs NCP co-processor via UART/bt_tunnel. State lives in `/data/middleware/res/`: `bt_mesh_project.json`, `cdb_*.json` (groups/scenes/functions), `btmesh_iv_index`, `btmesh_sequence_number`. Schema version 6 with migration framework and 6 numbered rollback backups.

### api-server

`/opt/api-server/` — Node.js TypeScript app (`package: "src"` v1.5.0). Express 5.2.1 + ws 8.19.0. Exposes the REST API on `:3000` and WebSocket on `:8080` (proxied by nginx). Has 14 controllers (devices, groups, scenes, functions, products, users, health, config, versions, remote, logs). Also serves a full Bootstrap-based Web UI with dashboards, device views, and settings pages.

### jungremote-client

`/opt/jungremote-client/` — Node.js TypeScript app (`package: "lbc-gw-jungremote-client"` v1.0.0). Socket.io-client 2.4.0 connecting to the JUNG Cloud. Acts as a transparent proxy, forwarding cloud commands to local subsystems. Bundles a JUNG CA certificate (`jung-ca-cer2034.crt`) for validating cloud TLS. Git origin: `https://git.jung.de/lb-connect-gateway/lbc-gw-jungremote-client.git`

### bt_tunnel

`/opt/bt_tunnel/lbc-gw-bt-tunnel_pi-zero` — 145 KB ARM ELF *with debug symbols* (not stripped). Bridges BT Mesh BGAPI frames between the Pi UART and the Silicon Labs co-processor. Built against Mesh SDK v4.4.6.0, cross-compiled with gcc-8.3.0. Version 1.2.21.1 (build was marked DIRTY — uncommitted changes at release time).

### wireless_module

`/opt/wireless_module/` — Silicon Labs `.gbl` firmware images for the BLE co-processor (full image, bootloader, secure element upgrade). Version 1.2.15.4 / Mesh SDK 4.4.6.0.

### tools

`/opt/tools/` — 7 bash scripts:

| Script | Purpose |
|---|---|
| `gpio_init.sh` | Initialises LEDs on GPIO 4 (LAN/act), 17 (BT), 27 (Cloud) |
| `led.sh` | LED state machine with `flock` serialisation |
| `firewall.sh` | iptables rules (runtime only, no persisted rules file) |
| `static_mac.sh` | Derives stable MAC from Pi serial to work around RTL8152 USB-Ethernet bug (LBGW-279); sets mDNS hostname |
| `dns_test.sh` | DNS connectivity test with flock |
| `serial.sh` | Reads Pi serial from `/proc/cpuinfo` |
| `timezone_validator.sh` | Validates timezone against `/usr/share/zoneinfo` |

### matter-interface

`/opt/matter-interface/` — symlink `res → /data/matter-interface/res`. Currently empty — Matter integration placeholder, not yet populated.

---

## Systemd Services

Only two custom services; everything else is stock Debian.

### lbc-gw.service

```ini
[Unit]
Description=System Service
After=network.target
RequiresMountsFor=/data

[Service]
Type=simple
WorkingDirectory=/opt/board_ctrl
User=root
OOMScoreAdjust=-999
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=lbc-gw.service
KillMode=mixed
KillSignal=SIGTERM
SendSIGKILL=yes
TimeoutStopSec=10
ExecStart=/opt/board_ctrl/board_ctrl
Restart=always
RestartSec=90

[Install]
WantedBy=multi-user.target
```

### nginx.service (customised)

Pre-exec hooks run before nginx starts:

1. `/etc/nginx/generate_ssl_key.sh` — generates a 2048-bit RSA self-signed cert (CN=`junghome.local`, O=Albrecht Jung GmbH & Co. KG, Schalksmuehle NRW DE) valid for 35 years, stored in `/data/etc/nginx/`. Also generates `dh2048.pem` and a fingerprint file.
2. `/etc/nginx/generate_nginx_sites.sh` — reads `/tmp/ip` and `/tmp/mac` at runtime, generates the HTTPS server block dynamically and writes it to `/tmp/nginx/sites-available/https`.

### Masked services (explicitly disabled)

`dhcpcd`, `fake-hwclock`, `networking`, `nfs-*`, `systemd-timesyncd`, `systemd-tmpfiles-clean`, `raspi-config`, `rpi-display-backlight`, `rpi-eeprom-update`, `sshswitch`, `plymouth-*`

### Enabled services

`console-setup`, `cron`, `exim4`, `lbc-gw` (custom), `nfs-client.target`, `remote-fs.target`, `rng-tools-debian`, `rsyslog`, `wpa_supplicant`

### Drop-in overrides

| Override | Effect |
|---|---|
| `dhcpcd.service.d/wait.conf` | Runs dhcpcd with `-q -w` (quiet, wait for address) |
| `getty@tty1.service.d/noclear.conf` | `TTYVTDisallocate=no` — keeps console output after logout |
| `rc-local.service.d/ttyoutput.conf` | Redirects rc.local stdout to TTY |

---

## Nginx — Reverse Proxy & WAF

### Proxy topology

```
Internet/LAN → :443 (nginx TLS) → :3000 (api-server HTTP)
                                 → :8080 (api-server WebSocket, path /ws)
              :80 → 307 redirect → :443
```

### TLS

- Protocols: TLSv1.2 and TLSv1.3 only
- Server ciphers preferred; RC4, SHA1, 3DES, NULL, CAMELLIA, RSA key exchange excluded
- Session tickets disabled
- DH parameters: `/data/etc/nginx/dh2048.pem`
- Certificates: `/data/etc/nginx/svs.crt` / `svs.key`
- HSTS: 1 year + `includeSubDomains`

### Certificate details

| Field | Value |
|---|---|
| CN | `junghome.local` |
| O | Albrecht Jung GmbH & Co. KG |
| OU | Software Engineering |
| L | Schalksmuehle |
| ST | NRW Germany |
| C | DE |
| Validity | 35 years (12,775 days) |

### Security snippets (`/etc/nginx/snippets/`)

| File | Purpose |
|---|---|
| `6G.conf` | WAF: SQLi, XSS, path traversal, bad UAs (nikto, sqlmap), blocks DELETE/PUT/TRACE/DEBUG/CONNECT |
| `secrules.conf` | Blocks `.pl/.cgi/.py/.sh/.lua` execution, `base64_encode`, `display_errors` |
| `header.conf` | HSTS (1 yr), X-Frame-Options: SAMEORIGIN, X-Content-Type-Options: nosniff, X-XSS-Protection |
| `http-dns-rebind-protection.conf` | Default server returns 444 for unrecognised Host headers |
| `rewrite_secure_cookies.conf` | Adds `Secure; HttpOnly; SameSite=strict` to all Set-Cookie headers |
| `cors.conf` | CORS origin: device IP or `junghome-{MAC}.local` only |
| `sse.conf` | Server-Sent Events support |
| `error_page_503.conf` | Booting page (5 s auto-refresh) |
| `error_page_505.conf` | Mode-switching page (9 s auto-refresh) |

---

## Network & Discovery

| Setting | Value |
|---|---|
| Interface | eth0, DHCP by default |
| Rescue static IP | `192.168.178.115/24`, gw `192.168.178.1` |
| IPv6 | Disabled kernel-wide (`net.ipv6.conf.all.disable_ipv6=1`) |
| mDNS | Avahi, eth0 + IPv4 only |
| mDNS hostname | `junghome-0022d1059602.local` |
| Advertised services | `_junghome._tcp:443` and `_workstation._tcp:443` with TXT: version, serial, mac, manufacturer=JUNG |
| Firewall | No persistent rules; iptables managed at runtime by `firewall.sh` |
| NetworkManager | Not installed |

`/etc/network/interfaces` is a symlink to `/data/etc/network/interfaces` — the persistent partition owns network config.

---

## Hardware Configuration (`/boot/config.txt`)

| Setting | Value |
|---|---|
| Bluetooth | **Disabled** (`dtoverlay=disable-bt`) |
| WiFi | **Disabled** (`dtoverlay=disable-wifi`) |
| UART | **Enabled** (`enable_uart=1`) — used for NCP co-processor |
| Audio | Disabled (`dtparam=audio=off`) |
| GPU memory | 4 MB (`gpu_mem=4`) |
| SD overclock | 50 MHz → 100 MHz (`dtoverlay=sdtweak,overclock_50=100`) |
| LEDs | GPIO 4 (LAN/act trigger), 17 (BT), 27 (Cloud) — outputs, low on boot |
| I2C / SPI / I2S | All commented out / disabled |

Boot cmdline: root on `/dev/mmcblk0p2`, ext4, `elevator=deadline`, Plymouth disabled, loglevel=3.

BT Mesh is entirely offloaded to the Silicon Labs co-processor via UART — the Pi's own BT radio is disabled.

---

## Users & Authentication

| Account | UID | Shell | Password |
|---|---|---|---|
| `root` | 0 | `/bin/bash` | Set (yescrypt) |
| `service` | 999 | `/usr/sbin/nologin` | Locked (`!`) |
| `www-data` | 33 | `/usr/sbin/nologin` | Locked |
| `avahi` | 108 | `/usr/sbin/nologin` | Locked |

- Root SSH login: **enabled** (`PermitRootLogin yes`)
- No `authorized_keys` configured for any account
- SSH host keys dated Dec 2020 (pre-date this firmware build — generated on first boot)
- `PasswordAuthentication` defaults to `yes` (not explicitly set)
- `/home/` is empty; no user home directories

Custom profile scripts:
- `/etc/profile.d/jung.sh` — colour PS1 with firmware version, extended history (99,999 entries with timestamps)
- `/etc/profile.d/sshpwd.sh` — warns if SSH is enabled and default `pi` password is unchanged

---

## What's Not From Debian Repos

All dpkg-managed packages come from official Raspbian/RPi Foundation Trixie repos. The following are out-of-band:

| Item | Source | Notes |
|---|---|---|
| Node.js v22.22.0 | nodejs.org tarball → `/usr/local/node/` | Symlinked to `/usr/bin/node`; npm 10.9.4 |
| `board_ctrl` binary | `lbc-gw-system` GitLab CI | Proprietary C daemon |
| `lbc-gw-bt-tunnel_pi-zero` binary | `lbc-gw-bt-tunnel` GitLab CI | Has debug symbols (not stripped) |
| All `/opt/*/dist/` JS | `lbc-gw-*` GitLab CI | Compiled TypeScript |
| Silicon Labs `.gbl` firmware | Embedded SDK | NCP co-processor images |
| ~39 "local" dpkg packages | Carried from Buster/Bullseye | `libssl1.1`, `libicu67`, `libpython3.9`, `libnettle6`, etc. — compatibility shims for proprietary binaries |

No custom apt repositories configured.

---

## /data — Persistent Partition Layout

All runtime state lives here. `/opt` and `/etc` are read-only firmware; everything that changes at runtime is in `/data`.

```
/data/
├── middleware/res/          # BT mesh live state (project, CDB, IV index, seq no.)
│   └── backups/res_1…6/    # 6 rollback snapshots
├── api-server/res/          # API server config + logger config
├── jungremote-client/res/   # Cloud client config
├── matter-interface/res/    # Matter placeholder (empty)
├── board_ctrl/ncp_ctrl/res/ # NCP controller state
├── etc/
│   ├── network/interfaces   # Active network config (eth0 DHCP)
│   ├── network/rescue       # Static fallback (192.168.178.115/24)
│   ├── dhcp/dhclient.conf   # DHCP client options
│   └── nginx/               # TLS cert, key, DH params, fingerprint
├── root/.gnupg/             # GPG keyring (symlinked from /root/.gnupg)
└── update/update_check.sh   # A/B update manager
```

---

## Update System

`/data/update/update_check.sh` implements a full A/B partition update scheme:

- Checks `https://software.jung.de/lbcgw/` for new versions (also supports USB/IBN offline)
- Verifies GPG signature (key fingerprint `AE2C321F44CC904C8BD037CCDCF86435A471F117`) and decrypts with `mdecrypt`
- Switches between `mmcblk0p2` (current) and `mmcblk0p3` on update
- Backs up `/data` before flash; restores on rollback
- Supports beta/experimental channels

---

## Key Architecture Takeaways

1. **`board_ctrl` is the real init.** The single systemd unit that owns everything. It starts/monitors the Node.js processes (middleware, api-server, jungremote-client).

2. **BT Mesh is fully offloaded.** The Pi's own BT radio is disabled. All mesh traffic flows: `middleware ↔ bt_tunnel ↔ UART ↔ Silicon Labs co-processor`.

3. **Cloud connectivity is optional.** `jungremote-client` connects to `jung.de` cloud via Socket.io. All local control works without it.

4. **`/data` is the source of truth.** Mesh state, certificates, network config all live on the persistent data partition. The root partition (`/opt`, `/etc`) is effectively stateless firmware.

5. **nginx is the only external-facing surface.** Port 443 (and 80→redirect). No other ports in UFW profiles. The WAF (`6G.conf`) and DNS rebind protection make it reasonably hardened despite the self-signed cert.

6. **Security weak spots.** Root SSH login enabled with password auth and no key pre-configuration. No persistent firewall rules. UART exposed (physical access to NCP co-processor).
