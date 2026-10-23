# Docker Setup

How to build and run the dashboard with Docker. For all settings and features see the [main README](README.md).

> **Made for a dashboard screen:** the dashboard is meant to be shown on a wall-mounted tablet or another always-on screen on your home network, not as a public web service. Run the container on a machine in your home (a NAS, a Raspberry Pi) and don't expose port 8080 to the internet.

## How configuration works

The container keeps **everything it stores in `./data`**, mounted at `/app/data`:

- **Configuration:** `data/.env`. The app loads it at startup, and the settings wizard and **Settings → Edit configuration** write to it.
- **Data files:** stats, logs and caches (see [Data persistence](#data-persistence)).

Don't pass settings to the container from outside (`--env-file`, `-e`, or `env_file:` / `environment:` in a compose file). Environment variables take precedence over `data/.env`, so anything you later change in the dashboard's settings would be silently ignored.

Without `data/.env` the dashboard starts in setup mode and opens the setup wizard.

## Quick start

### Docker Compose (recommended)

```bash
git clone https://github.com/cutteroid/inverter-dashboard.git
cd inverter-dashboard

docker compose up -d --build
```

Open http://localhost:8080 and follow the setup wizard, which saves `data/.env`.

To prepare the configuration by hand instead (e.g. to copy an existing one):

```bash
mkdir -p data
cp .env.example data/.env     # then edit data/.env
docker compose up -d --build
```

`docker compose up` starts two containers:

| Container | Purpose | Port |
|-----------|---------|------|
| `inverter-dashboard` | The dashboard | 8080 |
| `inverter-mosquitto` | MQTT broker for the optional smart meter; reachable from the network only once `SMART_METER_MQTT_USER` / `SMART_METER_MQTT_PASSWORD` are set in `data/.env` | 1883 |

Logs and stopping:

```bash
docker compose logs -f
docker compose down
```

### Docker CLI

```bash
docker build -t inverter-dashboard:latest .

docker run -d \
  --name inverter-dashboard \
  -p 8080:8080 \
  -v $(pwd)/data:/app/data \
  --restart unless-stopped \
  inverter-dashboard:latest
```

The configuration comes from `./data/.env` (or the setup wizard), as with Compose. This runs only the dashboard. For the smart meter's broker, use Compose.

```bash
docker logs -f inverter-dashboard
docker stop inverter-dashboard && docker rm inverter-dashboard
```

## Configuration

Required, set by the setup wizard (step 1) or in `data/.env`:

| Variable | Description | Example |
|----------|-------------|---------|
| `INVERTER_IP` | IP address of the inverter's Solarman logger | `192.168.1.100` |
| `LOGGER_SERIAL` | Serial number of the Solarman Wi-Fi logger | `1234567890` |

Everything else is optional: weather location, outage provider, air raid alerts, smart meter, Telegram bot, battery capacity, settings PIN and so on. See [Configuration in the README](README.md#configuration) and `.env.example`. Most of it can also be changed in the dashboard under **Settings → Edit configuration**. Changes are saved to `data/.env` and the dashboard restarts by itself.

### Data persistence

Everything the dashboard stores lives in `data/`, so it survives rebuilds, updates and container re-creation:

- `.env`: the configuration
- `phase_stats.json`: phase statistics
- `grid_daily_log.json`, `generator_log.json`: grid import and generator logs
- `inverter_cache.json`, `inverter_config.json`: last readings and the detected inverter model
- `bot_state.json`, `battery_reports/`: Telegram bot state
- `smart_meter_daily.json`, `mosquitto/`: smart meter counter and broker files

The dashboard runs as user `1000` inside the container, so it needs write access to `data/`. The container takes care of that: it starts as root only for `docker-entrypoint.sh`, which gives user 1000 whatever in `data/` it can't write yet (a folder created by hand, or by Docker itself, belongs to another user), and then runs the dashboard as user 1000. Files user 1000 can already write keep their owner, and `data/mosquitto/` (the broker's) is never touched. Started with `docker run --user ...`, the container skips this step.

### Ports and volumes

| | Dashboard | MQTT broker |
|---|---|---|
| Port | `8080` | `1883` |
| Volume | `./data:/app/data` | `./data/mosquitto:/mosquitto/data`, and `./data` read-only (for the broker login in `data/.env`) |

## Networking

- The services share the bridge network `inverter-network`. The dashboard reaches the broker as `mosquitto`.
- From the host, the dashboard is at `http://localhost:8080`. From other devices, use `http://<host-ip>:8080`.
- The container reaches devices on your local network (the inverter's logger, a smart meter) through the host. If they're on a separate IoT / guest network, the router has to allow exactly that traffic; see [Devices on a Separate IoT Network](README.md#devices-on-a-separate-iot-network).

## Health checks

The image has a built-in health check: it requests `/api/data` every 30 seconds, starting 40 seconds after startup, and marks the container unhealthy after 3 failures.

```bash
docker ps
docker inspect inverter-dashboard --format='{{.State.Health.Status}}'
```

## Updating

```bash
git pull
docker compose up -d --build
```

The image is built locally from the source, so there's nothing to `docker pull`. Configuration and data in `data/` are kept.

- **Synology NAS** or another Docker host without the source: `tools/deploy_nas.sh` sends only the changed build files and rebuilds there (see the README).
- **An image bundle for offline installs:** `tools/export_images.sh`.

## Building images

```bash
docker build -t myregistry/inverter-dashboard:2.0.1 .

# Several architectures (requires buildx)
docker buildx build --platform linux/amd64,linux/arm64 -t myregistry/inverter-dashboard:2.0.1 .
```

## Troubleshooting

### Container won't start / setup wizard appears every time

```bash
docker compose logs inverter-dashboard
```

- If the wizard keeps appearing, the dashboard can't read a valid `data/.env`. Check that the file exists; the container log says if it had to fix the folder's permissions (see above).
- Settings you change in the dashboard don't stick: check that nothing passes them as environment variables (`--env-file`, `-e`, `environment:` / `env_file:` for the dashboard service).

### Cannot connect to the inverter

Check that the logger is reachable from the container:

```bash
docker exec inverter-dashboard python -c "import socket; socket.create_connection(('192.168.1.100', 8899), 3); print('logger reachable')"
```

If it isn't, the logger may be on another network (see Networking).

### Resource usage

```bash
docker stats inverter-dashboard
```

To limit resources, add to the `inverter-dashboard` service in `docker-compose.yml`:

```yaml
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 256M
```

## Production notes

- Put a reverse proxy (nginx, Traefik) in front for TLS. Keep the dashboard itself on the local network; its settings PIN is basic protection, not real authentication.
- Keep `data/` backed up: it holds the configuration (including any Telegram token and MQTT password) and the statistics.
- Example with nginx, added next to the existing services in `docker-compose.yml` (no `env_file` / `environment` for the dashboard):

```yaml
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./ssl:/etc/nginx/ssl:ro
    depends_on:
      - inverter-dashboard
    networks:
      - inverter-network
    restart: unless-stopped
```

## See also

- [Main README](README.md)
- [Docker documentation](https://docs.docker.com/)
- [Docker Compose documentation](https://docs.docker.com/compose/)
