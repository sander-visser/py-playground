#!/usr/bin/env python

"""
Find peak power use based on Tibber 30 day historic consumption
with Easee EV charging excluded.
"""

import asyncio
import datetime
import statistics
import sys
import pytz
import requests
import tibber  # pip install pyTibber
from nordpool import elspot  # pip install nordpool

# curl --request POST --url https://api.easee.com/api/accounts/login --header 'accept: application/json' --header 'content-type: application/*+json' --data '{ "userName": "the@email.com", "password": "the_pass"}'
# Note: Easee access token expires after a few hours
EASEE_API_ACCESS_TOKEN = None  # Leave as None to analyze without ignoring EV
EASEE_CHARGER_ID = "EHVZ2792"
NORDPOOL_PRICE_CODE = "SEK"
# Note: Earliest allowed by nortpool API is datetime.date.fromisoformat("2024-09-25")
NORDPOOL_REGION = "SE3"  # Set to None to skip pricing calculation
START_DATE = None  # datetime.date.fromisoformat("2024-11-01") # None for one month back
API_TIMEOUT = 10.0  # seconds
EASEE_API_BASE = "https://api.easee.com/api"
HTTP_SUCCESS_CODE = 200
HTTP_UNAUTHORIZED_CODE = 401
# Get personal token from https://developer.tibber.com/settings/access-token
TIBBER_API_ACCESS_TOKEN = "5K4MVS-OjfWhK_4yrjOlFe1F6kJXPVf7eQYggo8ebAE"  # demo token


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


async def start():
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
        hours_in_month = 31 * 24
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
    spot_price = elspot.Prices(NORDPOOL_PRICE_CODE)
    day_prices = []
    day_prices_date = None
    power_peak_incl_ev = {}
    power_peak_incl_ev_time = {}
    power_hour_samples = {}
    power_map = {}
    ev_cost = 0.0
    ev_energy = 0.0
    other_cost = 0.0
    other_energy = 0.0
    for power_sample in hourly_consumption_data:
        curr_time = datetime.datetime.fromisoformat(power_sample["from"])
        if NORDPOOL_REGION is not None and curr_time.date() != day_prices_date:
            day_prices_date = curr_time.date()
            day_prices = spot_price.hourly(
                end_date=day_prices_date, areas=[NORDPOOL_REGION]
            )["areas"][NORDPOOL_REGION]["values"]
        curr_time_utc_str = str(curr_time.astimezone(pytz.utc)).replace(" ", "T")
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
        curr_hour_price = 0.0
        for hour_price in day_prices:
            if hour_price["start"] == curr_time.astimezone(pytz.utc):
                curr_hour_price = hour_price["value"]
                break

        if charger_consumption is not None:
            for easee_power_sample in charger_consumption:
                if easee_power_sample["date"] == curr_time_utc_str:
                    curr_power -= easee_power_sample["consumption"]
                    ev_energy += easee_power_sample["consumption"]
                    ev_cost += curr_hour_price * easee_power_sample["consumption"]
                    # if easee_power_sample['consumption'] > 0:
                    #    print(f"power excl easee: {curr_power}")
                    break

        power_map.setdefault(curr_power, []).append(f" {curr_time}")
        power_hour_samples.setdefault(curr_time.hour, []).append(curr_power)

        other_cost += curr_power * curr_hour_price
        other_energy += curr_power

    for peak_month, peak_month_pwr in power_peak_incl_ev.items():
        print(
            f"Month peak power incl EV: {peak_month_pwr:3f} at {power_peak_incl_ev_time[peak_month]}"
        )

    print(
        f"Energy used {other_energy:.3f} kWh at total cost of {(other_cost/1000):.3f} {NORDPOOL_PRICE_CODE} (excl VAT and surcharges)"
    )
    if charger_consumption is None:
        print("Top ten peak power hours:")
    else:
        print(
            f"Plus EV energy used {ev_energy:.3f} kWh at total cost of {(ev_cost/1000):.3f} {NORDPOOL_PRICE_CODE} (excl VAT and surcharges)"
        )
        print("Top ten peak power hours with EV charging excluded:")

    for peak_pwr in sorted(power_map, reverse=True)[0:10]:
        time_str = "".join(power_map[peak_pwr])
        print(f"Peak of {peak_pwr:.3f} kWh/h has occured at{time_str}")

    if charger_consumption is None:
        print("Power use distribution:")
    else:
        print("Power use distribution with EV charging excluded:")

    for hour in range(24):
        print(
            f"{hour:2}-{(hour+1):2}  Avg: {(statistics.fmean(power_hour_samples[hour])):.3f} kWh/h. Peak: {sorted(power_hour_samples[hour])[-1]:.3f} kWh/h"
        )

    await tibber_connection.close_connection()


loop = asyncio.run(start())

