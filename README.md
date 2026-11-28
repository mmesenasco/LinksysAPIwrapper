# JNAP Velop

Standalone service that talks to a Linksys Velop / MR9000 mesh over JNAP (HTTP,
cloud-less firmware) and provides:

- a lightweight **web GUI** (`/`) with nodes, backhaul (RSSI / channel / speed / DFS flag), clients and WAN status;
- a **JSON API** (`/api/state`) — an integration point for other tools;
- **Home Assistant export** via MQTT discovery (sensors appear automatically);
- a **health check** (`/healthz`);
- a **Clients page** (`/clients`), filterable and sortable, with a **Health** column (flapping / roaming / weak signal over a rolling window) and a "problems only" filter; `/api/clients`;
- a **Leases page** (`/leases`) with the DHCP/ARP table (IP/MAC/host, reserved vs dynamic, expiry, online, node); `/api/leases`;
- a **Security page** (`/security`) with posture (firewall, remote management, DMZ, UPnP, WPS, default admin password, …) and exposed ports; `/api/security`; security drift is recorded in the Log;
- a **Log page** (`/events`) with DFS changes, new devices, node up/down and security drift (persisted to a volume); `/api/events`.

State is read into a single snapshot (`app/poller.py`), shared by the GUI, the API and MQTT.

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| `ROUTER_URL` | `http://192.168.1.1` | master router IP |
| `POLL_INTERVAL` | `30` | seconds between polls |
| `ROUTER_PASSWORD` / `ROUTER_PASSWORD_FILE` | — | local admin password; the secret takes precedence |
| `MQTT_HOST` | — | if unset, MQTT is disabled |
| `MQTT_PORT` | `1883` | |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | optional |
| `ALLOW_WRITES` | `false` | enables the write endpoints (DHCP, reboot) |

### Deployment variables (compose/Swarm)

| Variable | Default | Notes |
|---|---|---|
| `IMAGE_TAG` | `latest` | GHCR image tag |
| `GUI_PORT` | `8091` | published GUI port |
| `OVERLAY_NETWORK` | `homelab` | name of the existing external overlay network |
| `DEPLOY_NODE` | `rpi1` | Swarm node hostname to pin the service to |

All of these can be set as stack env vars in Portainer — nothing to edit in the
file. See `.env.example`.

## Run locally

```bash
pip install -r requirements.txt
export ROUTER_PASSWORD='your-password'
export ROUTER_URL='http://192.168.1.1'
uvicorn app.main:app --host 0.0.0.0 --port 8080
# open http://localhost:8080
```

## Image (GHCR)

GitHub Actions (`.github/workflows/build.yml`) builds a multi-arch image
(arm64 + amd64) on every push to `main` and publishes it to
`ghcr.io/mmesenasco/linksysapiwrapper:latest`.

## Deploy on Docker Swarm (Portainer, from git)

1. **Secret**: Portainer → Secrets → Add secret, name `router_password`, value = the router's local admin password.
2. **Network**: set `OVERLAY_NETWORK` to the name of your external overlay network (default `homelab`).
3. **GHCR registry**: if the package is private, Portainer → Registries → add `ghcr.io` with your GitHub username and a `read:packages` PAT. Alternatively make the package public.
4. **Stack**: Portainer → Stacks → Add stack → *Repository*; repo URL, reference `refs/heads/main`, compose path `stack.yml`. Deploy.

GUI at `http://rpi1:8091`.

### Manual deploy (CLI)

```bash
printf 'your-password' | docker secret create router_password -
docker stack deploy -c stack.yml velop   # requires the image on GHCR
```

## Home Assistant

With `MQTT_HOST` set and the MQTT integration active in HA, the "Velop <room>"
and "Velop mesh" devices appear automatically with sensors: online state,
backhaul speed and RSSI, channel, DFS flag, total clients.

## JNAP notes

- Actions used: `core/GetDeviceInfo`, `devicelist/GetDevices3`,
  `nodes/diagnostics/GetBackhaulInfo`. `router/GetWANStatus` is best-effort.
- Per-client radio metrics come from `networkconnections/GetNetworkConnections`,
  queried on the master **and** on each node's own IP and merged by MAC, so
  clients on the Velop nodes also get signal/band/rate.
- DFS: a backhaul on channel 52–64 or 100–144 can drop on radar detection; the
  `is_dfs` flag highlights it.

## Writes (phase 2)

With `ALLOW_WRITES=true` the write endpoints and the management section of the
GUI are enabled. With the flag `false` (default) the service is read-only and
these endpoints return `403`.

| Method | Endpoint | Action |
|---|---|---|
| `GET` | `/api/reservations` | list DHCP reservations (even with writes off) |
| `POST` | `/api/reservations` | add/update `{mac, ip, description}` |
| `DELETE` | `/api/reservations/{mac}` | remove a reservation |
| `POST` | `/api/nodes/{ip}/reboot` | reboot a node by IP (master or child) |

Reservations use a **strict read-modify-write**: the whole `GetLANSettings`
object is read, only the `reservations` array is changed, and the complete
object is sent back with `SetLANSettings`. The DHCP range, gateway and other
reservations stay intact. MAC and IP are validated, and an IP already reserved
for a different MAC is rejected before writing.

Rebooting a child node relies on the fact that each Velop speaks JNAP on its own
IP and accepts the same admin password as the master.

## License

[MIT](LICENSE) — free to use, modify and redistribute, provided the copyright
notice is kept. Contributions and issues welcome: if you have the same router on
the cloud-less firmware, feel free to open a PR.
