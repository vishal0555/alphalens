# AlphaLens — Analyst-Facing Dashboard

## Mandate

AlphaLens is the analyst's window into the fund. Mobile-first,
read-only, hosted on Vercel so it loads instantly anywhere — the
single URL an analyst opens to see today's NAV, today's posture,
today's universe diff, today's fills, and the lessons accumulating
from past sessions. AlphaLens is intentionally not a control surface:
no buttons that fire trades, no overrides. It's the way the team
*sees* the fund.

In hedge-fund vocabulary: this is the front-office analyst dashboard
that sits between the trading floor's real-time monitors and the PMs'
weekly committee deck. Distinct from the internal Console (which is
ops-shaped, on the cluster, port 5002); AlphaLens is the externally
hosted, password-gated, analyst-mobile view.

## What we run today

| View | Route | Purpose |
|------|-------|---------|
| Dashboard | `/` | NAV, posture, universe tiles, current positions, recent lessons |
| Universe | `/universe` | Today's picks with diff vs yesterday |
| Morning | `/morning` | Per-pick playbooks (entry / stop / target / action) |
| Positions | `/positions` | Today's filled / exited / no-fill items with realised PnL |
| Debriefs | `/debriefs` | List of recent debriefs |
| Debrief detail | `/debrief/<id>` | Single retrospective with extracted lessons |
| NAV | `/nav` | NAV history table + sparkline |

Stack: Flask + Jinja templates, deployed to Vercel; auth via single
`AUTH_PASSWORD` env. Reads directly from Neon — same tables Research,
PM, Trading, Analytics, and Ops write to.

## How we collaborate

| Counterparty | Their need from us | Our need from them | Contract |
|--------------|--------------------|--------------------|----------|
| **Analytics (Console)** | Avoid duplicating views | None directly | We're the consumer view; Console is the engineer view. Both read the same tables. |
| **Research** | Surface universes + briefings legibly | A stable `universes` + `fund_briefings` shape | Read only |
| **PM** | Surface plans + plan_items + their playbook context | Stable `pm_plans` + `pm_plan_items` shape | Read only |
| **Trading** | Surface fills + exits with PnL | `executed_trades` schema | Read only |
| **Analytics (Debriefer)** | Surface debriefs + lessons | `debriefs` + `lessons` shape | Read only |
| **Ops** | NAV history view | `nav_marks` shape | Read only |

We never write. If a column we surface changes shape, the owning team
gives us a heads-up — but we don't gate their schema changes.

## Roadmap to leading-fund maturity

A peer fund's analyst dashboard does these; we have the basics.

1. **Mobile-first read-only fund view** ✅ Today.
2. **Single-password auth** ✅ Acceptable for a single-team prototype.
3. **Per-analyst views / saved filters** ❌ Today: one view per page.
   A peer fund's dashboard remembers the analyst's watchlist,
   highlighted sectors, hidden columns.
4. **Real-time updates** ⚠️ Page-load refresh today. SSE / websockets
   for live PnL + fills.
5. **Sharing / commenting** ❌ "Analyst A flagged NVDA in today's
   briefing" — no in-product collaboration.
6. **Investor reporting variant** ❌ When external capital arrives,
   the dashboard gets a redacted public variant for LPs (no
   per-position detail, aggregated by sector / theme).
7. **Mobile push notifications** ❌ "NAV breached -2% intraday";
   today the analyst has to check the dashboard.
8. **Multi-fund support** ❌ Today: one fund. A peer shop runs N
   funds; the dashboard gates by user / strategy.
9. **Read-only by enforcement** ⚠️ Today: by convention (no POST
   routes). Add Postgres RBAC so the AlphaLens role can't write
   even if the code had a bug.

Adjacent gaps:
- **Performance attribution view.** Mentioned in Analytics charter;
  AlphaLens is where it lands once the engine exists.
- **Risk dashboard view.** Same — once Risk has VaR / stress /
  exposure metrics, AlphaLens surfaces them.
- **Historical replay.** "Show me the dashboard as of 2026-03-15 at
  market close" — a vintaged view. Becomes natural once `dataflow`
  silver is the data source (bi-temporal queries fall out).
- **External-vendor data overlay.** When Bloomberg / Refinitiv land,
  AlphaLens overlays vendor data on internal views (consensus
  estimates next to internal playbook targets).

## Strategic gaps and open questions

- **Hosting choice.** Vercel works; it also means our data leaves the
  internal network on every page load. Fine for a single-team
  prototype; needs review for a regulated context.
- **Latency to truth.** AlphaLens reads directly from Neon. Under
  load, that could thrash the same DB the agent fleet writes to. A
  read replica or a per-page cache becomes interesting at scale.
- **Mobile vs desktop split.** Today: mobile-first. Analysts at a
  real fund spend desk time on multi-monitor desktop. The next
  iteration probably has a desktop variant with more density.
- **Authentication.** Single password is fine internally. SSO
  becomes mandatory when external users (LPs, auditors) need
  scoped access.
