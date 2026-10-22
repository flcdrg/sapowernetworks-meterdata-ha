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
- NEM12 interval timestamps and summary period-end dates are interpreted as AEST
  (UTC+10, without daylight-saving adjustment) and converted to UTC before import.
- Energy readings declared as Wh, kWh, or MWh are normalized to kWh. Unsupported
  units fail the sync rather than being imported with an incorrect kWh label.
- Data stream meanings:

  | Stream      | Typical meter type                                                | Meaning                                                                                                                                                                                           |
  | ----------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
  | Interval    | Smart meter (interval-capable)                                    | Detailed NEM12 interval readings (for example, 5/15/30-minute source intervals) grouped into fully covered hourly Recorder statistics per channel.                                                  |
  | Accumulated | Non-smart/accumulation meter (or summary-only feed)               | Summary CSV period totals imported as period-based statistics (one point per summary period end date) for accumulated import/export channels.                                                     |
  | Combined    | Handover from accumulated history to interval smart-meter history | A single import timeline that stitches accumulated import points to interval import points when they are consecutive (interval starts at or after the accumulated end, with at most a 1-day gap). |

- Combined stream notes:
  - Combined is import-focused (Combined Import) and is used to give one continuous import history across stream handoff.
  - If a combined stream already exists, new interval import points can continue that stream on later syncs.
  - When interval and accumulated statistics already exist but the combined stream
    does not, the first combined import re-fetches available source history to build
    its baseline. This can require a historical portal download.
  - Completed historical initialization attempts that find no consecutive streams
    or usable history are remembered across refreshes and restarts. The full backfill
    is tried again only when the candidate source statistic IDs change; ordinary
    incremental imports continue. Failed downloads remain retryable.
- Partial hours are deferred until a later sync rather than imported as hourly subtotals.
- Meters identified by the portal as Accumulated skip detailed interval downloads
  unless interval or combined statistics already exist. Interval and unknown meter
  types remain eligible for interval backfill, including after a meter handover.

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

### Existing installations and the timezone correction

**Who this applies to:** installations that imported interval or accumulated
statistics before upgrading to a release that interprets NEM12 timestamps and summary
period-end dates as AEST. Fresh installations, and installations that have not yet
imported meter data, do not need this cleanup.

Existing Recorder statistics are not shifted or rebuilt automatically. Previously
imported interval, accumulated, and combined history therefore remains at its original
timestamps; newly imported data uses the corrected timestamps. This can leave a
discontinuity around the upgrade.

If you want to rebuild the history with corrected timestamps:

1. Back up the Home Assistant Recorder database.
2. Unload or disable all SA Power Networks config entries so they cannot import data
   during cleanup.
3. Using a supported Recorder statistics cleanup method, clear all statistic IDs
   beginning with `sapowernetworks:`. Clear interval, accumulated, and combined
   statistics for every configured account so no existing import checkpoint remains.
4. Reload the config entries and allow the integration to backfill again.

The integration does not currently provide a built-in reset action. This cleanup
deletes the integration's historical statistics, and the portal may not provide the
complete history back to 2000-01-01. Do not proceed unless you have a backup and are
comfortable with that limitation. Do not edit the Recorder database directly; if you
do not have a supported way to clear these statistics, contact the integration
maintainer before attempting a reset.

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

Multiple portal accounts can be configured. Each entry uses its own HTTP session
and cookies, which are released when the entry is unloaded. Credential checks
during setup and reauthentication use separate, temporary sessions.

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

The integration reuses only the portal session belonging to that config entry.
Setup logs in independently of the temporary session used to validate credentials.
If login fails, check Home Assistant logs for:

- HTTP 401/403: credentials rejected
- HTTP 503: portal unavailable or maintenance page
- Portal shape/parse errors after portal changes

### No statistics appear in Energy Dashboard

- Check Rows Imported and Channels Imported first
- Trigger a manual refresh using the button or service
- Confirm Recorder is enabled and healthy
- Wait for first backfill/import to complete

### Last Error has content

The Last Error sensor redacts sensitive message content, stays available after a
failed refresh, and clears after a successful refresh. Open Home Assistant logs and
diagnostics for the config entry for more detail. Diagnostics redact sensitive
fields such as credentials and account identifiers.

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
