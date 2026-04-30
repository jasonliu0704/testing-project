# AI Agent Design Document
## Real Estate Market Intelligence → Video Infographic Pipeline

**Version:** 1.0  
**Date:** April 27, 2026  
**Status:** Design Phase  
**Owner:** Real Estate Marketing Automation Team

---

example output reference: 
https://www.instagram.com/keepingcurrentmatters/

# PRD: Hyper-Local Market Data → Voiceover → Video Engine

## 1. Product Overview

### Problem

Real estate agents need consistent, localized market-update content that explains what is happening in their market, but most agents do not have the time, data literacy, or production workflow to turn housing metrics into polished short-form videos.

Existing products can automate posting and branding, but many outputs feel templated. The opportunity is to build a pipeline that converts structured local market data into explainable, branded, high-retention videos that make the agent sound like a local economic authority.

### Solution

Build an AI-powered pipeline that ingests local housing market data, identifies meaningful changes, generates a consumer-friendly narrative, creates data visualizations, produces voiceover, composes a 30–60 second short-form video, and prepares the asset for social distribution.

Example outputs:

- “Inventory is up 18% in Mountain View — here is what that means for buyers.”
- “Homes are taking 9 days longer to sell in Santa Clara County.”
- “Prices are flat, but negotiation power is shifting.”
- “The market is not crashing; it is normalizing.”

---

## 2. Product Goals

### Primary Goals

- Help real estate agents publish local market-update videos daily or weekly.
- Translate raw market metrics into clear buyer/seller implications.
- Make market content feel specific, data-backed, and visually credible.
- Reduce video creation time from hours to minutes.

### Success Metrics

| Metric | Target |
|---|---:|
| Time from data ingest to preview video | < 3 minutes |
| Weekly videos per agent | 3–7 |
| Script factual validation pass rate | > 98% |
| Chart/data consistency pass rate | > 99% |
| Agent approval rate without manual edits | > 70% |
| Published videos per active agent per month | > 12 |
| Average video completion rate | > 35% |

---

## 3. User Persona

### Primary User

Real estate agents, teams, and brokerages who operate in a specific city, neighborhood, zip code, county, or metro area.

### Core Job To Be Done

> “Turn the latest housing market numbers for my local area into a short, trustworthy video that my buyers and sellers can understand.”

### User Needs

- Show expertise without manually analyzing spreadsheets.
- Explain whether the market favors buyers, sellers, or balanced negotiation.
- Post local content consistently across Instagram Reels, TikTok, YouTube Shorts, Facebook, and LinkedIn.
- Avoid misleading, overly bearish, overly bullish, or unsupported claims.

---

## 4. Scope

### Included In This PRD

- Local housing data ingestion.
- Metric normalization and time-window comparison.
- Market signal detection.
- Buyer/seller narrative generation.
- Chart and infographic generation.
- Voiceover generation.
- Branded short-form video composition.
- Human review and approval workflow.

### Out Of Scope For V1

- Local news article ingestion.
- Paid ad campaign creation.
- Lead nurture workflows.
- CRM follow-up automation.
- MLS listing-video generation for a specific property.
- Predictive pricing recommendations for individual homes.

---

## 5. Data Sources

### 5.1 Primary Data Sources

| Source Type | Examples | Usage |
|---|---|---|
| MLS / brokerage feed | MLS Grid, Bridge Interactive, local MLS exports | Best local source when available |
| Public brokerage data | Redfin Data Center, brokerage market pages | City, neighborhood, metro, county trends |
| REALTOR association data | NAR, state/local REALTOR associations | National, regional, metro context |
| Mortgage-rate data | Freddie Mac PMMS, Mortgage News Daily, lender APIs | Affordability context |
| Census / economic data | Census ACS, BLS, FRED | Optional macro context |
| Internal agent data | Recent buyer/seller activity, local listings | Optional agent-specific examples |

### 5.2 Required Metrics

| Metric | Required | Notes |
|---|---|---|
| Median sale price | Yes | Main price trend signal |
| Active listings / inventory | Yes | Supply signal |
| New listings | Yes | Seller activity signal |
| Homes sold / closed sales | Yes | Demand signal |
| Pending sales | Preferred | Forward demand signal |
| Median days on market | Yes | Speed / competition signal |
| Sale-to-list ratio | Preferred | Negotiation signal |
| Price reductions | Preferred | Seller flexibility signal |
| Months of supply | Preferred | Market balance signal |
| Mortgage rate | Yes | Affordability context |

### 5.3 Geographic Levels

The pipeline should support:

- Zip code
- City
- Neighborhood
- County
- Metro area
- State
- National benchmark

V1 should support city + county + metro first. Zip/neighborhood support is valuable but should depend on source availability and sample-size thresholds.

---

## 6. System Architecture

```text
[Market Data Sources]
   ↓
[Data Ingestion + Normalization]
   ↓
[Metric Store + Time Series Store]
   ↓
[Market Signal Engine]
   ↓
[Narrative Insight Engine]
   ↓
[Script Generator]
   ↓
[Chart + Visual Generator]
   ↓
[Voice Generator]
   ↓
[Video Composer]
   ↓
[Review + Distribution]
Success depends on reliable data sources, accurate LLM-based analysis, efficient video rendering, and continuous feedback loops to improve content performance over time.
