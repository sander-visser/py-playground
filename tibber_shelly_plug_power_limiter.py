#!/usr/bin/env python

"""
Monitor power usage via Tibber Pulse HAN port cloud API.
Shelly S plug gen3 used to both measure energy and control relay.
If power is too high then act by controlling a load if the load is heating.
Load controlled via a controlling relay (Shelly local API).

If getting issues with certificate verification run on windows:
export SSL_CERT_FILE=$(python -m certifi)
"""

import asyncio
import datetime
import logging
import time

import aiohttp
import requests
import tibber  # pip install pyTibber (min 0.30.3 - supporting python 3.11 or later)

# Get personal token from https://developer.tibber.com/settings/access-token
TIBBER_API_ACCESS_TOKEN = (
    "3A77EECF61BD445F47241A5A36202185C35AF3AF58609E19B53F3A8872AD7BE1-1"  # Demo token
)
HOME_INDEX = 0  # 0 unless multiple Tibber homes registered
RESTRICTED_HOURS = list(range(7, 20 + 1))  # 07:00 - 20:59
RESTRICTED_DAYS = [0, 1, 2, 3, 4, 5, 6]  # 0 is Monday
# fmt: off
# kWh/h budget per month: Jan  Feb  Mar  Apr  May  Jun  Jul  Aug  Sept Oct  Nov  Dec
RESTRICTED_KW_BUDGET   = [3.5, 3.5, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.0, 3.5]
UNRESTRICTED_KW_BUDGET = [7.5, 7.5, 6.5, 6.0, 5.5, 5.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0]
# fmt: on
BUDGET_FILTER_LEN = 3
FIRSTLINE_RELAY_URL = "http://192.168.1.191/rpc/switch."  # Set None if no relay is installed
FIRSTLINE_RELAY_MODE = "true"  # Set "false" if normally open (NO) relay is used
FIRSTLINE_RELAY_SET_URL = f"{FIRSTLINE_RELAY_URL}set?id=0&on={FIRSTLINE_RELAY_MODE}&toggle_after=120"
FIRSTLINE_RELAY_GET_URL = f"{FIRSTLINE_RELAY_URL}getstatus?id=0"
RELAY_GET_URL = "http://192.168.1.222/rpc/Switch.GetStatus?id=0"
RELAY_SET_URL = f"http://192.168.1.222/rpc/switch.set?id=0&on=false&toggle_after="
HEATING_ENERGY_PER_MINUTE_THRESHOLD = 25000  # Wh/min for a 2kW heater
HYSTERESIS_MINUTES = 15

API_TIMEOUT = 10.0  # In seconds
MIN_PER_H = 60
SEC_PER_MIN = 60
MAX_RETRY_COUNT = 6  # 10s apart


def pause_with_relay(sec_pause):
    try:
        if FIRSTLINE_RELAY_URL is not None:
            firstline_state = requests.get(FIRSTLINE_RELAY_GET_URL, timeout=API_TIMEOUT)
            if firstline_state.status_code != requests.codes.ok:
                logging.warning(
                    f"Failed to check firstline {firstline_state.status_code}"
                )
            elif str(firstline_state.json()["output"]).lower() != FIRSTLINE_RELAY_MODE:
                logging.info(f"Firstline has not yet acted  - do that first")
                resp = requests.get(FIRSTLINE_RELAY_SET_URL, timeout=API_TIMEOUT)
                if resp.status_code == requests.codes.ok:
                    logging.info(f"Waiting for firstline acting to have effect")
                    return

        if sec_pause is not None:
            resp = requests.get(RELAY_SET_URL + f"{sec_pause}", timeout=API_TIMEOUT)
            if resp.status_code != requests.codes.ok:
                logging.warning(f"Acting relay failed {resp.status_code}")
    except requests.exceptions.ConnectionError:
        logging.warning("Acting relay failed - connection error")
    except requests.exceptions.Timeout:
        logging.warning("Acting relay failed - timeout")


def _rt_callback(pkg):
    global last_load_report_month
    global last_adaptive_hour
    global adaptive_restricted_budget
    global adaptive_unrestricted_budget

    data = pkg.get("data")
    if data is None:
        return
    live_data = data.get("liveMeasurement")
    current_time = datetime.datetime.fromisoformat(live_data["timestamp"])

    restricted_time = (
        current_time.weekday() in RESTRICTED_DAYS
        and current_time.hour in RESTRICTED_HOURS
    )
    budget = (
        RESTRICTED_KW_BUDGET[current_time.month - 1]
        if restricted_time
        else UNRESTRICTED_KW_BUDGET[current_time.month - 1]
    )
    if (
        current_time.month != last_load_report_month
        or adaptive_unrestricted_budget is None
        or adaptive_restricted_budget is None
    ):
        adaptive_unrestricted_budget = [0.0]
        adaptive_restricted_budget = [0.0]
        last_load_report_month = current_time.month
    if restricted_time:
        adaptive_restricted_budget = sorted(adaptive_restricted_budget, reverse=True)
        del adaptive_restricted_budget[BUDGET_FILTER_LEN:]
        budget = max(budget, adaptive_restricted_budget[-1])
        if last_adaptive_hour != current_time.hour and current_time.minute == 59:
            last_adaptive_hour = current_time.hour
            adaptive_restricted_budget.append(
                live_data["accumulatedConsumptionLastHour"]
            )
    else:
        adaptive_unrestricted_budget = sorted(
            adaptive_unrestricted_budget, reverse=True
        )
        del adaptive_unrestricted_budget[BUDGET_FILTER_LEN:]
        budget = max(budget, adaptive_unrestricted_budget[-1])
        if last_adaptive_hour != current_time.hour and current_time.minute == 59:
            last_adaptive_hour = current_time.hour
            adaptive_unrestricted_budget.append(
                live_data["accumulatedConsumptionLastHour"]
            )

    acting_needed = (live_data["estimatedHourConsumption"]) > budget
    if acting_needed:
        try:
            resp = requests.get(RELAY_GET_URL, timeout=API_TIMEOUT)
            if resp.status_code != requests.codes.ok:
                logging.warning(f"Polling relay failed {resp.status_code}")
        except requests.exceptions.ConnectionError:
            logging.warning("Polling relay failed - connection error")
        except requests.exceptions.Timeout:
            logging.warning("Polling relay failed - timeout")

        load_last_three_min = resp.json()["aenergy"]["by_minute"]
        logging.info(
            f"Supervised load active at {live_data['timestamp']}: {load_last_three_min}\n"
            + f"Possibly acting to reduce consumption to keep {budget} kWh budget.\n"
            + f"kWh/h estimate filtered: {live_data['estimatedHourConsumption']}"
        )
        if load_last_three_min[0] >= HEATING_ENERGY_PER_MINUTE_THRESHOLD:
            pause_with_relay(None)  # First line act
        if (
            load_last_three_min[0] >= HEATING_ENERGY_PER_MINUTE_THRESHOLD
            and load_last_three_min[1] >= HEATING_ENERGY_PER_MINUTE_THRESHOLD
        ):
            logging.info(f"Acting with relay to pause power use: {live_data}")
            sec_pause = (
                MIN_PER_H - current_time.minute
            ) * SEC_PER_MIN - current_time.second
            sec_pause = min(sec_pause, HYSTERESIS_MINUTES * SEC_PER_MIN)
            if sec_pause > 15:
                pause_with_relay(sec_pause)


async def start():
    session = aiohttp.ClientSession()
    tibber_connection = tibber.Tibber(
        TIBBER_API_ACCESS_TOKEN,
        user_agent="tibber_shelly_plug_power_limiter",
        websession=session,
        time_zone=datetime.timezone.utc,
    )
    home = None
    try:
        await tibber_connection.update_info()
        home = tibber_connection.get_homes()[HOME_INDEX]
        await home.rt_subscribe(_rt_callback)
    except Exception as e:
        logging.error(f"Setup error: {e}")

    alive_timeout = MAX_RETRY_COUNT
    while home is not None:
        if home.rt_subscription_running:
            alive_timeout = MAX_RETRY_COUNT
        else:
            alive_timeout -= 1
            if alive_timeout < 0:
                return
        await asyncio.sleep(10)


#  Globals
last_adaptive_hour = None
last_load_report_month = None
adaptive_unrestricted_budget = None
adaptive_restricted_budget = None
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("tibber_shelly_plug_power_limiter.log"),
        logging.StreamHandler(),
    ],
)

while True:
    try:
        loop = asyncio.run(start())
    except tibber.exceptions.FatalHttpExceptionError:
        logging.error("Server issues detected...")
    time.sleep(SEC_PER_MIN)
