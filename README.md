# SA Power Networks for Home Assistant

Home Assistant custom integration for importing SA Power Networks meter data into Recorder long-term statistics.

_Disclaimer: This integration is neither created nor supported by SA Power Networks._

## Overview

This integration logs in to the SA Power Networks customer portal, fetches interval and accumulated meter data, and writes it into Home Assistant Recorder as external statistics.

The imported statistics can be used in:

- Energy Dashboard
- Statistics Graph cards
- Any Lovelace card that supports long-term statistics

This project is cloud polling and uses the UI config flow (username and password).

## What The Integration Creates

After setup, the integration creates:

- One device named SA Power Networks
- Status sensors:

  | Name                         | Description                                                                                          |
  | ---------------------------- | ---------------------------------------------------------------------------------------------------- |
  | Authentication Status        | Whether portal authentication is currently valid for data sync operations.                           |
  | NMI Count                    | Number of NMIs (meter identifiers) discovered for the configured account.                            |
  | Rows Imported                | Total number of data rows imported into Recorder across interval, accumulated, and combined streams. |
  | Interval Rows Imported       | Number of interval (detailed) rows imported into Recorder statistics.                                |
  | Accumulated Periods Imported | Number of accumulated summary periods imported into Recorder statistics.                             |
  | Channels Imported            | Number of unique statistic channels/streams imported.                                                |
  | Feed Lag                     | Estimated lag, in hours, between current time and the latest imported reading.                       |
  | Last Error                   | Most recent sync or processing error message, if any.                                                |
  | Last Successful Sync         | Timestamp of the most recent successful import/sync cycle.                                           |

- One button entity:
  - Refresh Meter Data
- One service:
  - sapowernetworks.refresh

Important: this integration currently imports statistics to Recorder and does not create dedicated per-channel energy sensor entities for every stream.

## How Data Is Imported

- Update cadence: twice daily by default (around 00:30 local Home Assistant time and 10:00 UTC)
- Manual refresh: use the Refresh Meter Data button or call sapowernetworks.refresh
- Historical backfill: starts from 2000-01-01 for first import
- Data stream meanings:

  | Stream      | Typical meter type                                                | Meaning                                                                                                                                                                                           |
  | ----------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | Interval    | Smart meter (interval-capable)                                    | Detailed NEM12 interval readings (for example, 5/15/30-minute source intervals) grouped into hourly Recorder statistics per channel.                                                              |
  | Accumulated | Non-smart/accumulation meter (or summary-only feed)               | Summary CSV period totals imported as period-based statistics (one point per summary period end date) for accumulated import/export channels.                                                     |
  | Combined    | Handover from accumulated history to interval smart-meter history | A single import timeline that stitches accumulated import points to interval import points when they are consecutive (interval starts at or after the accumulated end, with at most a 1-day gap). |

- Combined stream notes:
  - Combined is import-focused (Combined Import) and is used to give one continuous import history across stream handoff.
  - If a combined stream already exists, new interval import points can continue that stream on later syncs.

Statistic display names begin with SA Power Networks and include direction labels such as Import, Export, Accumulated Import, Accumulated Export, and Combined Import.

Examples of what you might see:

| Stream                    | Example display name in Home Assistant              | Example statistic_id pattern                    |
| ------------------------- | --------------------------------------------------- | ----------------------------------------------- |
| Interval (import channel) | SA Power Networks Import **\*\*\***5678 E1          | sapowernetworks:abc123def456_import_e1          |
| Interval (export channel) | SA Power Networks Export **\*\*\***5678 B1          | sapowernetworks:abc123def456_export_b1          |
| Accumulated (import)      | SA Power Networks Accumulated Import **\*\*\***5678 | sapowernetworks:abc123def456_accumulated_import |
| Accumulated (export)      | SA Power Networks Accumulated Export **\*\*\***5678 | sapowernetworks:abc123def456_accumulated_export |
| Combined                  | SA Power Networks Combined Import **\*\*\***5678    | sapowernetworks:abc123def456_combined_import    |

Notes:

- The masked number is the NMI with only the last 4 digits visible.
- The 12-character segment in statistic_id (abc123def456 above) is a deterministic hash of the NMI, so your value will differ.

## Requirements

- Home Assistant with Recorder enabled
- Valid SA Power Networks portal credentials
- Network access from Home Assistant to customer.portal.sapowernetworks.com.au

Recorder is required because this integration writes external statistics directly to the statistics database.

## Installation

### Option 1: HACS (recommended)

1. Open HACS in Home Assistant.
2. Add this repository as a custom repository (category: Integration).
3. Install SA Power Networks.
4. Restart Home Assistant.

### Option 2: Manual install

1. Copy custom_components/sapowernetworks into your Home Assistant config custom_components directory.
2. Restart Home Assistant.

## Configuration

1. Go to Settings -> Devices & Services -> Add Integration.
2. Search for SA Power Networks.
3. Enter your portal username and password.
4. Complete setup.

No YAML configuration is required.

## Using The Imported Statistics

Before configuring dashboard cards, verify import health first:

1. Open the SA Power Networks status sensors.
2. Confirm Rows Imported is greater than 0.
3. Confirm Channels Imported is greater than 0.
4. Check Last Error is empty or stable.

### Energy Dashboard

1. Open Settings -> Dashboards -> Energy.
2. Add or edit grid consumption / return-to-grid sources.
3. Select statistics whose names begin with SA Power Networks.
4. Use Import for consumption and Export for feed-in, where available.

### Statistics Graph Cards

Useful graph modes:

- change: usually best for usage over time
- sum: useful for cumulative-style graphs
- state: raw bucket value per point

Because Recorder stores long-term statistics in hourly buckets, interval source data is visible as hourly points in these views.

## Troubleshooting

### Login works in config flow but setup later fails

The integration now attempts to reuse an already-authenticated Home Assistant HTTP session before doing another login POST. If this still fails, check Home Assistant logs for:

- HTTP 401/403: credentials rejected
- HTTP 503: portal unavailable or maintenance page
- Portal shape/parse errors after portal changes

### No statistics appear in Energy Dashboard

- Check Rows Imported and Channels Imported first
- Trigger a manual refresh using the button or service
- Confirm Recorder is enabled and healthy
- Wait for first backfill/import to complete

### Last Error has content

Open Home Assistant logs and diagnostics for the config entry. Diagnostics redact sensitive fields such as credentials and account identifiers.

## Privacy And Diagnostics

- Diagnostics output is redacted
- Sensitive fields (passwords, tokens, usernames, email-like text, account identifiers) are masked/redacted
- Statistic IDs are generated in a privacy-safe deterministic format

## Development

Repository layout:

- Integration code: custom_components/sapowernetworks
- Tests: tests
- Local dev config: config

Useful commands from repository root:

- Install dependencies: scripts/setup
- Start local Home Assistant dev instance: scripts/develop
- Lint and auto-fix: scripts/lint
- Run tests: scripts/test

## Known Limitations

- No tariff/cost modeling is generated by this integration
- No dedicated per-stream energy entities beyond integration status and refresh button
- Visibility in UI depends on successful Recorder imports
