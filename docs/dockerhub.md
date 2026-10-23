# Inverter Dashboard

A web dashboard for hybrid solar inverters (Deye / Sunsynk with a Solarman Wi-Fi logger), made for a wall tablet or any browser on your home network.

![Dashboard](https://raw.githubusercontent.com/cutteroid/inverter-dashboard/main/docs/preview.png)

- **Live readings:** solar, battery, grid and home consumption, per phase on 3-phase inverters.
- **Battery:** "lasts about …" during an outage and "full in …" while charging; outage readiness ("will the battery last through the next planned outage?"); inverter state, temperatures and lifetime battery cycles.
- **Planned outages:** today's and tomorrow's schedule (DTEK, Yasno, Lvivoblenergo), with a countdown.
- **Air raid alerts:** a map of Ukraine by region and district (alerts.in.ua or NEPTUN).
- **Weather**, a fullscreen kiosk view, English / Ukrainian / Russian, an optional Telegram bot.
- **Setup in the browser:** a setup wizard on first start; settings protected by a PIN.

Source, full documentation and releases: **[github.com/cutteroid/inverter-dashboard](https://github.com/cutteroid/inverter-dashboard)**

## Tags

- `latest`: the newest release
- `2.4.0`, …: a specific release

Each tag is built for `linux/amd64` (Intel/AMD) and `linux/arm64` (ARM, e.g. Synology j models such as the DS223j, Raspberry Pi 4/5).

## Install on a Synology NAS (Container Manager)

1. **Registry:** search `cutteroid/inverter-dashboard` → **Download** → tag `latest`. (Not found yet? **Image → Add → Add from URL** with `cutteroid/inverter-dashboard`.)
2. **File Station:** create a folder for the dashboard's data, e.g. `docker/inverter-dashboard/data`.
3. **Container → Create:** pick the image, then:
   - enable auto-restart;
   - **Port settings:** local port `8080` → container port `8080`;
   - **Volume settings:** add the folder from step 2, mounted at `/app/data`.
4. Open `http://<NAS-IP>:8080`. The setup wizard asks for the inverter's logger (IP address and serial number) and the optional features, and saves everything in the data folder.

## Install with Docker

```bash
docker run -d --name inverter-dashboard \
  -p 8080:8080 \
  -v /path/to/data:/app/data \
  --restart unless-stopped \
  cutteroid/inverter-dashboard:latest
```

Or with Docker Compose:

```yaml
services:
  inverter-dashboard:
    image: cutteroid/inverter-dashboard:latest
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    restart: unless-stopped
```

## Data and settings

- **`/app/data`** holds everything the dashboard keeps: the configuration (`.env`, written by the setup wizard and the settings panel), statistics, logs and caches. Mount a host folder there so it survives updates.
- **No permissions to set:** the dashboard runs as a non-root user (uid 1000). At start the container makes the data folder writable for it, changing only what that user can't write yet.
- **Don't pass settings with `-e` / `--env-file`:** environment variables override `data/.env`, so changes saved in the dashboard would be ignored.
- **Port:** `8080`. Keep the dashboard on your local network; the settings PIN is basic protection, not real authentication.

## Updating

Download the new `latest` (or a version tag), then recreate the container with the same port and folder (`docker pull` + remove + `docker run` again, or `docker compose pull && docker compose up -d`). The data folder keeps the configuration and statistics.

## Not in this image

The **smart meter** feature (Bakler meter over MQTT) needs a Mosquitto broker, which this image doesn't include. Install from the source with Docker Compose for it; see the GitHub README.

## License

MIT. Based on [deye-dashboard](https://github.com/ivanursul/deye-dashboard) by Ivan Ursul.
