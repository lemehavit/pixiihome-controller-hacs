# Energy forecast, smart solar charging and the whole-day planner

Version 0.5.0 adds a household consumption forecast, an optional solar forecast
and three features that use them. Nothing on this page sends commands to the
battery by itself. The forecasts only change the decisions that solar charging
and price optimization already make.

## Configure the forecast

Open the integration's **Configure → Energy forecast** and choose:

| Field | Use |
| --- | --- |
| Solar forecast sensors | One or more Solcast PV Forecast sensors (*Forecast today*, *Forecast tomorrow*; the `detailedForecast` or `detailedHourly` attribute is read) or Open-Meteo Solar Forecast sensors with a `watts` attribute. Optional. |
| Solar production power sensor | The inverter's current production in W or kW, for example `sensor.inverter_active_power`. Needed to separate solar from consumption and to correct the forecast during the day. |
| Outdoor temperature | A temperature sensor or a `weather` entity. Lets the model learn extra consumption on cold days. Optional. |

Without a solar forecast, the consumption forecast still works and the features
that need sun stay off with reason `solar_forecast_unavailable`.

### Getting started with Solcast

1. Create a free *Home user / hobbyist* account at <https://toolkit.solcast.com.au/register>.
2. Add your rooftop site (location, azimuth, tilt, kWp, install date). Two roof
   planes can be two sites.
3. Copy your API key from the account page.
4. In HACS, install **Solcast PV Forecast** (<https://github.com/BJReplay/ha-solcast-solar>),
   restart Home Assistant and add the integration with the API key. The free
   plan allows 10 updates per day; the integration's default automatic updates
   fit within that.
5. In Pixiihome Controller choose **Configure → Energy forecast** and select the
   Solcast *Forecast today* and *Forecast tomorrow* sensors.

## Household consumption forecast

Household consumption is computed as grid power − battery power + solar
production. The model only uses your own measurements. For each hour of the
week it keeps the hourly means of the last six weeks, and forecasts their
median. While few weeks are known, the median of the same clock hour over the
last two weeks is blended in. Medians ignore single unusual days. **Plan for
high consumption** uses the 80th percentile of the same values instead.

With an outdoor temperature, the model fits a straight line each day between
daily mean consumption and heating degrees (degrees below 17 °C) over the last
60 days, once at least ten days over a 4 °C range exist. Each stored hour is
then moved to the forecast temperature before the median is taken. Without any
measurements the forecast is a flat 1 kW.

On first start the model learns from up to 70 days of Home Assistant long-term
statistics for the Pixii meter and battery power sensors (and the solar power
sensor when configured), so it is useful from day one. After that it learns from
live samples every quarter of an hour and saves itself every ten minutes.

## Smart solar charging

Switch **Smart solar charging** (Automatic solar charging device) lets the solar
controller wait with charging while the price is high now and lower later the
same day, and sell the surplus instead. It only waits when all of these hold:

- the conservative solar forecast (Solcast's 10th percentile, or 70 % of the
  mean forecast) minus the high consumption forecast, limited to the solar charge
  power, still fills the battery to **Solar maximum SoC** later today, with the
  **Smart solar safety margin** (default 25 %) on top;
- the price now is positive and at least **Smart solar minimum price advantage**
  (default 0.20 kr/kWh) above the average price of the cheapest later quarters
  needed to charge;
- the actual production during the last 30 minutes is not below 60 % of the
  forecast. If it is, smart waiting is turned off for the rest of the day.

If the target can no longer be reached, the controller charges at once and keeps
charging for at least 30 minutes. The status is then `deferring` with
`reason_text` explaining why. The attribute `smart_solar_charging` shows the
calculation, including `reachable_soc_percent`: what the forecast says can be
reached today. **Price usable capacity** must be set.

## Load forecast in price optimization

Price optimization now limits self-consumption discharge in its calculation to
the forecast household consumption minus solar, so it no longer plans to
discharge more energy than the house uses. The schedule's power limit is
unchanged; the battery still follows the actual load. Switch **Plan for high
consumption** uses the high value instead, so more energy is kept for hours when
the house often uses more.

## Whole-day planner

Switch **Use whole-day planner** replaces the hour-by-hour rule with a plan that
looks at the full price horizon, the consumption forecast and the solar forecast
together (dynamic programming over the battery's energy). It decides for each
interval whether to charge from the grid, store solar, cover the house, export
(only with **Allow grid export**) or stay idle, with the same minimum profit,
efficiency, cycle cost and SOC limits as before. Adjacent identical intervals are
merged into one schedule.

The whole-day plan is calculated every round even when the switch is off, and
shown in the attribute `whole_day_plan` of **Price optimization status** next to
the standard plan's margin. Compare the two before turning it on. Aggregator
Target SOC schedules are handled as in the standard plan: with **Allow changing
aggregator schedules** off they refill the battery and are planned around; with
it on, those between a planned discharge and the next planned charge are
lowered to the planned level (never below the minimum SOC), so a sale is not
undone by the aggregator's next refill.
If the forecast is missing, the standard plan is used and `planner_engine` shows
`standard_whole_day_unavailable`.

## Other additions

- **Battery savings today** (kr): grid cost without the battery minus the actual
  grid cost, with yesterday, this month and 30 days in its attributes. A logbook
  entry is written for each finished day.
- **Notify when price optimization is stopped for** (default 3 h, 0 = off): a
  Home Assistant notification when price optimization has been blocked longer
  than this; it disappears when planning runs again.
- Every status sensor has a `reason_text` attribute in Swedish.
- New sensors: **Household consumption**, **Consumption forecast next 24 h**
  (hourly forecast in attributes) and **Solar forecast remaining today**.

The idea of learning consumption per hour of the week, using a conservative
solar forecast and planning with dynamic programming was inspired by the
[FTW](https://github.com/srcfl/ftw) project. No FTW code is used.

[← Price optimization](price-optimization.md) · [Next: schedules and dashboards →](schedules-and-dashboard.md)
