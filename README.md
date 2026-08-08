# sapowernetworks-meterdata-ha

Home Assistant integration with SA Power Networks meter data

## Using The Recorder Data In Home Assistant

This integration writes meter data into Home Assistant Recorder as external statistics.
It does not currently create normal per-meter energy entities for the imported kWh data.

What you should see in Home Assistant after setup:

- Status sensors for the integration, including authentication status, NMI count, rows imported, channels imported, last error, and last successful sync.
- Recorder-backed statistics with names starting with `SA Power Networks ...` once data has been imported successfully.

Before trying to add the data to the UI, confirm the import is working:

1. Open the SA Power Networks status sensors in Home Assistant.
2. Check that `Rows Imported` is greater than `0`.
3. Check that `Channels Imported` is greater than `0`.
4. Check that `Last Error` is empty or unchanged.

### Add The Data To The Energy Dashboard

1. Open `Settings -> Dashboards -> Energy`.
2. Add or edit an electricity source such as `Grid consumption` or `Return to grid`.
3. In the statistic selector, choose the imported statistic with a name starting with `SA Power Networks`.
4. Use the `Import` statistic for consumption data.
5. Use the `Export` statistic for feed-in data if your NMI includes export channels.
6. Save the dashboard configuration.

Notes:

- The imported statistics may not appear immediately if the first historical sync is still running.
- If the Energy dashboard selector is empty, the integration has usually not imported any rows yet.
- Historical data is imported into Recorder first, so UI visibility depends on successful Recorder writes rather than entity creation.
- Imported interval data is stored as hourly statistics in Recorder. The source SAPN data may be 5-minute data, but Home Assistant long-term statistics are hourly, so graphs built from these imported statistics will reflect hourly buckets rather than raw 5-minute points.

### Add Your Own Graphs And Cards

You can also use the imported statistics outside the Energy dashboard for custom views.

Common options in Home Assistant:

1. Use a statistics-compatible dashboard card that can select long-term statistics directly.
2. Build a custom dashboard section for import and export trends by choosing the `SA Power Networks ...` statistics as the card data source.
3. Create separate cards for import and export if your account has both channels.
4. Use the status sensors from this integration alongside the charts so you can see whether the latest sync imported new rows.

Practical graph ideas:

- Daily import history for general household usage.
- Daily export history if you have solar feed-in.
- Separate views per NMI if your account has more than one site.
- A simple admin card showing `Rows Imported`, `Channels Imported`, and `Last Successful Sync` next to the graph.

When building your own dashboards:

- Look for statistic names beginning with `SA Power Networks`.
- `Import` statistics represent energy consumed from the grid.
- `Export` statistics represent energy returned to the grid.
- If no SAPN statistics appear in the selector, check the integration status sensors first and confirm that rows have been imported.

If you use the Statistics Graph card, these options usually make the most sense:

- `change`: best default for most usage graphs. This shows how much energy changed during each displayed period.
- `sum`: useful for a cumulative or lifetime-style graph that keeps increasing over time.
- `state`: useful if you want the raw bucket value stored for each point.

For this integration:

- Interval data is imported as hourly buckets, so `state` means the kWh for that hour.
- `change` is usually the most intuitive choice for daily, weekly, or monthly usage charts.
- `sum` is usually the most useful choice for cumulative import or export curves.
- Accumulated summary data is stored per summary period, so `state` there represents the value of that imported period rather than a normalized per-hour value.
