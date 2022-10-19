#!/usr/bin/env python3

"""
Optimizer balancing comfort vs price for an IR remote controlled heatpump
- pre-heats the home in an optimal way when occupants are away or sleeping
- offers extra heat when the price is affordable

Usage:
Install needed pip packages
Run this script on a internet connected machine configured with relevant timezone
 - Tip: Use environment variable TZ='Europe/Stockholm'
Script tested with a Sensibo Sky placed 20cm above floor level
Adapt constants as needed for your home
"""

from datetime import datetime, timedelta
from time import sleep
import sys
import copy

# "python3 -m pip install X" below python modules
import requests
import pause
import holidays
from nordpool import elspot
import pytz
import sensibo_client  # https://github.com/Sensibo/sensibo-python-sdk with py3 print fix

# should return a number "x.y"
TEMPERATURE_URL = (
    "https://www.temperatur.nu/termo/gettemp.php?stadname=partille&what=temp"
)
REGION = "SE3"
REGION_HOLIDAYS = holidays.country_holidays("SE")
TIME_ZONE = "CET"
# Expecting about 10% of heat energy to be leaked per hour
WORKDAY_MORNING = {
    "comfort_by_hour": 6,
    "comfort_until_hour": 7,
    "comfort_until_minute": 30,
    "idle_monitor_from_hour": 8,
}
DAYOFF_MORNING = {
    "comfort_by_hour": 8,
    "eat_until_hour": 9,
}
WORKDAY_MORNING_COMFORT_UNTIL_HOUR = 8
EARLIEST_AFTERNOON_PREHEAT_HOUR = 11  # Must be a pause since morning hour
BEGIN_AFTERNOON_HEATING_BY_HOUR = 14
WORKDAY_AFTERNOON_COMFORT_BY_HOUR = 16
WORKDAY_COMFORT_UNTIL_HOUR = 22
WEEKEND_COMFORT_UNTIL_HOUR = 23
DEGREES_PER_HOUR_DURING_RAMPUP = 1
SECONDS_BETWEEN_COMMANDS = 1.5
AT_HOME_DAYS = [5, 6, 7]
TRANSFER_AND_TAX_COST_PER_MWH_EXCL_VAT = 634.0
ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_REASONABLE = 750.0
RELATIVE_SEK_PER_MWH_TO_CONSIDER_REASONABLE_WHEN_COMPARED_TO_CHEAPEST = 600.0
ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_CHEAP = 300.0
ABSOLUTE_SEK_PER_MWH_BEYOND_WHICH_TO_REDUCE_COMFORT = 7000.0
MAX_HOURS_OF_REDUCED_COMFORT_PER_DAY = 3
MIN_OUTDOOR_TEMP_TO_REDUCE_COMFORT_AT = 2.0  # Pure electric heaters should be off above
MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE = 22.5
MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE = 20.0
MIN_FLOOR_SENSOR_IDLE_TEMPERATURE = 17.0

# Data for MSZ-FD35VA - at 100% compressor
HEATPUMP_COP_AT_PLUS7 = 3.0
HEATPUMP_COP_AT_PLUS2 = 2.8
HEATPUMP_COP_AT_MINUS7 = 2.3
HEATPUMP_COP_AT_MINUS15 = 2.1
HEATPUMP_HEATING_WATTS_AT_PLUS7 = 6600.0
HEATPUMP_HEATING_WATTS_AT_PLUS2 = 5600.0
HEATPUMP_HEATING_WATTS_AT_MINUS7 = 5200.0
HEATPUMP_HEATING_WATTS_AT_MINUS15 = 4300.0
# 5.6kW can be produced by above Mitsubishi heat pump at plus 2
# This info together with info that extra electrical heaters
# are needed if colder gives dissiapation of the home in watts
HEAT_DISSIPATION_WATTS_PER_DELTA_DEGREE = 320.0
WATTS_STORED_IN_BUILDING_PER_DELTA_DEGREE = 22500.0

COMFORT_PLUS_TEMP_DELTA = 2  # Int
IDLE_SETTINGS = {
    "on": True,
    "mode": "heat",
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 17,
}

MAX_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "high",
    "targetTemperature": 23,
}

COMFORT_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}

COMFORT_PLUS_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": COMFORT_HEAT_SETTINGS["targetTemperature"]
    + COMFORT_PLUS_TEMP_DELTA,
}

COMFORT_REDUCED_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "medium",
    "targetTemperature": 18,
}

HEAT_DISTRIBUTION_SETTINGS = {
    "targetTemperature": 16,  # Ignored,b ut needed during restore
    "mode": "fan",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "medium",
}

COMFORT_EATING_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedCenterRight",
    "swing": "fixedMiddle",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}


class PriceAnalyzer:
    def __init__(self):
        self._day_spot_prices = None
        self._cheap_hours = {}
        self._reasonably_priced_hours = None
        self._reduced_comfort_hours = None
        self._pre_heat_favorable_hours = None
        self._percent_additional_heat_leak_from_two_degrees_warmer = None
        self.significantly_more_expensive_after_midnight = False

    def cheap_morning_hour(self):
        return self._cheap_hours["morning"]

    def cheap_afternoon_hour(self):
        return self._cheap_hours["afternoon"]

    def is_hour_with_reduced_comfort(self, hour):
        return hour in self._reduced_comfort_hours

    def is_hour_reasonably_priced(self, hour):
        return hour in self._reasonably_priced_hours

    def is_hour_preheat_favorable(self, hour):
        return hour in self._pre_heat_favorable_hours

    def prepare_next_day(
        self, lookup_date, percent_additional_heat_leak_from_two_degrees_warmer
    ):
        spot_prices = elspot.Prices("SEK")
        self._percent_additional_heat_leak_from_two_degrees_warmer = (
            percent_additional_heat_leak_from_two_degrees_warmer
        )

        print(
            f"Getting prices for {lookup_date} to find cheap hours. Plus comfort loss is: "
            + f"{round(100.0 * percent_additional_heat_leak_from_two_degrees_warmer, 2)}"
            + " % given current outdoor temperature"
        )
        day_spot_prices = spot_prices.hourly(end_date=lookup_date, areas=[REGION])[
            "areas"
        ][REGION]["values"]

        if self._day_spot_prices is not None:
            lowest_price_first_three_hours = min(
                min(day_spot_prices[0]["value"], day_spot_prices[1]["value"]),
                day_spot_prices[2]["value"],
            )
            self.significantly_more_expensive_after_midnight = (
                TRANSFER_AND_TAX_COST_PER_MWH_EXCL_VAT + lowest_price_first_three_hours
            ) > self.cost_of_early_consumed_mwh(self._day_spot_prices[23]["value"])
            if self.significantly_more_expensive_after_midnight:
                print("Prepared to boost before midnight..")
        self._day_spot_prices = day_spot_prices

    def cost_of_early_consumed_mwh(self, raw_mwh_cost, nbr_of_hours_too_early=1):
        return (TRANSFER_AND_TAX_COST_PER_MWH_EXCL_VAT + raw_mwh_cost) * (
            1
            + self._percent_additional_heat_leak_from_two_degrees_warmer
            * nbr_of_hours_too_early
        )

    def process_preheat_favourable_hour(
        self,
        previous_hour_price,
        current_hour_price,
        previous_price_period_start_hour,
    ):
        if previous_hour_price is not None and (
            TRANSFER_AND_TAX_COST_PER_MWH_EXCL_VAT + current_hour_price
        ) > self.cost_of_early_consumed_mwh(previous_hour_price):
            self._pre_heat_favorable_hours.append(previous_price_period_start_hour)

    def find_warmup_hours(
        self,
        first_comfort_range,
        second_comfort_range,
    ):
        self._cheap_hours = {}
        local_tz = pytz.timezone(TIME_ZONE)
        lowest_price = None
        previous_hour_price = None
        self._reasonably_priced_hours = []
        self._pre_heat_favorable_hours = []
        for hour_price in self._day_spot_prices:
            if lowest_price is None or hour_price["value"] < lowest_price:
                lowest_price = hour_price["value"]

        curr_hour_idx = 0
        comfort_hours = {}
        for hour_price in self._day_spot_prices:
            price_period_start_hour = hour_price["start"].astimezone(local_tz).hour
            print(
                f"{hour_price['start'].astimezone(local_tz)} @ {hour_price['value']} SEK/MWh"
            )
            if (
                price_period_start_hour in first_comfort_range
                or price_period_start_hour == first_comfort_range.stop
            ):
                comfort_hours[
                    hour_price["value"] + 0.000001 * len(comfort_hours)
                ] = price_period_start_hour
            if second_comfort_range is not None and (
                price_period_start_hour in second_comfort_range
                or price_period_start_hour == second_comfort_range.stop
            ):
                comfort_hours[
                    hour_price["value"] + 0.000001 * len(comfort_hours)
                ] = price_period_start_hour

            self.process_preheat_favourable_hour(
                previous_hour_price, hour_price["value"], price_period_start_hour - 1
            )
            self.update_reasonably_priced_hours(
                hour_price["value"],
                curr_hour_idx,
                price_period_start_hour,
                lowest_price,
            )
            self.update_cheap_boost_hours(
                price_period_start_hour,
                hour_price["value"],
                price_period_start_hour < first_comfort_range.start,
            )
            curr_hour_idx += 1
            previous_hour_price = hour_price["value"]
        self.calculate_reduced_comfort_hours(comfort_hours)

    def update_reasonably_priced_hours(
        self, hour_price, curr_hour_idx, price_period_start_hour, lowest_price
    ):
        if (
            hour_price
            <= (
                lowest_price
                + RELATIVE_SEK_PER_MWH_TO_CONSIDER_REASONABLE_WHEN_COMPARED_TO_CHEAPEST
            )
        ) or hour_price <= ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_REASONABLE:
            if (
                (curr_hour_idx + 1) < len(self._day_spot_prices)
                and (
                    hour_price <= self._day_spot_prices[curr_hour_idx + 1]["value"]
                    or (price_period_start_hour - 1)
                    not in self._reasonably_priced_hours
                )
            ) or hour_price <= ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_CHEAP:
                self._reasonably_priced_hours.append(price_period_start_hour)

    def update_cheap_boost_hours(
        self, price_period_start_hour, hour_price, is_morning_hour
    ):
        if is_morning_hour:
            if "morning_price" not in self._cheap_hours or (
                hour_price + TRANSFER_AND_TAX_COST_PER_MWH_EXCL_VAT
            ) <= self.cost_of_early_consumed_mwh(
                self._cheap_hours["morning_price"],
                price_period_start_hour - self._cheap_hours["morning"],
            ):
                self._cheap_hours["morning_price"] = hour_price
                self._cheap_hours["morning"] = price_period_start_hour
        elif (
            EARLIEST_AFTERNOON_PREHEAT_HOUR
            <= price_period_start_hour
            <= BEGIN_AFTERNOON_HEATING_BY_HOUR
        ):
            if "afternoon_price" not in self._cheap_hours or (
                hour_price + TRANSFER_AND_TAX_COST_PER_MWH_EXCL_VAT
            ) <= self.cost_of_early_consumed_mwh(
                self._cheap_hours["afternoon_price"],
                price_period_start_hour - self._cheap_hours["afternoon"],
            ):
                self._cheap_hours["afternoon_price"] = hour_price
                self._cheap_hours["afternoon"] = price_period_start_hour

    def calculate_reduced_comfort_hours(self, comfort_hours):
        self._reduced_comfort_hours = []
        for comfort_hour_price, comfort_hour_start in sorted(
            comfort_hours.items(), reverse=True
        ):
            if comfort_hour_price > ABSOLUTE_SEK_PER_MWH_BEYOND_WHICH_TO_REDUCE_COMFORT:
                self._reduced_comfort_hours.append(comfort_hour_start)
                if (
                    len(self._reduced_comfort_hours)
                    >= MAX_HOURS_OF_REDUCED_COMFORT_PER_DAY
                ):
                    break


class TemperatureProvider:
    def __init__(self, controller):
        self.indoor_temperature = MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE
        self.outdoor_temperature = MIN_OUTDOOR_TEMP_TO_REDUCE_COMFORT_AT
        self.last_indoor_update = None
        self.last_outdoor_update = None
        self._controller = controller

    def get_indoor_temperature(self, verbose):
        if (
            self.last_indoor_update is not None
            and (self.last_indoor_update + timedelta(minutes=5)) > datetime.today()
        ):
            return self.indoor_temperature
        try:
            self.indoor_temperature = (
                self.indoor_temperature + self._controller.read_temperature()
            ) / 2
            sleep(SECONDS_BETWEEN_COMMANDS)
            self.last_indoor_update = datetime.today()
            if verbose:
                print(f"floor temperature: {self.indoor_temperature}")
        except requests.exceptions.ConnectionError:
            print(f"Ignoring temperature read error - using {self.indoor_temperature}")
        return self.indoor_temperature

    def get_outdoor_temperature(self, verbose):
        if (
            self.last_outdoor_update is not None
            and (self.last_outdoor_update + timedelta(minutes=5)) > datetime.today()
        ):
            return self.outdoor_temperature
        try:
            outdoor_temperature_req = requests.get(TEMPERATURE_URL, timeout=10.0)
            if outdoor_temperature_req.status_code == 200:
                try:
                    self.outdoor_temperature = float(outdoor_temperature_req.text)
                    self.last_outdoor_update = datetime.today()
                    if verbose:
                        print(f"outdoor temperature: {self.outdoor_temperature}")
                except ValueError:
                    print(
                        f"{outdoor_temperature_req.text} Temperature is not possible to use"
                        + f" - using {self.outdoor_temperature}"
                    )
        except requests.exceptions.ConnectionError:
            print(f"Ignoring temperature read error - using {self.outdoor_temperature}")
        return self.outdoor_temperature


class SensiboController:
    def __init__(self, client, uid, verbose):
        self._verbose = verbose
        self._client = client
        self._uid = uid
        self._current_settings = {}

    def apply_multi_settings(self, settings, force=False):
        if self._verbose:
            print(f"Applying: {settings}")
        if force:
            self._current_settings = {}
        first_setting = True
        for setting in settings:
            if (
                setting not in self._current_settings
                or settings[setting] != self._current_settings[setting]
            ):
                self._current_settings[setting] = settings[setting]
                self._client.pod_change_ac_state(
                    self._uid, None, setting, settings[setting]
                )
                if not first_setting:
                    sleep(SECONDS_BETWEEN_COMMANDS)
                first_setting = False

    def read_temperature(self):
        return self._client.pod_measurement(self._uid)[0]["temperature"]


class SensiboOptimizer:
    def __init__(self, verbose):
        self.verbose = verbose
        self._controller = None
        self._temperature_provider = None
        self._price_analyzer = PriceAnalyzer()
        self._step_1_overtemperature_distribution_active = False
        program_start_time = datetime.today()
        self._prev_midnight = datetime(
            program_start_time.year,
            program_start_time.month,
            program_start_time.day,
            0,
            0,
        )

    def wait_for_hour(self, hour, minute=0):
        pause.until(
            self._prev_midnight + timedelta(hours=hour, minutes=minute)
        )  # Direct return if in the past...
        if self.verbose:
            print(f"At {hour}:{str(minute).zfill(2)}")

    def get_current_floor_temp(self):
        return self._temperature_provider.get_indoor_temperature(self.verbose)

    def get_current_outdoor_temp(self):
        return self._temperature_provider.get_outdoor_temperature(self.verbose)

    def manage_over_temperature(self):
        if self._step_1_overtemperature_distribution_active:
            self._controller.apply_multi_settings(HEAT_DISTRIBUTION_SETTINGS)
        else:
            self._controller.apply_multi_settings(COMFORT_HEAT_SETTINGS)
            self._step_1_overtemperature_distribution_active = True

    def run_boost_rampup_to_comfort(
        self, idle_hour_start, boost_hour_start, short_boost, comfort_hour_start
    ):
        self.wait_for_hour(idle_hour_start)
        self.monitor_idle_period(idle_hour_start, boost_hour_start - 1)
        max_boost = self._price_analyzer.is_hour_preheat_favorable(boost_hour_start)
        if short_boost:
            self.manage_pre_boost(boost_hour_start - 1, max_boost)
        self.wait_for_hour(boost_hour_start)

        self.handle_boost(
            boost_hour_start,
            max_boost,
        )
        self.handle_post_boost(boost_hour_start + 1, comfort_hour_start)

    def monitor_idle_period(self, idle_hour_start, idle_hour_end):
        for pause_hour in range(idle_hour_start, idle_hour_end):
            for sample_minute in range(9, 60, 10):
                current_floor_sensor_value = self.get_current_floor_temp()
                if current_floor_sensor_value < MIN_FLOOR_SENSOR_IDLE_TEMPERATURE:
                    self._step_1_overtemperature_distribution_active = False
                    self._controller.apply_multi_settings(COMFORT_HEAT_SETTINGS)
                elif (
                    current_floor_sensor_value
                    >= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    self.manage_over_temperature()
                else:
                    self._step_1_overtemperature_distribution_active = False
                    self._controller.apply_multi_settings(IDLE_SETTINGS)
                self.wait_for_hour(pause_hour, sample_minute)

    def manage_pre_boost(self, pre_boost_hour_start, max_boost):
        if self.verbose:
            print("Short boost monitoring")
        self.wait_for_hour(pre_boost_hour_start)
        for sample_minute in range(9, 60, 10):
            current_floor_sensor_value = self.get_current_floor_temp()
            if current_floor_sensor_value < MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
                if max_boost:
                    self._controller.apply_multi_settings(MAX_HEAT_SETTINGS)
                else:
                    self._controller.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
            elif (
                not max_boost
                or current_floor_sensor_value
                >= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
            ):
                self._controller.apply_multi_settings(COMFORT_HEAT_SETTINGS)
            else:
                self._controller.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
            self.wait_for_hour(pre_boost_hour_start, sample_minute)

    def handle_boost(self, boost_hour_start, max_boost):
        if self.verbose:
            if max_boost:
                print("max boosting")
            else:
                print("mild boosting")
        for sample_minute in range(9, 60, 10):
            current_floor_sensor_value = self.get_current_floor_temp()
            if (
                current_floor_sensor_value < MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                and max_boost
            ):
                self._controller.apply_multi_settings(MAX_HEAT_SETTINGS)
            else:
                self._controller.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
            self.wait_for_hour(boost_hour_start, sample_minute)

    def handle_post_boost(self, post_boost_hour_start, comfort_hour_start):
        if self.verbose:
            print(
                f"Post boost monitoring {post_boost_hour_start} to {comfort_hour_start}"
            )
        pause_setting = copy.deepcopy(COMFORT_HEAT_SETTINGS)
        if post_boost_hour_start >= comfort_hour_start:
            if self.verbose:
                print("skipping post boost due to imminent comfort")
            self.wait_for_hour(post_boost_hour_start)
        for pause_hour in range(post_boost_hour_start, comfort_hour_start):
            self.wait_for_hour(pause_hour)
            pause_setting["targetTemperature"] = int(
                COMFORT_HEAT_SETTINGS["targetTemperature"]
                - (comfort_hour_start - pause_hour) * DEGREES_PER_HOUR_DURING_RAMPUP
            )
            if self._price_analyzer.is_hour_preheat_favorable(pause_hour):
                self._controller.apply_multi_settings(COMFORT_HEAT_SETTINGS)
            elif (
                pause_setting["targetTemperature"] < IDLE_SETTINGS["targetTemperature"]
            ):
                self._controller.apply_multi_settings(IDLE_SETTINGS)
            else:
                self._controller.apply_multi_settings(pause_setting)

    def run_workday_8_to_22_schedule(self):
        self.wait_for_hour(
            WORKDAY_MORNING["comfort_until_hour"],
            WORKDAY_MORNING["comfort_until_minute"],
        )

        self._controller.apply_multi_settings(IDLE_SETTINGS)
        self.run_boost_rampup_to_comfort(
            WORKDAY_MORNING["idle_monitor_from_hour"],
            self._price_analyzer.cheap_afternoon_hour(),
            self._price_analyzer.cheap_afternoon_hour()
            == BEGIN_AFTERNOON_HEATING_BY_HOUR,
            WORKDAY_AFTERNOON_COMFORT_BY_HOUR,
        )

        self.manage_comfort_hours(
            [WORKDAY_AFTERNOON_COMFORT_BY_HOUR], idle_after_comfort=False
        )

        self.wait_for_hour(17)
        self._controller.apply_multi_settings(COMFORT_EATING_HEAT_SETTINGS)

        self.manage_comfort_hours(range(18, WORKDAY_COMFORT_UNTIL_HOUR))

    def check_and_reset_overtemp(self, current_floor_sensor_value):
        if current_floor_sensor_value <= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE:
            self._step_1_overtemperature_distribution_active = False

    def manage_comfort_rampout(self, current_floor_sensor_value):
        if current_floor_sensor_value > MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
            self._controller.apply_multi_settings(COMFORT_REDUCED_HEAT_SETTINGS)
        else:
            self._controller.apply_multi_settings(COMFORT_HEAT_SETTINGS)

    def manage_comfort_hours(self, comfort_range, idle_after_comfort=True):
        for comfort_hour in comfort_range:
            self.wait_for_hour(comfort_hour)
            for sample_minute in range(9, 60, 10):
                current_floor_sensor_value = self.get_current_floor_temp()
                current_outdoor_temperature = self.get_current_outdoor_temp()
                self.check_and_reset_overtemp(current_floor_sensor_value)

                if (
                    current_outdoor_temperature > MIN_OUTDOOR_TEMP_TO_REDUCE_COMFORT_AT
                    and self._price_analyzer.is_hour_with_reduced_comfort(comfort_hour)
                ):
                    self._controller.apply_multi_settings(COMFORT_REDUCED_HEAT_SETTINGS)
                elif (
                    current_outdoor_temperature >= MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE
                ):
                    self._controller.apply_multi_settings(IDLE_SETTINGS)
                elif idle_after_comfort and comfort_hour == comfort_range[-1]:
                    self.manage_comfort_rampout(current_floor_sensor_value)
                elif current_floor_sensor_value < MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
                    if self._price_analyzer.is_hour_preheat_favorable(comfort_hour):
                        self._controller.apply_multi_settings(MAX_HEAT_SETTINGS)
                    else:
                        self._controller.apply_multi_settings(
                            COMFORT_PLUS_HEAT_SETTINGS
                        )
                elif (
                    current_floor_sensor_value
                    >= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    self.manage_over_temperature()
                elif self._price_analyzer.is_hour_reasonably_priced(comfort_hour) or (
                    sample_minute == 59  # boost 49-59 if price will rise
                    and self._price_analyzer.is_hour_preheat_favorable(comfort_hour)
                ):
                    self._controller.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
                else:
                    self._controller.apply_multi_settings(COMFORT_HEAT_SETTINGS)
                self.wait_for_hour(comfort_hour, sample_minute)

    def get_delta_degree_percent(self, delta):
        delta_degrees = self._temperature_provider.get_indoor_temperature(
            self.verbose
        ) - self._temperature_provider.get_outdoor_temperature(self.verbose)
        delta_degree_percent = 99.0  # Super high to disalbe comfort plus
        if delta_degrees > delta:
            delta_degree_percent = 1 - ((delta_degrees - delta) / delta_degrees)
        return delta_degree_percent

    def run(self, at_home_until_end_of, device_name, client):
        devices = client.devices()
        print("-" * 10, "devices", "-" * 10)
        print(devices)

        if len(devices) == 0:
            print("No devices present in account associated with API key...")
            sys.exit(0)

        if device_name is None:
            print("No device selected for optimization - exiting")
            sys.exit(0)

        uid = devices[device_name]
        self._controller = SensiboController(client, uid, self.verbose)
        self._temperature_provider = TemperatureProvider(self._controller)
        print("-" * 10, f"AC State of {device_name}", "_" * 10)
        try:
            print(client.pod_measurement(uid))
            ac_state = client.pod_ac_state(
                uid
            )  # If no result then stop/start in the Sensibo App
            print(ac_state)
        except IndexError:
            print(
                "Warning: Server does not know current state - try to stop/start in the Sensibo App"
            )
        self._price_analyzer.prepare_next_day(
            self._prev_midnight.date(),
            self.get_delta_degree_percent(COMFORT_PLUS_TEMP_DELTA),
        )
        if at_home_until_end_of is not None:
            at_home_until_end_of = datetime.strptime(at_home_until_end_of, "%Y-%m-%d")
        while True:
            optimizing_a_workday = (
                self._prev_midnight.date().isoweekday() not in AT_HOME_DAYS
            ) and self._prev_midnight.date() not in REGION_HOLIDAYS
            if (
                at_home_until_end_of is not None
                and at_home_until_end_of.date() >= self._prev_midnight.date()
            ):
                optimizing_a_workday = False
            comfort_heating_first_range = (
                range(
                    WORKDAY_MORNING["comfort_by_hour"],
                    WORKDAY_MORNING_COMFORT_UNTIL_HOUR,
                )
                if optimizing_a_workday
                else range(
                    DAYOFF_MORNING["comfort_by_hour"], WEEKEND_COMFORT_UNTIL_HOUR
                )
            )
            comfort_heating_second_range = (
                range(WORKDAY_AFTERNOON_COMFORT_BY_HOUR, WORKDAY_COMFORT_UNTIL_HOUR)
                if optimizing_a_workday
                else None
            )
            self._price_analyzer.find_warmup_hours(
                comfort_heating_first_range, comfort_heating_second_range
            )
            cheap_morning_hour = self._price_analyzer.cheap_morning_hour()

            self._controller.apply_multi_settings(IDLE_SETTINGS, True)

            self.run_boost_rampup_to_comfort(
                0,
                cheap_morning_hour,
                cheap_morning_hour == (comfort_heating_first_range.start - 1),
                comfort_heating_first_range.start,
            )

            self.manage_comfort_hours(
                [comfort_heating_first_range.start], idle_after_comfort=False
            )

            self.wait_for_hour(comfort_heating_first_range.start + 1)
            self._controller.apply_multi_settings(COMFORT_EATING_HEAT_SETTINGS)

            if optimizing_a_workday:
                self.run_workday_8_to_22_schedule()
            else:
                self.manage_comfort_hours(
                    range(DAYOFF_MORNING["eat_until_hour"], WEEKEND_COMFORT_UNTIL_HOUR)
                )

            self._controller.apply_multi_settings(IDLE_SETTINGS)

            self._price_analyzer.prepare_next_day(
                self._prev_midnight.date() + timedelta(days=1),
                self.get_delta_degree_percent(COMFORT_PLUS_TEMP_DELTA),
            )

            if self._price_analyzer.significantly_more_expensive_after_midnight:
                self.wait_for_hour(23)
                self.handle_boost(23, max_boost=True)
                self.wait_for_hour(24)

            self._prev_midnight += timedelta(days=1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sensibo nordpool heating optimizer")
    parser.add_argument(
        "apikey", type=str, help="API KEY from https://home.sensibo.com/me/api"
    )
    parser.add_argument(
        "-d", "--device", type=str, default=None, required=False, dest="deviceName"
    )
    parser.add_argument(
        "-e",
        "--extra-at-home-override-until-end-of",
        type=str,
        default=None,
        required=False,
        dest="atHomeOverrideUntilEndOf",
        help="Provide day off work comfort up to and including YYYY-MM-DD",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        default=False,
        required=False,
        dest="verbose",
        help="increase output verbosity",
        action="store_true",
    )
    args = parser.parse_args()

    optimizer = SensiboOptimizer(args.verbose)

    while True:
        try:
            fresh_sensibo_client = sensibo_client.SensiboClientAPI(args.apikey)
            optimizer.run(
                args.atHomeOverrideUntilEndOf, args.deviceName, fresh_sensibo_client
            )
        except requests.exceptions.ReadTimeout:
            print("Resetting optimizer due to error 2")
        except requests.exceptions.ConnectTimeout:
            print("Resetting optimizer due to error 3")
        except requests.exceptions.Timeout:
            print("Resetting optimizer due to error 1")
        except requests.exceptions.ConnectionError:
            print("Resetting optimizer due to error 5")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                print("403: check the API key")
                sys.exit(1)
            print("Resetting optimizer due to error 6")
        except requests.exceptions.RequestException:
            print("Resetting optimizer due to error 4")
        sleep(300)
