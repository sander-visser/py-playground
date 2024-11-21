#!/usr/bin/env python

"""
Find peak power use based on Tibber 30 day historic consumption
with Easee EV charging excluded.
"""

import asyncio
import datetime
import sys
import pytz
import requests
import tibber  # pip install pyTibber

# curl --request POST --url https://api.easee.com/api/accounts/login --header 'accept: application/json' --header 'content-type: application/*+json' --data '{ "userName": "the@email.com", "password": "the_pass"}'
EASEE_API_ACCESS_TOKEN = ""
EASEE_CHARGER_ID = "EHVZ2792"
API_TIMEOUT = 10.0  # seconds
EASEE_API_BASE = "https://api.easee.com/api"
HTTP_SUCCESS_CODE = 200
# token from https://developer.tibber.com/explorer
TIBBER_API_ACCESS_TOKEN = ""


def get_hourly_energy_json(api_header, charger_id, from_date, to_date):
    hourly_energy_url = (
        f"{EASEE_API_BASE}/chargers/lifetime-energy/{charger_id}/hourly?"
        + f"from={from_date}&to={to_date}"
    )
    hourly_energy = requests.get(
        hourly_energy_url, headers=api_header, timeout=API_TIMEOUT
    )
    if hourly_energy.status_code != HTTP_SUCCESS_CODE:
        print(f"Error: {hourly_energy.text}")
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
    await home.fetch_consumption_data()

    api_header = {
        "accept": "application/json",
        "Authorization": "Bearer " + EASEE_API_ACCESS_TOKEN,
    }

    local_dt_from = datetime.datetime.fromisoformat(
        home.hourly_consumption_data[0]["from"]
    )

    local_dt_to = datetime.datetime.fromisoformat(
        home.hourly_consumption_data[-1]["from"]
    )

    utc_from = str(local_dt_from.astimezone(pytz.utc))
    zulu_from = utc_from.replace("+00:00", "Z")
    utc_to = str(local_dt_to.astimezone(pytz.utc))
    zulu_to = utc_to.replace("+00:00", "Z")

    print(f"Scanning peak power {local_dt_from} - {local_dt_to}...")

    charger_consumption = get_hourly_energy_json(
        api_header,
        EASEE_CHARGER_ID,
        zulu_from,
        zulu_to,
    )
    power_hour_samples = []
    power_hour_sum = []
    for hour in range(24):
        power_hour_samples.append(0)
        power_hour_sum.append(0)
    power_map = {}
    for power_sample in home.hourly_consumption_data:
        curr_time = datetime.datetime.fromisoformat(power_sample["from"])
        curr_time_utc_str = str(
            datetime.datetime.fromisoformat(power_sample["from"]).astimezone(pytz.utc)
        ).replace(" ", "T")
        if power_sample["consumption"] is None:
            continue
        curr_power = float(power_sample["consumption"])
        # print(f"Analyzing {curr_time_utc_str} with power {curr_power}")
        for easee_power_sample in charger_consumption:
            if easee_power_sample["date"] == curr_time_utc_str:
                curr_power -= easee_power_sample["consumption"]
                # if easee_power_sample['consumption'] > 0:
                #    print(f"power excl easee: {curr_power}")
                break

        power_map.setdefault(curr_power, []).append(f" {curr_time}")
        power_hour_samples[curr_time.hour] += 1
        power_hour_sum[curr_time.hour] += curr_power

    for peak_pwr in sorted(power_map, reverse=True)[0:10]:
        time_str = "".join(power_map[peak_pwr])
        print(f"Found power peak {peak_pwr:.3f} kWh/h to have occured at{time_str}")

    print("Average power excl Easee EV charging")
    for hour in range(24):
        print(
            f"{hour:2}-{(hour+1):2}: {(power_hour_sum[hour]/power_hour_samples[hour]):.3f} kW"
        )

    await tibber_connection.close_connection()


loop = asyncio.run(start())

