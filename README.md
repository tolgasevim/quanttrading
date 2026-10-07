# QuantTrading

A self-hosted guide for long-term investing: portfolio analytics, factor scores and rebalancing
guidance for a Trade Republic portfolio. It never places trades. The full spec is in
[docs/PRD.md](docs/PRD.md).

**Status: Phase 1 in progress.** Sign-in, invites, daily end-of-day prices and ECB FX rates,
import of the Trade Republic transaction export, and holdings rebuilt from it and checked against
the securities and crypto statements.

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
   ```
   Once you have imported your transactions in the app (step 5), look up the tickers and fetch the prices:
   ```sh
   docker compose exec api python -m quant.cli map-isins
   docker compose exec api python -m quant.cli ingest-prices
   ```
   The worker then does this by itself: FX at 16:30, ticker lookup at 22:00 and prices at 22:30
   (Europe/Berlin, weekdays), and the alerts right after the prices. It also catches up after the
   Mac mini was off. To make the alerts by hand, run
   `docker compose exec api python -m quant.cli create-alerts`. The alerts (big daily moves in
   your holdings, and failed jobs for the owner) are on the Alerts page, where you set the limits.
   A holding without a
   ticker is listed on the Prices page, where you can enter it by hand. An optional free OpenFIGI
   key (`QT_OPENFIGI_API_KEY`) raises the lookup rate limit. The same job reads the sector of each
   share (Yahoo's names) for the Holdings page. Coins are priced in euros from
   CoinGecko: the app finds each coin by the name Trade Republic gives it (Prices page, "coin id"
   for a hand entry). It works without a key; a free demo key (`QT_COINGECKO_API_KEY`) raises the
   rate limit. If your `.env` sets `QT_PRICE_PROVIDERS`, add `coingecko` to it. The free API serves
   one year of history, so older coin prices are not backfilled.
5. Open the app:
   - On the Mac mini itself: http://localhost:3000. Set `QT_COOKIE_SECURE=false` in `.env` for
     plain http and run `docker compose up -d` again.
   - From other devices: install [Tailscale](https://tailscale.com), run
     `tailscale serve --bg 3000`, and use the `https://<mac-mini>.<tailnet>.ts.net` address. Keep
     `QT_COOKIE_SECURE=true` for this.

Turn on two-factor sign-in under Settings after the first login.

## The AI guide (optional)

The AI page is a chat about your portfolio. It gives guidance only. It never places trades. Every
answer carries the label "AI-generated, may be wrong, not investment advice", the list of data it
used and the tools it called.

What it does:
- **Chat with tools**: the model can look up one of your holdings, stored prices, a price-shock
  scenario ("what if tech falls 20%"), your tax estimate, ECB rates and your earlier picks. It has
  no news, macro data or live quotes, and says so.
- **Pick log and track record**: every buy, sell or hold it names is logged with the price and
  the reason, and scored after 1, 3, 6 and 12 months against a Nasdaq-100 fund (`/ai/track-record`).
  The scoring job runs every evening after the prices.
- **Weekly commentary**: every Sunday at 18:00 (`QT_COMMENTARY_CRON`) each user gets a short note
  (`/ai/weekly`). If the AI is off, not allowed or over budget, it is a plain summary of your data.
  Write it by hand with `docker compose exec api python -m quant.cli weekly-commentary`.

Set-up:
1. Get an API key from Anthropic and set `QT_ANTHROPIC_API_KEY` in `.env`. Keep it secret.
2. The `anthropic` package is a new dependency. On a machine that can reach PyPI, run
   `cd backend && uv lock` and commit `uv.lock`, then rebuild the images. Until then the AI page
   says it is not set up.
3. Everybody accepts the app disclaimer on first login (and again when its text changes). On the
   AI page they accept a second consent about what is sent to the AI provider, and may hide
   amounts, so only weights are sent.
4. The spend cap is 2,000 EUR a month for everyone and 500 EUR for one person
   (`QT_LLM_MONTHLY_CAP_EUR`, `QT_LLM_USER_MONTHLY_CAP_EUR`). At the cap the chat pauses, the
   weekly note becomes a plain summary, and the pick log keeps working. Admins get an alert at 50,
   80 and 100 percent.
5. Server-side fallbacks are on: if the main model is busy, the API may answer with another
   model. The page says so. Switch it off with `QT_LLM_FALLBACKS=false`.
6. The code that talks to the live API was tested with a fake SDK only. Ask one question after
   set-up and check that it answers.

## Import your Trade Republic history

1. In the TR app: Profile → Settings → Account → Export transactions (CSV).
2. Optional dry run on the Mac mini, which stores nothing and prints only counts and pass/fail
   (safe to share):
   ```sh
   docker compose cp ~/Downloads/transactions.csv api:/tmp/tx.csv
   docker compose exec api python -m quant.cli check-tr-csv /tmp/tx.csv
   docker compose exec api rm /tmp/tx.csv
   ```
3. In the app: Import → choose the file → check the preview → Import.

## Check your holdings

Holdings are rebuilt from the transaction history. To compare them with a statement:

1. Download the *Depotauszug* (shares and funds) and the *Crypto-Übersicht* PDFs from the TR app
   (Profile → Documents).
2. In the app: Holdings → choose a PDF. The app tells which statement it is. Every difference is
   listed. The Depotauszug also gives the broker's price for each position.
3. Optional dry run that stores nothing and prints only counts and pass/fail:
   ```sh
   docker compose cp ~/Downloads/transactions.csv api:/tmp/tx.csv
   docker compose cp ~/Downloads/crypto.pdf api:/tmp/crypto.pdf
   docker compose cp ~/Downloads/depot.pdf api:/tmp/depot.pdf
   docker compose exec api python -m quant.cli check-holdings /tmp/tx.csv \
     --crypto-pdf /tmp/crypto.pdf --depot-pdf /tmp/depot.pdf
   docker compose exec api rm /tmp/tx.csv /tmp/crypto.pdf /tmp/depot.pdf
   ```

## Development

See [CLAUDE.md](CLAUDE.md) for the layout, commands and rules.
