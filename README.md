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
| `ALLOW_WRITES` | `false` | abilita gli endpoint di scrittura (DHCP, reboot) |

### Variabili di deployment (compose/Swarm)

| Variabile | Default | Note |
|---|---|---|
| `IMAGE_TAG` | `latest` | tag immagine GHCR |
| `GUI_PORT` | `8091` | porta pubblicata della GUI |
| `OVERLAY_NETWORK` | `homelab` | nome della rete overlay esterna esistente |
| `DEPLOY_NODE` | `rpi1` | hostname del nodo Swarm su cui fissare il servizio |

Tutte impostabili come env dello stack in Portainer: niente da editare nel file.
Vedi `.env.example`.

## Prova locale

```bash
pip install -r requirements.txt
export ROUTER_PASSWORD='la-tua-password'
export ROUTER_URL='http://192.168.1.1'
uvicorn app.main:app --host 0.0.0.0 --port 8080
# apri http://localhost:8080
```

## Immagine (GHCR)

GitHub Actions (`.github/workflows/build.yml`) builda l'immagine multi-arch
(arm64 + amd64) a ogni push su `main` e la pubblica su
`ghcr.io/mmesenasco/linksysapiwrapper:latest`.

## Deploy su Docker Swarm (Portainer, da git)

1. **Secret**: Portainer → Secrets → Add secret, nome `router_password`, valore = password locale admin del router.
2. **Rete**: imposta `OVERLAY_NETWORK` al nome della tua rete overlay esterna (default `homelab`).
3. **Registry GHCR**: se il package è privato, Portainer → Registries → aggiungi `ghcr.io` con username GitHub e un PAT `read:packages`. In alternativa rendi pubblico il package.
4. **Stack**: Portainer → Stacks → Add stack → *Repository*; URL del repo, reference `refs/heads/main`, compose path `stack.yml`. Deploy.

GUI su `http://rpi1:8091`.

### Deploy manuale (CLI)

```bash
printf 'la-tua-password' | docker secret create router_password -
docker stack deploy -c stack.yml velop   # richiede l'immagine su GHCR
```

## Home Assistant

Con `MQTT_HOST` impostato e l'integrazione MQTT attiva in HA, i dispositivi
"Velop <stanza>" e "Velop mesh" appaiono da soli con sensori: online, velocità e
RSSI di backhaul, canale, flag DFS, client totali.

## Note JNAP

- Azioni usate: `core/GetDeviceInfo`, `devicelist/GetDevices3`,
  `nodes/diagnostics/GetBackhaulInfo`. `router/GetWANStatus` è best-effort.
- DFS: un backhaul su canale 52–64 o 100–144 può cadere su rilevamento radar;
  il flag `is_dfs` lo evidenzia.
## Scritture (fase 2)

Con `ALLOW_WRITES=true` si attivano gli endpoint di scrittura e la sezione di
gestione nella GUI. Con il flag a `false` (default) il servizio è sola lettura e
questi endpoint rispondono `403`.

| Metodo | Endpoint | Azione |
|---|---|---|
| `GET` | `/api/reservations` | elenca le prenotazioni DHCP (anche a writes off) |
| `POST` | `/api/reservations` | aggiunge/aggiorna `{mac, ip, description}` |
| `DELETE` | `/api/reservations/{mac}` | rimuove una prenotazione |
| `POST` | `/api/nodes/{ip}/reboot` | riavvia un nodo per IP (master o figlio) |

Le prenotazioni usano un **read-modify-write stretto**: si legge l'intero oggetto
`GetLANSettings`, si modifica solo l'array `reservations`, e si rimanda indietro
l'oggetto completo con `SetLANSettings`. Range DHCP, gateway e altre prenotazioni
restano intatti. MAC e IP sono validati e un IP già prenotato per un altro MAC
viene rifiutato prima di scrivere.

Il reboot di un nodo figlio sfrutta il fatto che ogni Velop parla JNAP sul proprio
IP e accetta la stessa password admin del master.

## Licenza

[MIT](LICENSE) — libero di usare, modificare e ridistribuire, a patto di mantenere la nota di copyright. Contributi e issue benvenuti: se hai lo stesso router col firmware senza cloud, apri pure una PR.
