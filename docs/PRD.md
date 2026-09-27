# PRD — QuantTrading: Personal Quant Investing Guide

| | |
|---|---|
| **Status** | Draft v0.2 — updated after round 2 of Q&A and analysis of a real TR statement. See §13, Open Questions. |
| **Owner** | Tolga Sevim |
| **Last updated** | 2026-09-27 (v0.2) |
| **Working name** | QuantTrading (placeholder) |

---

## 1. Summary

QuantTrading is a self-hosted web app that guides a small group of investors (the owner plus a few invited friends and family) through long-term investing. It focuses on **tech stocks and ETFs**, and adds **commodity intelligence** (gold, silver, oil, and more) that is invested in through ETCs/ETFs.

The app **never places trades**. Users import their broker statements. The app computes portfolio analytics, factor scores and rebalancing guidance with deterministic quant code, and an LLM layer turns that into explanations, opinions and picks.

## 2. Decisions so far (from the kickoff Q&A)

| # | Topic | Decision | Consequence |
|---|---|---|---|
| D1 | Audience | Owner plus a few invited friends and family | Multi-user accounts, invite-only, data isolation per user. No payments. |
| D2 | Autonomy | Read-only portfolio awareness | No order routing. Guidance only; every trade is placed manually at the broker. |
| D3 | Region / broker | Germany, Trade Republic (TR) | EUR base currency, German tax logic, PRIIPs restriction (US-domiciled ETFs are not buyable, so the app must suggest UCITS equivalents). |
| D4 | Portfolio sync | CSV/PDF import | No live sync. Parsers for TR exports; manual edits as a fallback. |
| D5 | Commodities | Information, plus exposure through ETC/ETF proxies | No futures or CFDs. The app must explain ETC-specific risks (contango, roll yield, issuer risk). |
| D6 | Investing styles | All four: factor, swing, backtesting, macro | Too much for one release, so it is phased (§7). |
| D7 | MVP core | **Portfolio + factor scores** | Everything else comes later. |
| D8 | Data budget | €0, behind a swappable provider interface | Caching is mandatory; the free-source licence risk is accepted (§11). |
| D9 | AI | Heavy and **unrestricted** (owner's explicit choice) | Disclaimers, a pick log and accuracy tracking are mandatory (§6.6). |
| D10 | Stack | Python (FastAPI) backend, Next.js front end, Postgres | Python for the quant ecosystem. |
| D11 | Hosting | Home server / NAS | Docker Compose, a tunnel for friends, backups owned by the owner. |
| D12 | Notifications | In-app, email digest, Telegram bot | Three delivery channels. |
| D13 | Default risk profile | Growth, long horizon (10+ years, tolerates 30%+ drawdowns) | Owner default. Per-user profiles are an open question (Q7). |
| D14 | Friends' broker | Friends also use Trade Republic | MVP parsers cover TR only. Other brokers stay P2 (FR-16). |
| D15 | Data licence | Switch to a licensed provider (~€20–30/month) once friends are onboarded | Free sources are for the owner-only phase. The provider swap is a release gate for inviting friends (§7). |
| D16 | Universe | Nasdaq-100 + TecDAX + global semiconductors (all caps) + everything any user holds | See FR-30. |
| D17 | Single-stock cap | 15% | Measured on **look-through** exposure (direct holdings plus the share held inside ETFs), not on direct holdings alone. |
| D18 | Real sample | Owner supplied a TR *Depotauszug* (securities account statement PDF) | A prototype parser reconciled 112/112 positions to the cent. The file is never committed (see §9 Security). |

## 3. Goals and non-goals

### Goals
1. **G1** — Know at any moment what I own, what it's worth in EUR, how it performed (after estimated German tax), and how risky and concentrated it is.
2. **G2** — Get a clear, data-backed opinion on each holding and each watchlist candidate (factor scores plus AI commentary).
3. **G3** — Be told when to act: allocation drift, concentration breaches, big drawdowns, material score changes.
4. **G4** — Understand the macro and commodity backdrop (gold, silver, oil, rates, USD) and what it means for a tech-heavy portfolio.
5. **G5** — Hold the guidance accountable: every AI pick and quant signal is logged and scored against outcomes.

### Non-goals
- Placing or routing orders, or storing broker credentials.
- Futures, options, CFDs, crypto (open: Q12), forex trading.
- Intraday or real-time data and high-frequency strategies.
- Filing tax returns. Tax figures are estimates, not tax advice.
- Commercial use, payments, or public signup.

## 4. Users and personas

| Persona | Description | Needs |
|---|---|---|
| **Owner / Admin** (Tolga) | Growth investor, TR user, tech-conviction, technically capable | Everything, plus admin: invite users, manage data providers, view system health, see all AI logs. |
| **Invited member** | Friend or family member with their own portfolio | Import their own statements, see their own analytics and guidance. Must never see another member's holdings. |

## 5. Key user journeys

1. **Onboarding**: accept the invite, sign in with a passkey or TOTP, accept the disclaimer, set the risk profile and target allocation, upload the first TR export, review the parsed positions, see the dashboard.
2. **Weekly check-in**: open the email digest or Telegram message, jump to the dashboard, see drift and alerts, read the AI weekly commentary, adjust TR savings-plan (Sparplan) amounts as suggested.
3. **New idea**: add a ticker or ISIN to the watchlist, get its factor scorecard, its fit with the current portfolio (overlap, concentration impact), and an AI verdict. Log a decision with "why".
4. **Commodity check**: gold drops 5% in a week. Telegram alert, then the commodity page explains the drivers (real yields, USD) and suggests whether to top up the gold ETC hedge within the target band.
5. **Post-trade update**: buy at TR, upload the new export (or add the trade manually), the portfolio updates, and the rebalancing guidance is recomputed.

## 6. Functional requirements (MVP unless marked otherwise)

Priority: **P0** is required for the MVP, **P1** is desirable for the MVP, **P2** comes in a later phase.

### 6.1 Accounts and access
- **FR-1 (P0)**: Invite-only registration. The admin creates invite links.
- **FR-2 (P0)**: Sign-in with a passkey (WebAuthn), with email and password plus TOTP as a fallback.
- **FR-3 (P0)**: Strict per-user data isolation. Every query is scoped by `user_id`, enforced by Postgres row-level security.
- **FR-4 (P0)**: Each user accepts a disclaimer ("not investment advice, AI may be wrong") on first login and after every change to it.
- **FR-5 (P1)**: The admin panel shows users, last import, data-job health and LLM spend.

### 6.2 Portfolio import
- **FR-10 (P0)**: Parse the **Trade Republic transaction CSV export**: buys, sells, savings-plan executions, dividends, interest, fees, taxes, deposits and withdrawals.
- **FR-11 (P0)**: Parse the **TR Depotauszug** (securities account statement PDF), a holdings snapshot: quantity, name, ISIN, custody country, price and EUR value per line, plus the position count and total. Validated with a prototype on a real statement (112 positions, total reconciled exactly).
- **FR-11a (P1)**: Parse **TR trade confirmations** (Abrechnungen) to get cost basis and tax withheld per trade.
- **FR-17 (P0)**: **Snapshot vs. history**: a Depotauszug has *no cost basis*, so P&L and tax need the transaction history (FR-10/11a). Until history is imported, the app works in **snapshot mode**: allocation, risk, scores and guidance all work, while P&L and tax screens ask for history or a manual average cost per position.
- **FR-18 (P0)**: Parser edge cases seen in real data:
  - the same ISIN on several lines (e.g. split custody), which must be aggregated;
  - nominal-quoted instruments (bonds quoted in "USD" nominal, price in %) as well as piece-quoted "Stk.";
  - fractional quantities (savings plans);
  - multi-page tables with repeated headers and footers.
- **FR-19 (P0)**: **Import checksum**: the parsed position count and total value must equal the statement footer, otherwise the import is rejected with a line-level diff. Prices in the statement are Lang & Schwarz closing prices; the app stores them as the "broker mark" next to its own market-data price and shows any difference.
- **FR-19a (P0)**: **Personal data stripping**: name, address and account number are removed during parsing and never written to the database, logs, LLM prompts or test fixtures. Uploaded files are deleted after a successful import by default.
- **FR-12 (P0)**: Map ISINs to tickers and exchanges (OpenFIGI API, cached), with a manual override when mapping fails.
- **FR-13 (P0)**: Imports are idempotent: re-uploading overlapping exports must not duplicate transactions. Deduplicate on a transaction fingerprint.
- **FR-14 (P0)**: Manual add, edit and delete of transactions, with an audit trail.
- **FR-15 (P0)**: An import preview screen shows the parsed rows, unmapped ISINs and warnings, and requires confirmation before committing.
- **FR-16 (P2)**: Pluggable parsers for other brokers (Scalable, IBKR). How many are needed depends on Q2.

### 6.3 Portfolio analytics
- **FR-20 (P0)**: Holdings table showing quantity, average cost (FIFO, matching German tax rules), market value in EUR, unrealised and realised P&L, weight and asset class.
- **FR-21 (P0)**: Performance: time-weighted return and money-weighted return (XIRR), against a user-chosen benchmark (default: MSCI World; alternatives Nasdaq-100 and S&P 500 via UCITS ETF proxies).
- **FR-22 (P0)**: Risk: annualised volatility, maximum drawdown, beta against the benchmark, portfolio correlation matrix, and 1-year historical VaR/CVaR at 95%.
- **FR-23 (P0)**: Concentration by position, sector, country and currency (USD exposure), and asset class (equity, ETF, commodity ETC, cash).
- **FR-24 (P0, promoted from P1)**: ETF look-through: aggregate the underlying holdings of ETFs (e.g. an MSCI World ETF plus NVDA shares means hidden NVDA overlap), using issuer holdings files. *Promoted because real portfolios hold several Nasdaq-100 and IT-sector ETFs **and** the same mega-caps directly, so direct weights understate true exposure. Caps (FR-43, D17) apply to look-through exposure.*
- **FR-27 (P0)**: **Instrument-type awareness** beyond plain stocks and ETFs:
  - *Leveraged/inverse ETPs* (e.g. 3x short oil): flag the decay from daily resets, show holding time, and warn when one is held past a configurable number of days.
  - *Crypto-linked equities* (miners, treasury companies, exchanges): tagged as their own risk bucket.
  - *Bonds*: maturity, yield to maturity, currency.
  - *ELTIFs / private-market funds*: illiquid, infrequent net asset value; show a liquidity and valuation-staleness flag.
  - *Pre-IPO or thinly traded shares*: prices flagged as low-confidence.
- **FR-28 (P0)**: **Cash**: a Depotauszug does not include the cash account. Cash is entered manually or taken from the transaction export.
- **FR-25 (P0)**: FX: EUR base currency using daily ECB reference rates. Show the FX contribution to return separately.
- **FR-26 (P1)**: German tax estimate: 25% Abgeltungssteuer plus 5.5% Soli (plus optional church tax), the €1,000 Sparerpauschbetrag (€2,000 joint), 30% Teilfreistellung for equity ETFs, Vorabpauschale, and separate loss pots (equity loss pot vs. general). Show both pre-tax and estimated after-tax P&L.

### 6.4 Factor scoring
- **FR-30 (P0)**: The **universe** (D16) is the union of:
  - Nasdaq-100 constituents;
  - TecDAX constituents;
  - **global semiconductors across all market caps**: US (PHLX SOX members plus small and mid caps), EU (ASML, Infineon, STMicro, ASM International, BE Semiconductor, Aixtron, Soitec, …) and Asia (TSMC, Samsung, SK Hynix, Tokyo Electron, Advantest, …);
  - every instrument any user holds or watches.

  Constituent lists refresh monthly. Free fundamentals for Asian and small-cap semis are weak, so their scores carry data-quality flags (FR-34) until the paid provider (D15) is in place.
- **FR-31 (P0)**: **Stock factor scores**, computed as sector-relative percentile ranks from 0 to 100:
  - *Value*: EV/EBIT, free-cash-flow yield, EV/Sales (growth-adjusted)
  - *Quality*: ROIC, gross margin and its stability, accruals, net debt/EBITDA
  - *Growth*: revenue growth (TTM, 3-year CAGR), EPS revision trend where available
  - *Momentum*: 12-1 month return, 6-month return, distance from the 200-day moving average
  - *Low volatility / risk*: 1-year volatility, beta, drawdown
  - A **composite score** with weights set per risk profile (Growth default: Momentum 30, Quality 25, Growth 25, Value 10, Low-vol 10)
- **FR-32 (P0)**: **ETF scorecard**: TER, tracking difference, fund size, replication method, distributing vs. accumulating (tax impact through Vorabpauschale), domicile (UCITS required), overlap with existing holdings, and the factor tilt of its holdings (P1).
- **FR-33 (P0)**: **ETC scorecard** for commodities: physical vs. synthetic, delivery claim (e.g. Xetra-Gold), TER, issuer, roll methodology for oil, and German tax treatment. For example, gold ETCs with a physical delivery claim may be tax-free after one year of holding; the app shows this as "verify with your tax advisor".
- **FR-34 (P0)**: Each score has a **data-quality flag**: stale, missing inputs or imputed values. A score must never look confident when its inputs are missing.
- **FR-35 (P0)**: Score history is stored daily so trends ("quality falling for 3 months") can be shown and backtested later.

### 6.5 Guidance and rebalancing
- **FR-40 (P0)**: The user defines a **target allocation** as buckets with bands. Default for the Growth profile: tech single stocks 40%, broad/tech ETFs 45%, commodity hedge (gold/silver ETC) 5–10%, cash 5%.
- **FR-41 (P0)**: **Drift detection**: when a bucket leaves its band (default ±5 percentage points) or a single position exceeds its cap (default 15% for a single stock), raise an alert.
- **FR-42 (P0)**: **Rebalancing suggestions** that are:
  - **Cash-flow first**: prefer adjusting TR savings-plan amounts or directing new deposits over selling, because selling creates taxable gains.
  - **Tax-aware** (P1): use the remaining Sparerpauschbetrag, harvest losses, and avoid selling lots that are highly taxed.
  - Shown as concrete instructions ("reduce Sparplan X from €200 to €100/month, add €100 to Y"), with a rationale.
- **FR-43 (P0)**: **Guardrails** configurable per user: maximum single stock, maximum sector, maximum single-country exposure, minimum cash, and drawdown alert thresholds.
- **FR-45 (P0)**: **Long-tail review**: flag positions below a size threshold (default 0.5% of the portfolio) and ask for a decision on each: *grow to conviction size, keep as a tracker, or exit* (with the tax impact of exiting). Real portfolios can have many dozens of positions under 0.5%.
- **FR-46 (P0)**: **Redundancy detection**: several ETFs tracking the same or a heavily overlapping index (e.g. two Nasdaq-100 ETFs, two World Momentum ETFs), with a consolidation suggestion that respects tax (e.g. redirect savings plans instead of selling).
- **FR-44 (P0)**: **Decision journal**: the user logs "bought / sold / ignored guidance" with a reason. It feeds into accuracy tracking.

### 6.6 AI layer (heavy, unrestricted per D9)
- **FR-50 (P0)**: **Chat assistant** with tool access to the user's own portfolio, scores, prices, macro data and news. It can answer "should I buy more NVDA?", "what happens to my portfolio if oil hits $120?", and similar.
- **FR-51 (P0)**: **Opinions and picks allowed** (owner's decision). The AI may say buy, sell or hold and suggest new tickers.
- **FR-52 (P0, non-negotiable)**: **Pick log**: every AI recommendation that names an instrument and a direction is extracted and stored with a timestamp, the price at that time, the horizon and the rationale. It is scored automatically after 1, 3, 6 and 12 months against the benchmark and shown on an **"AI track record"** page. *Reason: an unrestricted AI without measurement is just noise with confidence.*
- **FR-53 (P0)**: Each AI message carries a persistent "AI-generated, may be wrong, not investment advice" label, and shows which data points it used (tool calls visible).
- **FR-54 (P0)**: A **weekly AI commentary** per user: portfolio changes, score movers, macro and commodity backdrop, suggested actions.
- **FR-55 (P1)**: News and earnings summaries for holdings (from free RSS or SEC filings).
- **FR-56 (P0)**: **LLM cost cap**: a monthly spend limit per user and in total, with graceful degradation when it is reached.
- **FR-57 (P0)**: **Privacy**: portfolio data is sent to the LLM provider. Each member must consent at onboarding. There is an option to anonymise amounts (weights only).

### 6.7 Commodities and macro (basic in the MVP, full in Phase 2)
- **FR-60 (P0)**: A commodity panel for gold, silver, WTI and Brent crude, copper, and natural gas (P1): price in USD and EUR, 1-week/1-month/1-year/5-year change, 52-week range.
- **FR-61 (P0)**: A key-driver panel with free data:
  - Gold and silver: 10-year real yield (FRED DFII10), USD index (FRED DTWEXBGS), gold/silver ratio
  - Oil: EIA weekly inventories, Brent–WTI spread, OPEC headlines (P1)
- **FR-62 (P1)**: Mapping of each commodity to tradable UCITS ETCs/ETFs that are available at TR.
- **FR-63 (P2)**: A regime model (risk-on/off from rates, inflation, USD, credit spreads and oil) and its implication for tech weighting.
- **FR-64 (P2)**: Correlation of the commodity hedge with the tech portfolio over rolling windows ("is gold actually hedging you?").

### 6.8 Notifications
- **FR-70 (P0)**: In-app notification centre.
- **FR-71 (P0)**: Email digest, weekly by default and optionally daily (SMTP from the home server via a relay such as Brevo's free tier).
- **FR-72 (P0)**: Telegram bot: each user links their own chat. Alert types: drift or guardrail breaches, a holding moving ±X% in a day, material score changes, the weekly AI commentary, data-job failures (admin only).
- **FR-73 (P0)**: Per-user alert settings and quiet hours.

### 6.9 Later phases (P2)
- **Backtesting lab**: test factor and composite strategies over historical data. It must handle survivorship bias (delisted tickers), transaction costs (TR: €1 per trade, savings plans free) and taxes. Output: CAGR, Sharpe, Sortino, maximum drawdown and turnover against the benchmark.
- **Swing signal scanner**: a daily end-of-day scan of the watchlist with trend and mean-reversion signals, and entry, stop and target levels. **Only signals whose backtests pass acceptance criteria are shown**, so the lab must come first.
- **Macro regime dashboard** (FR-63/64 in full).

## 7. Release plan

| Phase | Scope | Exit criteria |
|---|---|---|
| **0 — Foundations** | Repo, Docker Compose, auth, database schema, provider interface, price and FX ingestion, CI | Owner can log in; daily EOD prices land in the database. |
| **1 — MVP** | §6.1–6.8 P0 items | Owner imports real TR history; holdings match TR to within €1; factor scores for 100+ tickers; weekly digest delivered by Telegram and email. |
| **1.1** | P1 items (tax estimate, trade-confirmation import, news) | — |
| **Friends gate** | Licensed data provider live (D15), privacy consent flow, legal check (Q10) | Required **before** the first non-owner invite. |
| **2 — Macro & commodities** | Regime model, full commodity page | — |
| **3 — Backtesting lab** | Engine plus UI | Reproduces a known benchmark strategy within tolerance. |
| **4 — Swing scanner** | Signals gated by backtests | — |

## 8. Data sources (free tier, behind a `DataProvider` interface)

| Need | Primary (free) | Notes / fallback |
|---|---|---|
| EOD prices, stocks and ETFs (US and Xetra) | yfinance | Unofficial, **personal-use ToS**, can break. Fallback: Stooq. Paid swap target: EODHD or Tiingo (~€20–30/month). |
| US fundamentals | **SEC EDGAR companyfacts API** | Official and free; needs parsing of XBRL tags. |
| EU fundamentals | yfinance (patchy) | Weakest area of the free approach. Flag data quality (FR-34). |
| FX | ECB reference rates | Official, free. |
| Macro | FRED API | Free API key. |
| Oil inventories | EIA API | Free API key. |
| Commodity spot and futures prices | yfinance (GC=F, SI=F, CL=F, BZ=F) | Front-month futures used as a proxy for spot. |
| ISIN → ticker | OpenFIGI | Free, rate-limited. Cache permanently. |
| ETF holdings | Issuer CSVs (iShares, Xtrackers, Vanguard) | Scraped weekly; fragile. |
| News | RSS (company IR, Reuters/others), SEC 8-K | P1. |

**Rules**: all raw responses are cached in Postgres. Jobs run after the US close (around 22:30 CET) with retries. Each provider has a health check, and the admin is alerted on failure. Swapping providers is a configuration change, not a code change.

## 9. Architecture (proposal)

```
[Browser / PWA] ──HTTPS──> [Tunnel: Tailscale or Cloudflare Tunnel+Access]
                                   │
                           [Home server — Docker Compose]
        ┌──────────────┬───────────┴──────────┬──────────────────┐
   [Next.js web]  [FastAPI api]        [Worker + scheduler]   [Telegram bot]
                        │                (APScheduler/Celery)       │
                        └────────────┬──────────┴───────────────────┘
                                [Postgres 16]  (+ optional TimescaleDB)
                                     │
                         [Encrypted offsite backup (restic → B2/S3)]
External: data providers (§8), LLM API, SMTP relay
```

- **Quant core**: pandas, numpy, scipy, statsmodels; `vectorbt` or a custom engine for the Phase 3 backtests.
- **LLM**: provider behind an adapter (e.g. the Claude API) with tool calling into internal read-only endpoints.
- **Security**: real broker statements and exports are never committed. `.gitignore` blocks `*.pdf` and `statements/`, and parser fixtures are synthetic or fully anonymised. Secrets in `.env` stored outside git, row-level security in Postgres, uploaded statements encrypted at rest and deleted after parsing (configurable), rate limiting, automatic security updates on the host.
- **Ops**: Uptime Kuma or healthchecks.io for job monitoring; nightly backups with restore tested quarterly; a UPS is recommended.

## 10. Non-functional requirements

| Area | Requirement |
|---|---|
| Performance | Dashboard loads in under 2 s p95 for a portfolio of 100 positions. Import of 5 years of TR history takes under 30 s. |
| Freshness | EOD data by 07:00 CET the next morning. Staleness is shown in the UI. |
| Reproducibility | Every score and signal stores its input snapshot and code version. |
| Availability | Best effort (home server). Target 99% monthly. |
| Privacy | GDPR-minded: store the minimum personal data, allow export and delete per user, keep the consent log. |
| Accessibility | Keyboard-navigable, colour-blind-safe charts, dark mode. |
| Language | English UI. German and Turkish depend on Q9. |
| Mobile | Responsive PWA; no native app. |

## 11. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **Regulatory**: sharing buy/sell calls (especially unrestricted AI picks) with other people could count as investment advice under German law (KWG/WpIG, §34f/h GewO) | High | Keep it **strictly non-commercial and invite-only**, with clear disclaimers and per-user acknowledgement. Consider a one-off legal check before inviting anyone outside family (see Q10). |
| Free data breaks or its licence forbids sharing with friends | Medium | Provider interface plus cache (D8). Budget a paid swap when friends join (Q3). |
| AI hallucinates numbers or makes bad picks | High | Tool-grounded data, a visible sources panel, a pick log and track record (FR-52). |
| TR changes its export format | Medium | Versioned parsers, fixture tests from real anonymised exports, manual-entry fallback. |
| Home server outage or data loss | Medium | Offsite encrypted backups, restore drills, UPS. |
| Scope creep (four investing styles) | High | Strict phasing (§7). No Phase 2+ work until the MVP exit criteria are met. |
| Survivorship and look-ahead bias in scores or backtests | Medium | Point-in-time fundamentals (EDGAR filing dates), and keep delisted tickers. |

## 12. Success metrics

- The owner uses it weekly for 3 or more consecutive months.
- Portfolio value matches TR within 0.5% after each import.
- 100% of AI picks are logged. After 12 months there is a clear answer to whether the AI beat the benchmark.
- Fewer than 1 unhandled data-job failure per month.
- Rebalancing guidance followed at least 50% of the time, measured from the decision journal. If it is lower, either the guidance or the target allocation is wrong.

## 13. Open questions — round 3

**Answered in round 2**: Q1 (statement supplied, D18), Q2 (friends use TR, D14), Q3 (paid data when friends join, D15), Q4 (universe, D16), Q6 (15% cap, D17). Q5 (target allocation) is partly answered, because the statement shows the current split. It is re-asked below as Q20.

**Blocking the MVP design:**
- **Q18 — Cash**: the Depotauszug has no cash balance. How much cash or TR interest balance should count as the "cash" bucket?
- **Q19 — History**: can you also export the **transaction history** (TR app → Settings → Account → transaction export, or a set of Abrechnung PDFs)? Without it there is no cost basis, so no P&L or tax figures.
- **Q20 — Target allocation**: is the current split your target, or do you want the app to steer you somewhere else? (Actual split in the owner-only analysis; not stored in the repo.)
- **Q21 — What counts as "tech"?**: consumer internet (Amazon, Alibaba, Uber), fintech (Adyen, PayPal, Klarna), media/streaming (Netflix, Spotify) and IT services (Accenture, EPAM): all "tech", or separate buckets with their own targets?
- **Q22 — Crypto-linked equities** (miners, treasury companies, exchanges): a tagged sub-bucket with its own cap, or excluded from guidance? Should real crypto ever be tracked (the old Q12)?
- **Q23 — Leveraged ETPs**: what are your leveraged or inverse ETP positions for: hedges, tactical trades, or leftovers? This decides whether the app treats leveraged ETPs as "trading book" (strict holding-time alerts) or rejects them in guidance.
- **Q24 — Long tail**: are 0.5% as the "too small to matter" threshold and a target of roughly 40–60 positions reasonable, or do you deliberately want many small bets?

**Still open from round 1:**
- **Q7 — Per-user risk profiles**: do friends get their own questionnaire, or inherit Growth?
- **Q8 — Tax details**: single or joint filing? Church tax? A Freistellungsauftrag at TR only?
- **Q9 — UI language**: English only, or German/Turkish as well?
- **Q10 — Legal comfort**: family only, or also colleagues? Are the unrestricted AI picks visible to them?
- **Q11 — LLM budget and provider**: maximum monthly spend, in total and per user.
- **Q13 — Home server**: hardware (CPU/RAM, x86 or ARM)? Tailscale or Cloudflare Tunnel?
- **Q14 — Alert thresholds**: which daily move should ping you (e.g. ±5% for stocks, ±3% for ETFs)?
- **Q15 — Benchmark**: MSCI World, Nasdaq-100, or a custom mix?
- **Q16 — Savings plans**: which Sparpläne run today (instrument, amount, frequency)?
- **Q17 — Development**: who builds it, and what is the MVP timeline?

## 14. Glossary

- **ETC**: Exchange-Traded Commodity, a debt security tracking a commodity. The EU route to gold, silver and oil exposure.
- **UCITS**: EU fund regulation. Only UCITS ETFs are generally available to EU retail investors because of PRIIPs.
- **Vorabpauschale**: German advance lump-sum tax on accumulating funds.
- **Teilfreistellung**: partial tax exemption for equity funds (30%).
- **TWR / MWR (XIRR)**: time-weighted and money-weighted return.
- **Drift band**: allowed deviation from the target weight before a rebalance is suggested.
