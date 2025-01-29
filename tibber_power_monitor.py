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
MIN_WATER_HEATER_CURRENT = 7.0
MIN_WATER_HEATER_MIN_PER_H = 15
HOURLY_KWH_BUDGET = 6.0
ACTION_URL = "http://192.168.1.208/26"


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
        print("VVB maybe running")
    if (
        live_data["accumulatedConsumptionLastHour"] > 2.0
        and time.localtime()[4] > MIN_WATER_HEATER_MIN_PER_H
    ):
        print(
            f"None trivial consumption detected. Estimated: {live_data['estimatedHourConsumption']}. VVB: {could_water_heater_be_running}"
        )
        if (
            live_data["estimatedHourConsumption"] > HOURLY_KWH_BUDGET
            and could_water_heater_be_running
            and acted_hour is None
        ):
            print(f"acting to reduce power use: {live_data}")
            acted_hour = time.localtime()[3]
            requests.get(ACTION_URL, timeout=API_TIMEOUT)


async def start():
    async with aiohttp.ClientSession() as session:
        tibber_connection = tibber.Tibber(
            TIBBER_API_ACCESS_TOKEN,
            user_agent="tibber_power_monitor",
            websession=session,
            time_zone=datetime.timezone.utc,
        )
        await tibber_connection.update_info()
    home = tibber_connection.get_homes()[0]
    await home.rt_subscribe(_callback)

    while True:
        await asyncio.sleep(10)


#  Globals
acted_hour = None

loop = asyncio.run(start())
