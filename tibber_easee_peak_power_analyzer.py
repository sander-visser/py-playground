#!/usr/bin/env python

"""
Visualize peak power and energy distribution based on Tibber
historic consumption with Easee EV charging excluded.

Can be used to confirm energy use (when EV is excluded) is
cheaper on high resolution tariff.

Can also use SHMI solar irradiation database to calculate value
of future solar installation. Self use with current pattern and
improvement of self use if home battery is also added.
"""

import asyncio
import csv
import datetime
import statistics
import sys
import matplotlib.pyplot as plt
import numpy as np
import pytz
import requests
import tibber  # pip install pyTibber (min 0.30.3 - supporting python 3.11 or later)

# curl --request POST --url https://api.easee.com/api/accounts/login --header 'accept: application/json' --header 'content-type: application/*+json' --data '{ "userName": "the@email.com", "password": "the_pass"}'
# Note: Easee access token expires after a few hours
EASEE_API_ACCESS_TOKEN = None  # Leave as None to analyze without ignoring EV
EASEE_CHARGER_ID = "EHVZ2792"  # Note: must be configured with intervalInMinutes == 60 and alsoSendWhenNotCharging == true
NORDPOOL_PRICE_CODE = "SEK"
START_DATE = datetime.date.fromisoformat("2025-06-01")  # None for one month back
API_TIMEOUT = 10.0  # seconds
EASEE_API_BASE = "https://api.easee.com/api"
HTTP_SUCCESS_CODE = 200
HTTP_UNAUTHORIZED_CODE = 401
SECONDS_PER_HOUR = 3600
# Get personal token from https://developer.tibber.com/settings/access-token
TIBBER_API_ACCESS_TOKEN = (
    "3A77EECF61BD445F47241A5A36202185C35AF3AF58609E19B53F3A8872AD7BE1-1"  # Demo token
)
WEEKDAY_RESTRICTED_HOURS = [6, 7, 8, 9, 10, 17, 18, 19, 20, 21]
BATTERY_SIZE_KWH = 7.0
# Gotten from "https://www.smhi.se/data/solstralning/solstralning/irradiance/71415"
IRRADIANCE_OBSERVATION = None  # "smhi.csv" # Cleaned up with leading garbage removed
INSTALLED_PANEL_POWER = (
    10 * 0.45
)  # 10x 450W panels (perfect solar tracking assumed, could be refined by using pvlib...)
IRRADIANCE_FULL = 1000  # W / m2 needed to get full panel production
IRRADIANCE_MIN = 140  # W / m2 needed for any production


def get_easee_hourly_energy_json(api_header, charger_id, from_date, to_date_after):
    measurements_url = (
        f"{EASEE_API_BASE}/chargers/lifetime-energy/{charger_id}/all?"
        + f"from={from_date}&to={to_date_after}"
    )
    measurements = requests.get(
        measurements_url, headers=api_header, timeout=API_TIMEOUT
    )
    if measurements.status_code != HTTP_SUCCESS_CODE:
        if measurements.status_code == HTTP_UNAUTHORIZED_CODE:
            print("Error: Easee access token expired...")
        else:
            print(f"{measurementsy.status_code} Error: {measurements.text}")
        sys.exit(1)
    hourly_energy = []
    prev_measurement = None
    for measurement in measurements.json()["measurements"]:
        if prev_measurement is None:
            if ":00:00+00:00" not in measurement["measuredAt"]:
                print("Error: Easee from date not an hourly boundary...")
            prev_measurement = measurement
        else:
            if ":00:00+00:00" in measurement["measuredAt"]:
                hourly_energy.append(
                    {
                        "consumption": measurement["value"] - prev_measurement["value"],
                        "date": prev_measurement["measuredAt"],
                    }
                )
                prev_measurement = measurement

    return hourly_energy


def get_irradiance_observation():
    if IRRADIANCE_OBSERVATION is None:
        return None
    with open(IRRADIANCE_OBSERVATION, encoding="utf-8") as csvf:
        csv_lines = csvf.readlines()
        while True:
            curr_line = csv_lines.pop(0)
            if curr_line.startswith("Datum"):
                csv_lines.insert(0, curr_line)
                break
        csv_reader = csv.DictReader(csv_lines, delimiter=";")
        solar_irr = {}
        for data in csv_reader:
            datetime_str = f"{data['Datum']} {data['Tid (UTC)']}"
            datetime_object = datetime.datetime.strptime(
                datetime_str, "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=datetime.timezone.utc)
            solar_irr[datetime_object] = (
                data["Global Irradians (svenska stationer)"],
                data["Solskenstid"],
            )
        return solar_irr
    return None


def get_arbitrage_profit(curr_day_samples):
    arbitrage_savings = 0.0
    arbitrage_energy = BATTERY_SIZE_KWH
    arbitrage_savings -= BATTERY_SIZE_KWH * sorted(curr_day_samples)[0]
    for energy_price in sorted(curr_day_samples, reverse=True):
        energy_use = curr_day_samples[energy_price]
        if arbitrage_energy > energy_use:
            arbitrage_savings += energy_use * energy_price
            arbitrage_energy -= energy_use
        else:
            arbitrage_savings += arbitrage_energy * energy_price
            break
    return arbitrage_savings


def render_visualization(start_date, low_prices, low_cons, high_prices, high_cons):
    low_p_color = "tab:red"
    high_p_color = "tab:orange"
    low_e_color = "tab:green"
    high_e_color = "#7600bc"

    x = np.arange(0, 25)  # Render line til 24:00
    high_prices.append(None)  # Render line til 24:00
    low_prices.append(None)  # Render line til 24:00

    low_avg_cons = []
    low_peak_cons = []
    for cons in low_cons:
        low_avg_cons.append(cons["avg"])
        low_peak_cons.append(cons["max"])
    low_avg_cons.append(None)  # Render line til 24:00
    low_peak_cons.append(None)  # Render line til 24:00

    high_avg_cons = []
    high_peak_cons = []
    for cons in high_cons:
        high_avg_cons.append(cons["avg"])
        high_peak_cons.append(cons["max"])
    high_avg_cons.append(None)  # Render line til 24:00
    high_peak_cons.append(None)  # Render line til 24:00

    low_avg = np.array(low_avg_cons)
    low_peak = np.array(low_peak_cons)
    high_avg = np.array(high_avg_cons)
    high_peak = np.array(high_peak_cons)
    fig, axes = plt.subplots()
    plt.xticks(x)
    price_twin = axes.twinx()
    price_twin.grid(linestyle="-")
    price_twin.plot(
        x,
        np.array(low_prices),
        color=low_p_color,
        label="none congested",
        drawstyle="steps-post",
    )
    price_twin.plot(
        x,
        np.array(high_prices),
        color=high_p_color,
        label="congested",
        drawstyle="steps-post",
    )
    price_twin.set_ylabel("Energy price avg (SEK incl VAT and surcharges)")

    axes.plot(
        x,
        low_avg,
        color=low_e_color,
        label="avg (none congested)",
        drawstyle="steps-post",
    )
    axes.plot(
        x,
        low_peak,
        color=low_e_color,
        label="peak (none congested)",
        drawstyle="steps-post",
        linestyle="--",
    )
    axes.plot(
        x,
        high_avg,
        color=high_e_color,
        label="avg (congested)",
        drawstyle="steps-post",
    )
    axes.plot(
        x,
        high_peak,
        color=high_e_color,
        label="peak (congested)",
        drawstyle="steps-post",
        linestyle="--",
    )
    axes.set_xlabel("start hour")
    axes.set_ylabel("Energy use (kWh/h)")
    axes.grid(linestyle="--")

    # widen to make room for legends on the side
    fig.set_figwidth(fig.get_figwidth() * 1.35)

    # Shrink x axis
    box = axes.get_position()
    axes.set_position([box.x0, box.y0, box.width * 0.7, box.height])

    price_legend = price_twin.legend(
        title="Avg energy price", loc="upper left", bbox_to_anchor=(1.13, 0.5)
    )
    energy_legend = axes.legend(
        title="Energy use", loc="lower left", bbox_to_anchor=(1.13, 0.5)
    )

    plt.title(f"Energy usage pattern {start_date}")
    plt.savefig(f"{start_date}.png")
    # plt.show()


async def start():
    irradiance = get_irradiance_observation()

    tibber_connection = tibber.Tibber(
        TIBBER_API_ACCESS_TOKEN,
        user_agent="tibber_easee_peak_power",
        ssl=False,
        time_zone=datetime.timezone.utc,
    )
    await tibber_connection.update_info()
    print(f"Scanning home of {tibber_connection.name}")

    home = tibber_connection.get_homes()[0]
    hourly_consumption_data = None
    if START_DATE is not None:
        hours_in_month = (
            (31 * 24)
            if START_DATE.month in [1, 3, 5, 7, 8, 10, 12]
            else (30 * 24) if START_DATE.month != 2 else 28 * 24
        )
        if START_DATE.month == 2 and (START_DATE.year % 4) == 0:
            hours_in_month += 24
        if START_DATE.month == 3:
            hours_in_month -= 1
        if START_DATE.month == 10:
            hours_in_month += 1
        hourly_consumption_data = await home.get_historic_data_date(
            START_DATE, hours_in_month
        )
    else:
        await home.fetch_consumption_data()
        hourly_consumption_data = home.hourly_consumption_data

    local_dt_from = datetime.datetime.fromisoformat(hourly_consumption_data[0]["from"])

    local_dt_to = datetime.datetime.fromisoformat(hourly_consumption_data[-1]["from"])

    utc_from = str(local_dt_from.astimezone(pytz.utc))
    zulu_from = utc_from.replace("+00:00", "Z")
    utc_to_next_h = str(local_dt_to.astimezone(pytz.utc) + datetime.timedelta(hours=2))
    zulu_to_next_h = utc_to_next_h.replace("+00:00", "Z")

    print(f"Scanning peak power {local_dt_from} - {local_dt_to}...")

    charger_consumption = (
        None
        if EASEE_API_ACCESS_TOKEN is None
        else get_easee_hourly_energy_json(
            {
                "accept": "application/json",
                "Authorization": "Bearer " + EASEE_API_ACCESS_TOKEN,
            },
            EASEE_CHARGER_ID,
            zulu_from,
            zulu_to_next_h,
        )
    )
    power_peak_incl_ev = {}
    time_peak_incl_ev = {}
    power_hour_samples = {}
    high_power_hour_samples = {}
    power_map_low = {}
    power_map_high = {}
    curr_day_samples = {}
    arbitrage_savings = 0.0
    solar_battery_contents = 0.0
    solar_battery_self_use_kwh = 0.0
    ev_cost = 0.0
    ev_energy = 0.0
    other_cost = 0.0
    other_energy = 0.0
    exported_energy = 0.0
    exported_value = 0.0
    self_used_energy = 0.0
    self_used_value = 0.0
    for power_sample in hourly_consumption_data:
        if len(curr_day_samples) == 24:
            if BATTERY_SIZE_KWH is not None:
                todays_arbitrage_savings = get_arbitrage_profit(curr_day_samples)
                arbitrage_savings += todays_arbitrage_savings
            curr_day_samples = {}

        curr_time = datetime.datetime.fromisoformat(power_sample["from"])
        curr_utc_time = curr_time.astimezone(pytz.utc)
        curr_time_utc_str = str(curr_utc_time).replace(" ", "T")
        if power_sample["consumption"] is None:
            continue
        curr_power = float(power_sample["consumption"])
        if (
            curr_time.month not in power_peak_incl_ev
            or curr_power > power_peak_incl_ev[curr_time.month]
        ):
            power_peak_incl_ev[curr_time.month] = curr_power
            time_peak_incl_ev[curr_time.month] = curr_time
        # print(f"Analyzing {curr_time_utc_str} with power {curr_power}")
        curr_hour_price = float(power_sample["unitPrice"])
        curr_day_samples[curr_hour_price + (curr_time.hour * 0.000001)] = curr_power

        if irradiance is not None and curr_utc_time in irradiance:
            curr_irr = irradiance[curr_utc_time]
            if curr_irr[0] == "" or curr_irr[1] == "":
                curr_irr = (0, 0)
                # print(f"Missing solar data for {curr_utc_time}")
            irr_power = min(IRRADIANCE_FULL, float(curr_irr[0]))
            irr_duration = float(curr_irr[1])
            self_use = 0.0
            export = 0.0
            if irr_power > IRRADIANCE_MIN:
                solar_power = irr_power / IRRADIANCE_FULL * INSTALLED_PANEL_POWER
                self_use = curr_power * irr_duration / SECONDS_PER_HOUR
                solar_factor = solar_power / curr_power

                if solar_factor < 1:
                    self_use *= solar_factor
                export = solar_power - self_use

                if (
                    BATTERY_SIZE_KWH is not None
                    and solar_battery_contents < BATTERY_SIZE_KWH
                ):
                    solar_battery_contents += export
                    export = 0
                    if solar_battery_contents > BATTERY_SIZE_KWH:
                        export = solar_battery_contents - BATTERY_SIZE_KWH
                        solar_battery_contents = BATTERY_SIZE_KWH
            if BATTERY_SIZE_KWH is not None:
                if (curr_power - self_use) > 0:
                    discharge = min(solar_battery_contents, (curr_power - self_use))
                    self_use += discharge
                    solar_battery_contents -= discharge
                    solar_battery_self_use_kwh += discharge

            self_used_energy += self_use
            self_used_value += self_use * curr_hour_price
            if curr_hour_price >= 0.0:
                exported_energy += export
                exported_value += export * curr_hour_price

        if charger_consumption is not None:
            for easee_power_sample in charger_consumption:
                if easee_power_sample["date"] == curr_time_utc_str:
                    curr_power -= easee_power_sample["consumption"]
                    ev_energy += easee_power_sample["consumption"]
                    ev_cost += curr_hour_price * easee_power_sample["consumption"]
                    # if easee_power_sample['consumption'] > 0:
                    #    print(f"power excl easee: {curr_power}")
                    break

        if curr_time.weekday() < 5 and curr_time.hour in WEEKDAY_RESTRICTED_HOURS:
            power_map_high.setdefault(curr_power, []).append(curr_time)
            high_power_hour_samples.setdefault(curr_time.hour, []).append(
                {curr_power: curr_hour_price}
            )
        else:
            power_map_low.setdefault(curr_power, []).append(curr_time)
            power_hour_samples.setdefault(curr_time.hour, []).append(
                {curr_power: curr_hour_price}
            )

        other_cost += curr_power * curr_hour_price
        other_energy += curr_power

    for peak_month, peak_month_pwr in power_peak_incl_ev.items():
        print(
            f"Month peak power incl EV: {peak_month_pwr:3f} at {time_peak_incl_ev[peak_month]}"
        )

    print(
        f"Energy used {other_energy:.3f} kWh"
        + f" at energy cost of {other_cost:.3f} {NORDPOOL_PRICE_CODE} (incl VAT and surcharges)"
        + f" (avg price: {other_cost/other_energy:.3f})"
    )
    if charger_consumption is None or ev_energy == 0.0:
        print("\nTop ten peak power hours:")
    else:
        print(
            f"Plus EV energy used {ev_energy:.3f} kWh"
            + f" at energy cost of {ev_cost:.3f} {NORDPOOL_PRICE_CODE} (incl VAT and surcharges)"
            + f" (avg price: {ev_cost/ev_energy:.3f} (excl grid rewards))"
        )
        print("\nTop ten peak power hours with EV charging excluded:")

    print(f"\nHigh cost peaks - weekdays at {WEEKDAY_RESTRICTED_HOURS} :00 - :59")
    for peak_pwr in sorted(power_map_high, reverse=True)[0:10]:
        time_str = f"{power_map_high[peak_pwr][0]}"
        for times in power_map_high[peak_pwr][1:]:
            time_str += "".join(f", {times}")
        print(f"Peak of {peak_pwr:.3f} kWh/h has occured at {time_str}")

    print("\nLow cost peaks:")
    for peak_pwr in sorted(power_map_low, reverse=True)[0:10]:
        time_str = f"{power_map_low[peak_pwr][0]}"
        for times in power_map_low[peak_pwr][1:]:
            time_str += "".join(f", {times}")
        print(f"Peak of {peak_pwr:.3f} kWh/h has occured at {time_str}")

    if charger_consumption is None:
        print("\nPower use distribution:")
    else:
        print("\nPower use distribution with EV charging excluded:")

    high_prices = []
    high_cons = []
    low_prices = []
    low_cons = []
    for hour in range(24):
        high_str = ""
        if hour in high_power_hour_samples:
            price_list = []
            consumption_list = []
            price_sum = 0.0
            for hour_sample in high_power_hour_samples[hour]:
                price_list.append(list(hour_sample.values())[0])
                consumption_list.append(list(hour_sample.keys())[0])
                price_sum += list(hour_sample.values())[0] * list(hour_sample.keys())[0]
            high_prices.append(price_sum / sum(consumption_list))
            high_cons.append(
                {
                    "avg": statistics.fmean(consumption_list),
                    "max": sorted(consumption_list)[-1],
                }
            )
            print(
                f"{hour:2}-{(hour+1):2} High Avg: "
                + f"{high_cons[hour]['avg']:.2f} kW"
                + f" @{high_prices[hour]:.2f}"
                + f" (flat avg: {statistics.fmean(price_list):.2f}) SEK/kWh)"
                + f" Peak: {high_cons[hour]['max']:.2f} kWh/h"
            )
        else:
            high_prices.append(None)
            high_cons.append({"avg": None, "max": None})
    for hour in range(24):
        price_list = []
        consumption_list = []
        price_sum = 0.0
        for hour_sample in power_hour_samples[hour]:
            price_list.append(list(hour_sample.values())[0])
            consumption_list.append(list(hour_sample.keys())[0])
            price_sum += list(hour_sample.values())[0] * list(hour_sample.keys())[0]
        low_prices.append(price_sum / sum(consumption_list))
        low_cons.append(
            {
                "avg": statistics.fmean(consumption_list),
                "max": sorted(consumption_list)[-1],
            }
        )
        print(
            f"{hour:2}-{(hour+1):2}  Low Avg: "
            + f"{low_cons[hour]['avg']:.2f} kW"
            + f" @{low_prices[hour]:.2f}"
            + f" (flat avg: {statistics.fmean(price_list):.2f}) SEK/kWh)"
            + f" Peak: {low_cons[hour]['max']:.2f} kWh/h"
        )

    render_visualization(
        f"{str(local_dt_from)[:10]}_{str(local_dt_to)[:10]}",
        low_prices,
        low_cons,
        high_prices,
        high_cons,
    )

    if irradiance is not None:
        battery_str = ""
        if BATTERY_SIZE_KWH is not None:
            battery_str = (
                f" when combined with {BATTERY_SIZE_KWH} kWh energy storage"
                + f" cycle count used {(solar_battery_self_use_kwh/BATTERY_SIZE_KWH):.1f}"
            )
        print(
            f"\nEstimated value from {INSTALLED_PANEL_POWER} kW solar installation"
            + battery_str
            + " (excl energy tax and network transfer cost, incl VAT."
            + " Note: Assuming broker fee and network benefit cancel each other out)"
        )
        print(f"Min solar power required for production: {IRRADIANCE_MIN} W / m2")
        print(f"Analysed with database until: {list(irradiance.keys())[-1]}")
        print(
            f"Export: {exported_energy:.2f} kWh - valued at {exported_value:.2f} SEK (incl VAT)"
        )
        print(
            f"Self use: {self_used_energy:.2f} kWh - valued at {self_used_value:.2f} SEK (incl VAT)"
        )
    print(
        f"\nArbitrage savings possible with {BATTERY_SIZE_KWH} kWh battery:"
        + " (not relevant during months when battery is used for solar storage)"
        + f" {arbitrage_savings:.2f} {NORDPOOL_PRICE_CODE} (incl VAT)"
    )
    await tibber_connection.close_connection()


loop = asyncio.run(start())
