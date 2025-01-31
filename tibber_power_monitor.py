#!/usr/bin/env python

"""
Monitor power usage via tibber and if too high act

If getting issues with certificate verification run:
export SSL_CERT_FILE=$(python -m certifi)
"""

import asyncio
import datetime
import time

import aiohttp
import requests
import tibber  # pip install pyTibber

# Get personal token from https://developer.tibber.com/settings/access-token
TIBBER_API_ACCESS_TOKEN = "5K4MVS-OjfWhK_4yrjOlFe1F6kJXPVf7eQYggo8ebAE"  # demo token
WEEKDAY_FIRST_HIGH_H = 6
WEEKDAY_LAST_HIGH_H = 21
API_TIMEOUT = 10.0  # seconds
MIN_WATER_HEATER_CURRENT = 6.5
MIN_WATER_HEATER_MIN_PER_H = 15
MIN_PER_H = 60
WATT_PER_KW = 1000
HOURLY_KWH_BUDGET = 3.5
ACTION_URL = "http://192.168.1.208/25"


def _callback(pkg):
    global acted_hour
    data = pkg.get("data")
    if data is None:
        return
    live_data = data.get("liveMeasurement")
    could_water_heater_be_running = False
    if acted_hour is not None and acted_hour != time.localtime()[3]:
        acted_hour = None
    if (
        live_data["currentL1"] > MIN_WATER_HEATER_CURRENT
        and live_data["currentL2"] > MIN_WATER_HEATER_CURRENT
    ):
        could_water_heater_be_running = True
    if (
        live_data["accumulatedConsumptionLastHour"]
        > (HOURLY_KWH_BUDGET * MIN_WATER_HEATER_MIN_PER_H / MIN_PER_H)
        and time.localtime()[4] > MIN_WATER_HEATER_MIN_PER_H
    ):
        controllable_energy = (
            MIN_WATER_HEATER_CURRENT
            * (live_data["voltagePhase1"] + live_data["voltagePhase2"])
            * ((MIN_PER_H - time.localtime()[4]) / MIN_PER_H)
            / WATT_PER_KW
        )
        print(
            f"Supervising VVB active: {could_water_heater_be_running} kWh/h estimate: "
            + f"{live_data["estimatedHourConsumption"]} - {controllable_energy:.3f}"
        )
        if (
            (live_data["estimatedHourConsumption"] - controllable_energy)
            > HOURLY_KWH_BUDGET
            and could_water_heater_be_running
            and acted_hour is None
        ):
            acted_hour = time.localtime()[3]
            if WEEKDAY_FIRST_HIGH_H <= acted_hour <= WEEKDAY_LAST_HIGH_H:
                print(f"Acting to reduce power use: {live_data}")
                resp = requests.get(
                    ACTION_URL + f".{time.localtime()[4]}", timeout=API_TIMEOUT
                )
                if resp.status != 200:
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

    alive_timeout = 10
    while True:
        if home.rt_subscription_running:
            alive_timeout = 10
        else:
            alive_timeout -= 1
            print(f"Reconnecting. Session closed? {session.closed}")
            if alive_timeout <= 0:
                await home.rt_resubscribe()
        await asyncio.sleep(10)


#  Globals
acted_hour = None

loop = asyncio.run(start())
