# Income Investor — Product Requirements Document

## 1. Overview

Income Investor helps a user build an investment portfolio aimed at steady income (dividends/distributions) rather than pure growth. It has three parts:

1. **Funds Universe Builder** — a background job that downloads and calculates data for a large list of tickers (stocks, ETFs, CEFs, REITs, MLPs) so the other two parts have something to work with.
2. **Portfolio Evaluator** — given a list of tickers and how much of the portfolio each one makes up (its "weight"), calculates a set of metrics describing that portfolio. Available both as a function other parts call, and as its own screen where a user can test any combination of tickers they choose.
3. **Portfolio Optimizer** — given the user's preferences (which metrics matter most, and whether risk tolerance should be conservative or aggressive), automatically searches for the best combination of tickers and weights.

This document is written for two audiences at once: an AI coding agent that will implement it literally, and a non-technical reader who wants to understand what's being built. Financial and technical jargon is defined the first time it's used, in the Glossary below.

## 2. Glossary

- **Ticker** — the short code for a tradable security, e.g. `AAPL`, `SPY`.
- **ETF / CEF / Mutual Fund** — three ways to pool many investments into one tradable ticker. An **ETF** (Exchange-Traded Fund) trades all day like a stock and its price stays very close to the value of what it holds. A **CEF** (Closed-End Fund) also trades all day like a stock, but its price can drift meaningfully away from the value of what it holds. A **Mutual Fund** only trades once a day, always exactly at the value of what it holds.
- **REIT / MLP** — a **REIT** (Real Estate Investment Trust) is a company that owns income-producing real estate. An **MLP** (Master Limited Partnership) is a business structure common in energy/pipeline companies; MLPs can add tax-filing complexity (see Part 3's tax notes).
- **Single Name ticker** — an individual company's stock (e.g. `AAPL`), as opposed to an ETF, CEF, REIT, or Mutual Fund which each hold many underlying investments.
- **Adjusted Close vs. Close** — "Close" is the actual price a security traded at. "Adjusted Close" is a recalculated version of past prices that already factors in dividends paid out, so a price chart made from it shows what your money would be worth if every dividend were reinvested.
- **Distribution** — a cash payment a ticker makes to holders (a dividend, for a stock or fund).
- **Total Return** — how much money you made on a ticker, combining price growth and distributions, expressed as a percentage.
- **Annualized Return** — a Total Return converted to a "per year" rate, so tickers with different amounts of price history can be compared fairly.
- **Distribution Yield** — the annual distributions a ticker pays, as a percentage of its price.
- **Beta** — how much a ticker tends to move compared to the overall market (here, the S&P 500, ticker `SPY`, used as "the market" throughout this document). Beta of 1.0 means it moves in step with the market; above 1.0 means bigger swings than the market; below 1.0 means smaller swings.
- **Correlation** — whether two things tend to move in the *same direction*, regardless of by how much. Ranges from -1 (always move opposite) to +1 (always move together). Unlike Beta, Correlation doesn't tell you the size of the move, only the direction relationship.
- **Sharpe Ratio** — how much return a portfolio earned for each unit of risk (volatility, defined below) it took on. Higher is better.
- **Sortino Ratio** — like Sharpe Ratio, but only counts the "bad" kind of volatility (downward moves) rather than all moves. Higher is better.
- **Volatility** — how much a value bounces around over time, measured statistically as a standard deviation.
- **Max Drawdown** — the biggest drop a portfolio has ever taken from a high point down to a low point, before recovering. Smaller (closer to zero) is better.
- **Distribution Lumpiness** — how uneven a portfolio's distribution payments are from year to year. Low lumpiness means steady, predictable income; high lumpiness means the amount varies a lot.
- **Cardinality constraint** — a rule limiting *how many* tickers can be in a portfolio (here: a minimum and maximum holding count).
- **Multi-objective optimization** — searching for the best answer when there are several, sometimes competing, goals at once (e.g., high income AND low risk).
- **Weighted-sum scalarization** — a way to handle multi-objective optimization by combining every goal into one overall score, using a weight to say how much each goal matters, then finding the single best-scoring answer.
- **Genetic Algorithm (GA)** — a search technique inspired by evolution: it keeps a pool of candidate solutions, combines and randomly tweaks the best ones over many rounds, and keeps improving until it converges on a strong answer. Well suited to problems like this one where the number of possible combinations is far too large to check exhaustively.

## 3. Tech Stack

- Frontend: Next.js with Shadcn components; social login (Google, GitHub, Facebook, etc.).
- Backend: Python, using libraries such as `yfinance`.
- Data sources: free sources wherever possible (`yfinance`, SEC EDGAR).
- Database: SQLite, used to persist all data.
- Deployment: Azure.
- Observability: OpenTelemetry, with detailed logging and exception capture.

## 4. Part 1 — Funds Universe Builder

### 4.1 What it does

Downloads price, distribution, and identity data for every candidate ticker, computes a set of derived metrics for each one, and stores the result as "the universe" that Parts 2 and 3 read from. An Administrator can view this universe in its own screen (see Part 7).

### 4.1.1 Candidate ticker list

The candidate ticker list — the raw pool of tickers considered before the §4.6 rejection rules narrow it down — is not a separately maintained, persisted list. It is downloaded fresh at the start of every refresh (§4.2):

- **Source:** Alpha Vantage's `LISTING_STATUS` endpoint (`https://www.alphavantage.co/query?function=LISTING_STATUS&apikey={key}`), which returns every US-listed stock and ETF along with its exchange, asset type, and listing status.
- **Filter to active listings only** — exclude any ticker whose status is not `Active` (e.g., delisted tickers) before fetching per-ticker data (§4.5).
- This endpoint distinguishes only Stock vs. ETF, not CEF/REIT/MLP — that finer classification is derived per-ticker from the data fetched in §4.5, not from this listing.
- **API key** is an admin-configurable setting (Admin Settings screen, §7), not hardcoded. Alpha Vantage's free `demo` key (shown above) is rate-limited to the point of being unusable for a full-universe download and must not be used in production.

### 4.2 Refresh triggers

The universe refresh runs two ways, both restricted to the **Administrator** role, in every environment (no separate developer-only mode):
- **On a schedule.** The cadence is an admin-configurable setting; default **weekly**.
- **On demand**, via a "Run Now" button on the Funds Universe Admin screen.

Completing a refresh **invalidates the Portfolio Optimizer's 24-hour result cache** (Part 3), since the underlying data changed.

### 4.3 All-or-nothing updates

A refresh must succeed completely or fail completely — never leave the universe partly updated. Use a database transaction (or equivalent) so a mid-run exception rolls back to the last good state instead of mixing fresh and stale data.

### 4.4 SPY — the benchmark

`SPY` (S&P 500) is used throughout this document as "the market" benchmark for Beta and Correlation. It is:
- **Force-fetched every refresh, unconditionally** — it is not a candidate holding, so it is never filtered out.
- **Exempt from the yield-floor rejection rule** (§4.6) and the **minimum-history rejection rule** (§4.6). SPY's own yield is typically around 1.3%, below the 3% floor applied to candidate tickers — without this exemption, an implementation could wrongly drop it.

For SPY, fetch: `shortName`, `longName`, `sector`, `industry`, and up to 5 years of monthly Adjusted Close price history. Compute its Total Return and Annualized Total Return the same way as for any other ticker (§4.7).

### 4.5 Per-ticker fields (all candidate tickers)

- `shortName`, `longName`, `sector`, `industry`.
- Whether the ticker is a Single Name stock, ETF, CEF, or Mutual Fund.
- Latest Adjusted Close price, and latest raw (unadjusted) Close price.
- Up to 5 years of **monthly** price history (both raw Close and Adjusted Close). If the ticker has less than 5 years of history, use whatever is available.
- **Raw monthly return series** (percentage change month-over-month, from raw Close): store this array itself, not just summary statistics — Part 3's optimizer needs to reuse it directly, on every candidate portfolio it evaluates, without recomputing it from scratch each time.
- Annual distribution totals for each full calendar year available (up to 5) — sum only full calendar years; a partial first or last year is excluded, per the ticker's actual distribution payment dates.

### 4.6 Rejection rules (candidate tickers only — SPY is exempt, §4.4)

- Reject if price history is shorter than 2 full calendar years.
- Reject if Distribution Yield (§4.7) is ≤ 3%.

### 4.7 Calculated fields and formulas

**Total Return** — price appreciation, measured **using Adjusted Close only** (its historical prices already factor in reinvested dividends), from the start to the end of the ticker's available history (up to 5 years). Do **not** separately add raw dividend dollars on top of this — Adjusted Close already contains that effect, and adding both double-counts the dividend benefit. (This corrects an approach that would otherwise inflate Total Return, worst for the exact high-yield tickers this product screens for.)

**Annualized Total Return** — converts Total Return to a fair "per year" rate, so tickers with different amounts of history can be compared and blended later without distortion:

```
Annualized Return = (1 + Total Return) ^ (1 / years) − 1
```

where `years` is however much history that ticker actually has (5, or less if newer). This is the field Part 2 uses whenever it needs to combine several tickers' returns into one portfolio-level number.

**Distribution Yield** — computed **independently** of Total Return, from **raw** (unadjusted) numbers only: sum of the ticker's annual distributions ÷ its raw (unadjusted) price. No inputs are shared between Total Return and Distribution Yield — this keeps the two metrics from overlapping.

**Ratio to SPY** — the ticker's Total Return ÷ SPY's Total Return, computed over the same time window (if the ticker is newer than 5 years, use its available window for both the ticker and SPY).

**Beta to SPY** — using monthly returns over the same window as above:
1. Compute each period's return for the ticker and for SPY.
2. Find each one's average return over the window.
3. Covariance: for each period, multiply (ticker's return − its average) by (SPY's return − its average), sum these, divide by (number of periods − 1).
4. Variance of SPY: for each period, square (SPY's return − its average), sum these, divide by (number of periods − 1).
5. `Beta = Covariance ÷ Variance of SPY`.

**Correlation to SPY** — the standard statistical correlation between the ticker's monthly returns and SPY's monthly returns, over the same window. (This is a straightforward two-series correlation at the single-ticker level — the aggregation issue described in Part 2 §5.4 only arises once multiple tickers are blended into one portfolio.)

## 5. Part 2 — Portfolio Evaluator

### 5.1 What it does

Given a list of tickers (all must belong to the Funds Universe) and a weight for each (must sum to exactly 100%), computes the 8 metrics below for that portfolio. It exists as:
- A **function**, called internally by the Portfolio Optimizer (Part 3) to score each candidate portfolio it considers.
- A **screen** (§5.5, new this revision) where a user manually types in their own tickers and weights to see how that specific portfolio scores.

### 5.2 Inputs and validation

- Every ticker must exist in the current Funds Universe.
- Weights must sum to exactly 100%.
- Validation is enforced in both the UI and the underlying Python function — never rely on the UI alone.

### 5.3 Metrics — final approved set

| Metric | Direction (locked) | Used in Evaluator | Used in Optimizer (weighted objective) |
|---|---|---|---|
| Total Return (annualized) | Maximize | Yes | Yes |
| Distribution Yield | Maximize | Yes | Yes |
| Beta to SPY | Minimize | Yes | Yes |
| Correlation to SPY | Minimize | Yes | Yes |
| Sharpe Ratio | Maximize | Yes | Yes |
| Sortino Ratio | Maximize | Yes | Yes |
| Max Drawdown | Minimize | Yes | Yes |
| Distribution Lumpiness | Minimize (less wobble = better) | Yes | Yes |

Why each direction is locked the way it is: more growth and income is better (Total Return, Distribution Yield); less market-driven swing and less lock-step movement with the market is better for an income-focused portfolio (Beta, Correlation); more return per unit of risk taken is better, whether measured against overall risk or just downside risk (Sharpe, Sortino); a smaller worst-case historical loss is better (Max Drawdown); and steadier, more predictable income is better (Distribution Lumpiness).

The **direction of every metric is locked** — it is not a user choice. A user only sets how much each metric matters (§6.2). This is a deliberate simplification versus letting users pick any direction: it prevents, for example, a novice accidentally telling the system to *maximize* Beta and unknowingly building a needlessly volatile portfolio, and it matches the fact that a sound income-focused portfolio virtually never wants the opposite of these defaults.

**Expense Ratio** (the fund's annual fee) was considered as a possible tenth metric and explicitly declined — it is not part of this product.

### 5.4 Calculation methods

Three metrics are simple **weighted averages** of each holding's own value — valid because these particular numbers combine linearly:
- **Portfolio Total Return** = weighted average of each holding's Annualized Total Return (§4.7).
- **Portfolio Distribution Yield** = weighted average of each holding's Distribution Yield (§4.7).
- **Portfolio Beta** = weighted average of each holding's Beta to SPY (§4.7).

The remaining five metrics must instead be built from the **portfolio's own combined series** first, and only then measured — a plain weighted average of each holding's individual value is mathematically wrong for these, the same way you cannot average five people's individual "how tall are the tallest and shortest of your friends" answers to learn the tallest and shortest of the whole group. All five share one rule for handling holdings with different amounts of available history:

> **Common-history rule:** build the series only over the calendar period that every currently-selected holding actually has data for (i.e., truncate to whichever selected holding has the shortest history). This keeps the blended series honest — mixing a 5-year return with a 2-year return, or a 5-year distribution history with a 1-year one, would otherwise distort the result.

- **Portfolio Correlation to SPY** — build the portfolio's own monthly return series (weighted sum of each holding's monthly return, using the portfolio's weights, over the common-history window), then compute the correlation of *that single series* against SPY's monthly returns over the same window. **This is not a weighted average of each holding's individual Correlation to SPY** — that approach is the one place in this document where the usual "just weight-average it like Beta" pattern breaks down mathematically, and must not be used.
- **Portfolio Sharpe Ratio** = (portfolio's arithmetic annualized return − risk-free rate) ÷ (portfolio's annualized volatility). Arithmetic annualized return here means the average of the portfolio's own monthly returns (built the same way as for Correlation, above) × 12 — **not** the CAGR-style Annualized Total Return used in §5.4's weighted averages; the two use different math and must be kept separate. Annualized volatility = standard deviation of the portfolio's monthly returns × √12. The risk-free rate is an admin-configurable flat annual percentage, defaulting to **4%**, managed on the Admin Settings screen (§7) rather than a live data feed.
- **Portfolio Sortino Ratio** = same formula as Sharpe, but the denominator only uses the standard deviation of the portfolio's *negative* monthly returns (downside deviation), annualized the same way.
- **Portfolio Max Drawdown** = the largest peak-to-trough decline in the portfolio's own cumulative return series (built from the same monthly return series as Correlation and Sharpe/Sortino, over the common-history window): track the running high point of cumulative value, and find the worst percentage drop from any running high to a later low.
- **Portfolio Distribution Lumpiness** = the coefficient of variation (standard deviation ÷ average) of the portfolio's own blended annual distribution series — the weighted sum of each holding's annual distribution amount per calendar year, over the common-history window. **Minimum data guard:** this requires at least 2 full calendar years of shared distribution history; if the common-history window has fewer, report this metric as "not enough history" rather than a misleading value (a single data point always shows zero variation, which would falsely look "perfectly steady").

### 5.5 Manual-entry screen (new)

A dedicated screen where a user types in any tickers (must belong to the Funds Universe) and weights (must sum to 100%) — **no minimum or maximum holding count applies here** (that rule is specific to the Optimizer's diversification requirement, Part 3; this screen is a "check anything" tool and only requires at least 1 ticker). It calls the same Portfolio Evaluator function described above and additionally must:

- Explain, in plain language, what each of the 8 metrics means, its typical value range, and any taxable-vs-retirement-account implication (same content as the Optimizer's tooltips, §6.8, extended with typical ranges here).
- **Color-code** each metric's value on a red-to-green gradient where a widely recognized good/bad range exists, interpolating smoothly between the anchor points below; metrics without an agreed universal range are shown with no color, just the value and its explanation. Implement the thresholds as an adjustable configuration table, not hardcoded, so they can be tuned later without a redesign:

| Metric | Green anchor | Yellow anchor | Red anchor |
|---|---|---|---|
| Beta | ≤ 0.7 | ~1.0 | ≥ 1.3 |
| Correlation to SPY | ≤ 0.3 | ~0.6 | ≥ 0.85 |
| Sharpe Ratio | > 2 (deep green) | 0 – 1 | < 0 |
| Sortino Ratio | same anchors as Sharpe (an approximation, not a distinct standard) | | |
| Max Drawdown | < 10% | 10 – 20% | > 20% |
| Total Return, Distribution Yield, Distribution Lumpiness | *(no fixed band — shown neutral)* | | |

- Allow the user to name and **save** a manually-built portfolio, using the same save mechanism the Optimizer uses (§6.6) — it then appears in the Portfolio Listing screen (§7) alongside optimizer-generated portfolios.

## 6. Part 3 — Portfolio Optimizer

### 6.1 What it does

Starting from the Funds Universe, filtered to the user's preferences, searches for the single best combination of tickers and weights according to the goals the user sets, then hands that combination to the Portfolio Evaluator (Part 2) to produce the final reported metrics. Available as a function and a screen.

### 6.2 Objective setup

For each of the 8 metrics (§5.3), the user picks only an **importance**: Low, Medium, or High, mapped internally to a numeric weight of **1, 2, or 3** respectively. Direction (minimize/maximize) is locked per metric and is not shown as a separate control (§5.3). Example: a retiree wanting a conservative, income-focused portfolio might set Correlation = Medium, Beta = Medium, Total Return = Low, Distribution Yield = High, and leave the rest at a default low importance.

**Note on overlap:** several of these 8 objectives are not fully independent — Sharpe and Sortino are themselves derived from return and volatility, which also drive Total Return, Beta, and Correlation. Setting several of these to High at once compounds related effects rather than pulling in 8 truly separate directions. This isn't a flaw, just something worth knowing when picking weights.

### 6.3 Optimization technique: Genetic Algorithm with weighted-sum scoring

This is how "multi-objective weighted optimization" is implemented:

1. **Combine all 8 goals into one score (weighted-sum scalarization).** For a candidate portfolio, each metric's value is first normalized: rescaled to a common 0–1 scale using percentile-based min-max normalization, where the population's 5th-percentile value maps to 0 and its 95th-percentile value maps to 1, with anything outside that range clipped to 0 or 1. This trims a single outlier ticker from skewing the whole scale. It is recomputed once per optimization run (not per GA generation) and is kept separate from the fixed reference bands used for the UI's color-coding in §5.5, since those are calibrated for human reading, not for fairly ranking candidates.

   The **population** the percentiles are measured against depends on the metric:
   - **Total Return, Distribution Yield, Beta to SPY, Correlation to SPY** — these already exist as per-ticker fields (§4.7); the population is every candidate ticker's own value, across the current filtered universe.
   - **Sharpe Ratio, Sortino Ratio, Max Drawdown, Distribution Lumpiness** — these only exist at the portfolio level (§5.4), so there is no per-ticker field to draw a population from. Instead, run every candidate ticker in the current filtered universe through the Portfolio Evaluator (Part 2) individually, as its own single-holding, 100%-weight portfolio, and use the resulting distribution of that metric across all those single-ticker "portfolios" as the population.

   Each normalized value is then multiplied by its metric's locked direction (+1 to maximize, −1 to minimize) and its importance weight (1/2/3). The fitness score is the sum of these 8 terms.
2. **Search for the best-scoring portfolio using a Genetic Algorithm.** A candidate portfolio is represented as: which tickers are selected (respecting the minimum/maximum holding count and the Single Name include/exclude choice, §6.4) plus a weight for each selected ticker. The GA keeps a population of candidate portfolios, combines pairs of promising ones (crossover) and randomly tweaks others (mutation), then repairs the result so weights still sum to 100% before scoring it via the Portfolio Evaluator (Part 2) and keeping the fittest for the next round. This repeats until the population converges on a strong answer. A standard, freely available Python GA library (e.g. DEAP or PyGAD) is a reasonable implementation choice; population size and number of rounds should be tuned empirically to keep the wait after clicking "Optimize" reasonable, not hardcoded to an arbitrary value.

**Why a Genetic Algorithm with one blended score, instead of finding a whole set of trade-off portfolios (an approach called a Pareto frontier):** this product asks the user for one specific weighting up front and shows one resulting portfolio — it doesn't ask the user to browse and choose among many trade-off options. A Genetic Algorithm searching directly for the single portfolio matching that one weighting gets there with meaningfully fewer evaluation steps than building an entire frontier of alternatives first and picking one out of it, which matters since every evaluation step calls the Portfolio Evaluator and is not free. This is also the standard approach in the finance literature for this category of problem — picking a fixed number of holdings out of a much larger list, plus a weight for each, is a well-studied problem type (a "cardinality-constrained portfolio" search) known to be too large to solve by brute force, and Genetic Algorithms have been the standard heuristic for it since Chang, Meade, Beasley & Sharaiha (2000), still an active research approach today.

One honest trade-off: combining goals into a single blended score (rather than exploring a full frontier) can in theory miss an unusual trade-off portfolio that no single weighting could ever produce. For the 8 smoothly-behaving metrics here, this risk is low, and is accepted for the simplicity and speed it buys.

### 6.4 Constraints

- **Minimum / maximum holdings** — user-specified, defaulting to 10 and 25. The Genetic Algorithm must respect these bounds when selecting tickers.
- If the user's filters (including the Single Name toggle below) leave **fewer tickers available than the minimum holding count**, the Optimizer must **block and show a validation error** — it must not run anyway or silently lower the minimum.
- **Single Name toggle** — the user may choose to include or exclude Single Name tickers (individual stocks) from consideration.
- All inputs are required unless stated otherwise, validated in both the UI and the Python functions.

### 6.5 Caching

Running the Optimizer with **exactly the same criteria** returns a cached result for 24 hours. "Exactly the same criteria" means an identical combination of: the current Funds Universe version, all 8 objective importance weights, minimum/maximum holdings, and the Single Name toggle — used together as the cache key. A Funds Universe refresh (§4.2) invalidates this cache immediately, since a refresh means the underlying data changed.

### 6.6 Saving and canned scenarios

- A user can name and save a portfolio's criteria together with its results (e.g., "Andy's Taxable Portfolio").
- The screen offers a few "canned" preset buttons that pre-fill criteria for common scenarios and immediately run the optimization. The four presets, and the exact importance level each sets for all 8 objectives plus the two constraints:

| Metric | Conservative Income | Balanced Income (default) | Growth & Income | Maximum Yield |
|---|---|---|---|---|
| Total Return | Low | Medium | High | Low |
| Distribution Yield | High | Medium | Medium | High |
| Beta to SPY | High | Medium | Low | Low |
| Correlation to SPY | High | Medium | Low | Low |
| Sharpe Ratio | Medium | Medium | Medium | Low |
| Sortino Ratio | High | Medium | Medium | Low |
| Max Drawdown | High | Medium | Low | Low |
| Distribution Lumpiness | High | Medium | Low | Medium |
| Minimum holdings | 15 | 10 | 10 | 10 |
| Maximum holdings | 25 | 25 | 20 | 15 |
| Single Name toggle | Excluded | Included | Included | Excluded |

  Rationale: **Conservative Income** targets a retiree-style, capital-preservation portfolio (the §6.2 example) — wide diversification, no single-stock risk. **Balanced Income** mirrors the system's own §6.4 defaults (10/25 holdings, all Medium) as a neutral starting point. **Growth & Income** suits a younger investor tilting toward Total Return while accepting more Beta/drawdown. **Maximum Yield** chases Distribution Yield hard with a concentrated (10–15 holding) portfolio of yield-structured vehicles (CEFs/REITs/MLPs), excluding Single Name since ordinary stocks rarely clear a high-yield screen.
- The Portfolio Listing screen (§7) shows every saved portfolio, whether it came from the Optimizer or was manually built and saved via §5.5.

### 6.7 Calculating units to invest

Once a portfolio is shown, the user can enter a dollar amount and click "Calculate Units to Invest." Using each ticker's portfolio weight and latest Adjusted Close price, compute the number of shares for each ticker, **rounded to the nearest whole share**. Because whole-share rounding rarely lands on the exact amount entered, display the **actual total dollars invested** after rounding alongside the amount the user typed in.

### 6.8 Tooltips

Every objective on this screen needs a tooltip, in plain language, covering:
1. What the metric means.
2. Its locked direction (minimize or maximize) and a one-line reason why.
3. Any special implication for a **taxable** account versus a **tax-advantaged retirement account** (IRA, 401(k), etc.):
   - **Distribution Yield & Distribution Lumpiness** — in a taxable account, distributions are generally taxed in the year received, even if automatically reinvested (at ordinary or qualified-dividend rates; CEF and MLP distributions can add complexity, such as return-of-capital treatment or a K-1 tax form). In a retirement account, there's no tax event until money is withdrawn. A high-income strategy generally means a bigger yearly tax bill if held in a taxable account.
   - **Total Return** — the price-growth portion isn't taxed until the position is sold (as a capital gain) in a taxable account; the distribution portion is taxed as described above. Inside a retirement account, none of this is taxed until withdrawal.
   - **Max Drawdown** — no direct tax effect, but worth knowing: a drawdown in a taxable account can be an opportunity for tax-loss harvesting (selling at a loss to offset other gains) — an option that doesn't exist inside a retirement account.
   - **Beta, Correlation, Sharpe Ratio, Sortino Ratio** — these are risk measures with no direct difference in tax treatment between account types; the tooltip should say so plainly rather than leave the question unanswered.

## 7. Screens

- **Home** — the only page open to anonymous visitors; marketing content describing the product.
- **Funds Universe Admin screen** (Administrator only) — every ticker in the universe with all identity and calculated fields, filterable/sortable/pageable; shows date and duration of the last refresh; includes the refresh schedule setting and a "Run Now" button (§4.2).
- **Admin Settings screen** (Administrator only) — a general-purpose screen for admin-configurable values, built to hold more settings over time. Currently holds two settings: the risk-free rate (§5.4), defaulting to **4%**, and the Alpha Vantage API key (§4.1.1).
- **Portfolio Evaluator screen** — the manual-entry tool described in §5.5.
- **Portfolio Optimizer screen** — the objective/weight/constraint controls and results described in Part 3.
- **Portfolio Listing screen** — every saved portfolio (from either the Optimizer or the Evaluator), showing the same columns available on the Funds Universe screen for each of its holdings, with the same tooltips on computed columns; supports view, re-run, and delete actions. May share common components with the Funds Universe Admin screen's list view.

## 8. Non-functional requirements

- Authentication via social login (Google, GitHub, Facebook, etc.); only the Home page allows anonymous access — everything else requires signing in.
- An **Administrator** role, required for Funds Universe refreshes (manual and scheduled) and for access to the Admin Settings screen (§7).
- SQLite for all persisted data.
- OpenTelemetry for logging, exception capture, and usage metrics (daily/weekly/monthly visits, sign-ins, portfolios constructed).
- **Tax/financial-advice disclaimer** — since this product now shows specific tax-treatment guidance (§6.8, §5.5), display a visible "this is general education, not tax or investment advice — consult a professional" notice on every screen that shows this content (the Optimizer screen and the Evaluator screen).