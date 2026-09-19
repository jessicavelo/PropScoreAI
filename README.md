# PropScore AI

Scores commercial properties in Atherton, Los Altos, Menlo Park and Palo Alto as
townhome-redevelopment opportunities. Blends four layers into one score:
Buildability (35%), Visual Condition (25%), Distressed Signal (25%),
Business Value (15%).

## Layout

| Path | Purpose |
|---|---|
| `build_regional_master.py` | Merges PropertyRadar, LandVision, PropStream and Google Maps exports into one cleaned master per city |
| `build_zoning_gp_lookup.py` | Current zoning + General Plan designation per APN from city GIS layers |
| `combined_opportunity_scoring/build_report.py` | Scores every property, fetches Street View, runs Gemini vision, writes the HTML report + CSV |
| `combined_opportunity_scoring/config.py` | Weights, thresholds, zoning tables, entitled-pipeline list |
| `PropScoreAI/functions/_middleware.js` | Password login for the Cloudflare Pages deploy |
| `BUSINESS_DATA_IDEAS.md` | Parked backlog of data improvements |

## Not in this repo (by design)

Source data exports (licensed), all outputs (they contain owner names, phones
and emails), Street View images, GIS files and `.env`. See `.gitignore`.

## Running

1. Copy `.env.example` to `combined_opportunity_scoring/.env` and add keys.
2. Place the source exports in the project root, then:
   ```
   python build_regional_master.py
   python build_zoning_gp_lookup.py
   cd combined_opportunity_scoring && python build_report.py
   ```
3. Copy `combined_opportunity_report.html` (as `index.html`) and the referenced
   `images/` into `PropScoreAI/`, then `npx wrangler pages deploy .` from there.
