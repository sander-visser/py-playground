#!/usr/bin/env python

"""
Find peak power use based on Tibber 30 day historic consumption
with Easee EV charging excluded.
"""

import asyncio
import csv
import datetime
import statistics
import sys
import pytz
import requests
import tibber  # pip install pyTibber

# curl --request POST --url https://api.easee.com/api/accounts/login --header 'accept: application/json' --header 'content-type: application/*+json' --data '{ "userName": "the@email.com", "password": "the_pass"}'
# Note: Easee access token expires after a few hours
EASEE_API_ACCESS_TOKEN = None  # Leave as None to analyze without ignoring EV
EASEE_CHARGER_ID = "EHVZ2792"
NORDPOOL_PRICE_CODE = "SEK"
START_DATE = None  # datetime.date.fromisoformat("2024-11-01") # None for one month back
API_TIMEOUT = 10.0  # seconds
EASEE_API_BASE = "https://api.easee.com/api"
HTTP_SUCCESS_CODE = 200
HTTP_UNAUTHORIZED_CODE = 401
# Get personal token from https://developer.tibber.com/settings/access-token
TIBBER_API_ACCESS_TOKEN = "5K4MVS-OjfWhK_4yrjOlFe1F6kJXPVf7eQYggo8ebAE"  # demo token
WEEKDAY_FIRST_HIGH_H = 6
WEEKDAY_LAST_HIGH_H = 21
IRRADIANCE_OBSERVATION = None # "smhi-opendata_11_41_42_10_24_71415.csv"  # gotten from : "https://www.smhi.se/data/meteorologi/ladda-ner-meteorologiska-observationer/irradiance/71415"
INSTALLED_PANEL_POWER = 8 * 0.45  # 8x 450W panels (perfect solar tracking assumed, could be refined by using pvlib...)
IRRADIANCE_FULL = 1000  # W / m2 needed to get full panel production
IRRADIANCE_MIN = 140  # W / m2 needed for any production


def get_easee_hourly_energy_json(api_header, charger_id, from_date, to_date):
    hourly_energy_url = (
        f"{EASEE_API_BASE}/chargers/lifetime-energy/{charger_id}/hourly?"
        + f"from={from_date}&to={to_date}"
    )
    hourly_energy = requests.get(
        hourly_energy_url, headers=api_header, timeout=API_TIMEOUT
    )
    if hourly_energy.status_code != HTTP_SUCCESS_CODE:
        if hourly_energy.status_code == HTTP_UNAUTHORIZED_CODE:
            print("Error: Easee access token expired...")
        else:
            print(f"{hourly_energy.status_code} Error: {hourly_energy.text}")
        sys.exit(1)
    return hourly_energy.json()


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
        csvReader = csv.DictReader(csv_lines, delimiter=";")
        solar_irr = {}
        for data in csvReader:
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
            else (30 * 24) if START_DATE.month != 2 else 28 * 30
        )
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
    utc_to_incl = str(local_dt_to.astimezone(pytz.utc) + datetime.timedelta(hours=1))
    zulu_to_incl = utc_to_incl.replace("+00:00", "Z")

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
            zulu_to_incl,
        )
    )
    power_peak_incl_ev = {}
    power_peak_incl_ev_time = {}
    power_hour_samples = {}
    power_map_low = {}
    power_map_high = {}
    ev_cost = 0.0
    ev_energy = 0.0
    other_cost = 0.0
    other_energy = 0.0
    exported_energy = 0.0
    exported_value = 0.0
    self_used_energy = 0.0
    self_used_value = 0.0
    for power_sample in hourly_consumption_data:
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
            power_peak_incl_ev_time[curr_time.month] = curr_time
        # print(f"Analyzing {curr_time_utc_str} with power {curr_power}")
        curr_hour_price = float(power_sample["unitPrice"])

        if irradiance is not None and curr_utc_time in irradiance:
            curr_irr = irradiance[curr_utc_time]
            if curr_irr[0] == "" or curr_irr[1] == "":
                curr_irr = (0, 0)
                # print(f"Missing solar data for {curr_utc_time}")
            irr_power = min(IRRADIANCE_FULL, float(curr_irr[0]))
            irr_duration = float(curr_irr[1])
            if irr_power > IRRADIANCE_MIN:
                solar_power = irr_power / IRRADIANCE_FULL * INSTALLED_PANEL_POWER
                self_use = curr_power * irr_duration / 3600
                solar_utilization = solar_power / curr_power
                export = 0
                if solar_utilization > 1:
                    export = (solar_utilization - 1) * irr_duration / 3600
                else:
                    self_use *= solar_utilization
                # print(
                #    f"{curr_utc_time} solar irr is {irr_power} W/m2 for {irr_duration}s. Export {export} estimate kWh. Self use estimate {self_use} kWh out of {curr_power} kWh"
                # )
                exported_energy += export
                exported_value += export * curr_hour_price
                self_used_energy += self_use
                self_used_value += self_use * curr_hour_price

        if charger_consumption is not None:
            for easee_power_sample in charger_consumption:
                if easee_power_sample["date"] == curr_time_utc_str:
                    curr_power -= easee_power_sample["consumption"]
                    ev_energy += easee_power_sample["consumption"]
                    ev_cost += curr_hour_price * easee_power_sample["consumption"]
                    # if easee_power_sample['consumption'] > 0:
                    #    print(f"power excl easee: {curr_power}")
                    break

        if (
            curr_time.weekday() < 5
            and WEEKDAY_FIRST_HIGH_H <= curr_time.hour <= WEEKDAY_LAST_HIGH_H
        ):
            power_map_high.setdefault(curr_power, []).append(curr_time)
        else:
            power_map_low.setdefault(curr_power, []).append(curr_time)
        power_hour_samples.setdefault(curr_time.hour, []).append(curr_power)

        other_cost += curr_power * curr_hour_price
        other_energy += curr_power

    for peak_month, peak_month_pwr in power_peak_incl_ev.items():
        print(
            f"Month peak power incl EV: {peak_month_pwr:3f} at {power_peak_incl_ev_time[peak_month]}"
        )

    print(
        f"Energy used {other_energy:.3f} kWh at total cost of {other_cost:.3f} {NORDPOOL_PRICE_CODE} (incl VAT and surcharges)"
    )
    if charger_consumption is None:
        print("\nTop ten peak power hours:")
    else:
        print(
            f"Plus EV energy used {ev_energy:.3f} kWh at total cost of {ev_cost:.3f} {NORDPOOL_PRICE_CODE} (incl VAT and surcharges)"
        )
        print("\nTop ten peak power hours with EV charging excluded:")

    print(
        f"\nHigh cost peaks - weekdays {WEEKDAY_FIRST_HIGH_H}:00 - {WEEKDAY_LAST_HIGH_H}:59"
    )
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

    for hour in range(24):
        print(
            f"{hour:2}-{(hour+1):2}  Avg: {(statistics.fmean(power_hour_samples[hour])):.3f} kWh/h. Peak: {sorted(power_hour_samples[hour])[-1]:.3f} kWh/h"
        )

    if irradiance is not None:
        print(
            f"\nValue from {INSTALLED_PANEL_POWER} kW solar installation (excl energy tax - assuming broker fee and network benefit cancel eachother out)"
        )
        print(f"Min solar power required for production: {IRRADIANCE_MIN} W / m2")
        print(f"Analysed with database until: {list(irradiance.keys())[-1]}")
        print(
            f"Estimated export: {exported_energy:.3f} kWh - valued at {exported_value:.3f} SEK (incl VAT)"
        )
        print(
            f"Estimated self use: {self_used_energy:.3f} kWh - valued at {self_used_value:.3f} SEK (incl VAT)"
        )
    await tibber_connection.close_connection()


loop = asyncio.run(start())
