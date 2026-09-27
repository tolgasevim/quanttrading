# QuantTrading

A self-hosted guide for long-term investing: portfolio analytics, factor scores and rebalancing
guidance for a Trade Republic portfolio. It never places trades. The full spec is in
[docs/PRD.md](docs/PRD.md).

**Status: Phase 0 (foundations).** Sign-in, invites, daily end-of-day prices and ECB FX rates.

## Run it on the Mac mini

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) or
   [OrbStack](https://orbstack.dev) and set it to start at login.
2. In System Settings → Energy, turn on "Prevent automatic sleeping" and "Start up automatically
   after a power failure".
3. Configure and start the stack:
   ```sh
   git clone https://github.com/tolgasevim/quanttrading && cd quanttrading
   cp .env.example .env        # set POSTGRES_PASSWORD to a long random string
   docker compose up -d --build
   ```
4. Create your account, load the starter instruments and fetch data once:
   ```sh
   docker compose exec api python -m quant.cli create-admin --email you@example.com --name Tolga
   docker compose exec api python -m quant.cli seed-instruments
   docker compose exec api python -m quant.cli ingest-fx
   docker compose exec api python -m quant.cli ingest-prices
   ```
   After this the worker fetches data by itself: FX at 16:30 and prices at 22:30 (Europe/Berlin,
   weekdays). It also catches up after the Mac mini was off.
5. Open the app:
   - On the Mac mini itself: http://localhost:3000. Set `QT_COOKIE_SECURE=false` in `.env` for
     plain http and run `docker compose up -d` again.
   - From other devices: install [Tailscale](https://tailscale.com), run
     `tailscale serve --bg 3000`, and use the `https://<mac-mini>.<tailnet>.ts.net` address. Keep
     `QT_COOKIE_SECURE=true` for this.

Turn on two-factor sign-in under Settings after the first login.

## Development

See [CLAUDE.md](CLAUDE.md) for the layout, commands and rules.
