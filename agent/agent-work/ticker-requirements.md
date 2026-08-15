# Income Investor Research Platform

Build a web application for income-focused investors, deployed on Cloudflare using this Next.js template.

Initial scope:
- A landing/dashboard page that introduces the platform and links to research tools.
- Clean, fast, mobile-friendly UI.
- No authentication required for the initial version.

## Feature: High-Yield Ticker Universe

1. Automated daily Cloudflare job (scheduled Worker / cron trigger) that builds and refreshes a "ticker universe" of securities with a current yield of 3% or more, using only free data sources such as yfinance and SEC EDGAR:
   - Include single stocks, ETFs, CEFs, BDCs, MLPs, and REITs.
   - For every ticker, store monthly price history for up to the last 10 years.
   - Store all other attributes available from yfinance, such as company name, industry, sector, category, market cap, dividend rate, dividend yield, payout ratio, expense ratio (for funds), etc.
   - Persist the universe in Cloudflare-native storage (e.g., D1) so it can be queried by the web app.
   - The job must be idempotent and safe to re-run; failures on individual tickers must not abort the whole run.

2. A web page to query the ticker universe:
   - Filter by yield range, security type (stock/ETF/CEF/BDC/MLP/REIT), sector/industry, and market cap.
   - Sortable results table with the stored attributes.
   - Detail view per ticker showing its attributes and a monthly price history chart.

## Decisions and constraints (assume these; make reasonable engineering assumptions for anything else)

- Seed universe: start from a static seed list of ~200 well-known dividend-paying tickers (bundled JSON in the repo covering stocks, ETFs, CEFs, BDCs, MLPs, REITs); the daily job refreshes their data and drops those below 3% yield from query results (keep the rows, flag them inactive).
- Data access: yfinance has no official API — use Yahoo Finance's public quote/chart JSON endpoints directly from the Worker (same data yfinance wraps). EDGAR is secondary/optional enrichment; skipping it is acceptable for v1.
- Storage: Cloudflare D1 (SQLite). Two tables minimum: tickers (attributes) and monthly_prices (ticker, month, close). Upserts keyed on ticker/month.
- Scheduling: one cron trigger at 06:00 UTC daily; batch tickers with per-ticker error isolation and modest rate limiting.
- Query page: server-rendered Next.js page hitting D1 via a route handler; no client state library; charts with a lightweight inline SVG/chart approach (no heavy chart dependency).
- Non-goals for v1: user accounts, alerts, real-time quotes, intraday data, backtesting.
- Success criteria: daily job completes under Worker CPU/time limits on the free tier for ~200 tickers; query page filters/sorts return in under 1 second against D1.
