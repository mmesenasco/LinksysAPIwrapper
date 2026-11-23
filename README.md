# JNAP Velop

Servizio autonomo che interroga il mesh Linksys Velop / MR9000 via JNAP (HTTP,
firmware senza cloud) e offre:

- **GUI web** leggera (`/`) con nodi, backhaul (RSSI / canale / velocità / flag DFS), client, stato WAN;
- **API JSON** (`/api/state`) — punto d'aggancio per integrazioni future (es. Teufelsberg);
- **export verso Home Assistant** via MQTT discovery (i sensori compaiono da soli);
- **healthcheck** (`/healthz`).

Stato letto da un unico snapshot (`app/poller.py`), condiviso da GUI, API e MQTT.

## Configurazione (env)

| Variabile | Default | Note |
|---|---|---|
| `ROUTER_URL` | `http://192.168.1.1` | IP del router master |
| `POLL_INTERVAL` | `30` | secondi tra un polling e l'altro |
| `ROUTER_PASSWORD` / `ROUTER_PASSWORD_FILE` | — | password locale admin; il secret ha priorità |
| `MQTT_HOST` | — | se assente, MQTT disabilitato |
| `MQTT_PORT` | `1883` | |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | — | opzionali |

## Prova locale

```bash
pip install -r requirements.txt
export ROUTER_PASSWORD='la-tua-password'
export ROUTER_URL='http://192.168.1.1'
uvicorn app.main:app --host 0.0.0.0 --port 8080
# apri http://localhost:8080
```

## Deploy su Docker Swarm

```bash
# 1. secret con la password del router
printf 'la-tua-password' | docker secret create router_password -

# 2. build multi-arch (su un nodo arm64 basta build locale)
docker build -t jnap-velop:latest .

# 3. deploy (adatta rete overlay e host MQTT in stack.yml)
docker stack deploy -c stack.yml velop
```

GUI su `http://<nodo>:8091`.

## Home Assistant

Con `MQTT_HOST` impostato e l'integrazione MQTT attiva in HA, i dispositivi
"Velop <stanza>" e "Velop mesh" appaiono da soli con sensori: online, velocità e
RSSI di backhaul, canale, flag DFS, client totali.

## Note JNAP

- Azioni usate: `core/GetDeviceInfo`, `devicelist/GetDevices3`,
  `nodes/diagnostics/GetBackhaulInfo`. `router/GetWANStatus` è best-effort.
- DFS: un backhaul su canale 52–64 o 100–144 può cadere su rilevamento radar;
  il flag `is_dfs` lo evidenzia.
- Le azioni di scrittura (prenotazioni DHCP, reboot nodo) sono volutamente
  **fuori** da questa v1 read-only. Fase 2.
