#!/usr/bin/env python

"""
Monitor power usage via Tibber and if too high act

If getting issues with certificate verification run on windows:
export SSL_CERT_FILE=$(python -m certifi)
"""

import asyncio
import datetime
import time

import aiohttp
import requests
import tibber  # pip install pyTibber (min 0.30.3 - supporting python 3.11 or later)

# Get personal token from https://developer.tibber.com/settings/access-token
TIBBER_API_ACCESS_TOKEN = "5K4MVS-OjfWhK_4yrjOlFe1F6kJXPVf7eQYggo8ebAE"  # demo token
WEEKDAY_FIRST_HIGH_H = 6
WEEKDAY_LAST_HIGH_H = 21  # :59
MIN_SUPERVISED_CURRENT = 6.5
SUPERVISED_CIRCUITS = ["1", "2"]
MINIMUM_LOAD_MINUTES_PER_H = 15
HOURLY_KWH_BUDGET = 4.0
ADDED_LOAD_MARGIN_KW = 2.3  # Laundry load
ADDED_LOAD_MARGIN_DURATION_MINS = 15
# Ex: Wifi connected VVB (Raspberry Pico WH + servo): vvb_optimizer_connected.py
ACTION_URL = "http://192.168.1.208/25"  # .[ACTED_MINUTE]
API_TIMEOUT = 10.0  # In seconds
MIN_PER_H = 60
WATT_PER_KW = 1000
MAX_RETRY_COUNT = 6  # 10s apart


def _callback(pkg):
    global acted_hour
    data = pkg.get("data")
    if data is None:
        return
    live_data = data.get("liveMeasurement")
    supervised_load_maybe_active = False
    if acted_hour is not None and acted_hour != time.localtime()[3]:
        acted_hour = None

    supervised_currents = []
    for circuit in SUPERVISED_CIRCUITS:
        supervised_currents.append(live_data[f"currentL{circuit}"])
    if min(supervised_currents) > MIN_SUPERVISED_CURRENT:
        supervised_load_maybe_active = True

    if (
        live_data["accumulatedConsumptionLastHour"]
        > (HOURLY_KWH_BUDGET * MINIMUM_LOAD_MINUTES_PER_H / MIN_PER_H)
        and time.localtime()[4] > MINIMUM_LOAD_MINUTES_PER_H
    ):
        reserved_energy_duration = min(
            ADDED_LOAD_MARGIN_DURATION_MINS, MIN_PER_H - time.localtime()[4]
        )
        reserved_energy = ADDED_LOAD_MARGIN_KW * reserved_energy_duration / MIN_PER_H

        volt_sum = 0
        for circuit in SUPERVISED_CIRCUITS:
            volt_sum += live_data[f"voltagePhase{circuit}"]
        controllable_energy = (
            MIN_SUPERVISED_CURRENT
            * volt_sum
            * ((MIN_PER_H - time.localtime()[4]) / MIN_PER_H)
            / WATT_PER_KW
        )
        print(
            f"Supervised load active: {supervised_load_maybe_active}\n"
            + f"Acted to reduce consumption: {acted_hour is not None}\n"
            + f"kWh/h estimate: {live_data["estimatedHourConsumption"]} + "
            + f"reserved: {reserved_energy:.3f} - "
            + f"controllable {controllable_energy:.3f}"
        )
        if (
            (
                live_data["estimatedHourConsumption"]
                + reserved_energy
                - controllable_energy
            )
            > HOURLY_KWH_BUDGET
            and supervised_load_maybe_active
            and acted_hour is None
        ):
            acted_hour = time.localtime()[3]
            if WEEKDAY_FIRST_HIGH_H <= acted_hour <= WEEKDAY_LAST_HIGH_H:
                print(f"Acting to reduce power use: {live_data}")
                try:
                    resp = requests.get(
                        ACTION_URL + f".{time.localtime()[4]}", timeout=API_TIMEOUT
                    )
                    if resp.status_code != requests.codes.ok:
                        print(f"Acting failed {resp.status_code}")
                        acted_hour = None  # Retry...
                except requests.exceptions.ConnectionError:
                    print("Acting failed - connection error")
                    acted_hour = None  # Retry...
                except requests.exceptions.Timeout:
                    print("Acting failed - timeout")
                    acted_hour = None  # Retry...
            else:
                print(f"Ignoring power use during cheap hours: {live_data}")


async def start():
    session = aiohttp.ClientSession()
    tibber_connection = tibber.Tibber(
        TIBBER_API_ACCESS_TOKEN,
        user_agent="tibber_power_monitor",
        websession=session,
        time_zone=datetime.timezone.utc,
    )
    await tibber_connection.update_info()
    home = tibber_connection.get_homes()[0]
    await home.rt_subscribe(_callback)

    alive_timeout = MAX_RETRY_COUNT
    while True:
        if home.rt_subscription_running:
            alive_timeout = MAX_RETRY_COUNT
        else:
            alive_timeout -= 1
            if alive_timeout < 0:
                return
        await asyncio.sleep(10)


#  Globals
acted_hour = None

while True:
    try:
        loop = asyncio.run(start())
    except tibber.exceptions.FatalHttpExceptionError:
        print("Server issues detected...")
    time.sleep(60)
