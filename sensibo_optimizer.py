#!/usr/bin/env python3

"""
Optimizer balancing comfort vs price for an IR remote controlled heatpump
- pre-heats the home in an optimal way when occupants are away or sleeping
- adapts wanted heating to price and outdoor temperature
Script is tested with a Sensibo Sky placed 20cm above floor level

MIT license (as the rest of the repo)

If you plan to migrate to Tibber electricity broker I can provide a referral
giving us both 500 SEK to shop gadgets with. Contact: github[a]visser.se

Usage (adapt constants as needed for your home=:
Install needed pip packages (see below pip module imports)
Run this script on a internet connected machine configured with relevant timezone
 - Tip: Use environment variable TZ='Europe/Stockholm'
"""

# pylint: disable=C0115,C0116  # Ignore docstring

from datetime import datetime, timedelta
from time import sleep
import sys
import copy
import math

# "python3 -m pip install X" below python modules
import requests
import pause
import holidays
from nordpool import elspot
import pytz
import sensibo_client  # https://github.com/Sensibo/sensibo-python-sdk with py3 print fix

# Location info
REGION = "SE3"
REGION_HOLIDAYS = holidays.country_holidays("SE")
TIME_ZONE = "CET"
# Each url in TEMPERATURE_URLS should return a number "x.y"
TEMPERATURE_URLS = [
    "https://www.temperatur.nu/termo/gettemp.php?stadname=partille&what=temp",
    "https://www.temperatur.nu/termo/gettemp.php?stadname=ojersjo&what=temp",
]
FORECAST_URL = (
    "https://opendata-download-metfcst.smhi.se/api/"
    + "category/pmp3g/version/2/geotype/point/lon/12.12860/lat/57.71934/data.json"
)

# Schedule info
WORKDAY_MORNING = {
    "comfort_by_hour": 6,
    "comfort_until_hour": 7,
    "comfort_until_minute": 30,
    "idle_monitor_from_hour": 8,
}
DAYOFF_MORNING = {
    "comfort_by_hour": 8,
}
WORKDAY_MORNING_COMFORT_UNTIL_HOUR = 8
EARLIEST_AFTERNOON_PREHEAT_HOUR = 11  # Must be a pause since morning hour
BEGIN_AFTERNOON_HEATING_BY_HOUR = 14
WORKDAY_AFTERNOON_COMFORT_BY_HOUR = 16
DINNER_HOUR = 17
WORKDAY_COMFORT_UNTIL_HOUR = 22
WEEKEND_COMFORT_UNTIL_HOUR = 23
SECONDS_BETWEEN_COMMANDS = 1.5
SCHOOL_DAYS = [1, 2, 3, 4, 5]
AT_HOME_DAYS = [5, 6, 7]

# Price info (excl VAT)
TRANSFER_AND_TAX_COST_PER_MWH_EXCL_VAT = 778.3  # incl broker fee
ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_REASONABLE = 750.0
RELATIVE_SEK_PER_MWH_TO_CONSIDER_REASONABLE_WHEN_COMPARED_TO_CHEAPEST = 600.0
ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_CHEAP = 300.0
ABSOLUTE_SEK_PER_MWH_BEYOND_WHICH_TO_REDUCE_COMFORT = 5500.0

# Heat pump data (for MSZ-FD35VA - at 100% compressor)
HEATPUMP_COP_AT_PLUS15 = 4.0  # Guestimate
HEATPUMP_COP_AT_PLUS7 = 3.3  # 3.0 at 100%, but assuming higher due to lower load
HEATPUMP_COP_AT_PLUS2 = 2.8
HEATPUMP_COP_AT_MINUS7 = 2.3
HEATPUMP_COP_AT_MINUS15 = 2.1
HEATPUMP_HEATING_WATTS_AT_PLUS7 = 6600.0
HEATPUMP_HEATING_WATTS_AT_PLUS2 = 5600.0
HEATPUMP_HEATING_WATTS_AT_MINUS7 = 5200.0
HEATPUMP_HEATING_WATTS_AT_MINUS15 = 4300.0

# Temperature and heating settings
COLD_OUTDOOR_TEMP = 1.0  # Increased fan speed below this temperature
HEATPUMP_LIMIT_COLD_OUTDOOR_TEMP = -4.5  # Pure electric heaters should be off above
EXTREMELY_COLD_OUTDOOR_TEMP = -8.0
MAX_HOURS_OF_REDUCED_COMFORT_PER_DAY = 3  # Will avoid two in a row
MAX_FLOOR_SENSOR_OVER_TEMPERATURE = 0.5
MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE = 20.0
MIN_FLOOR_SENSOR_IDLE_TEMPERATURE = 17.0
COMFORT_TEMPERATURE_HYSTERESIS = 0.75  # How far below comfort to aim for in idle
COMFORT_PLUS_TEMP_DELTA = int(2)
EXTRA_TEMP_OFFSET = int(1)
NORMAL_TEMP_OFFSET = int(0)
REDUCED_TEMP_OFFSET = int(-1)

# 5.2kW can be produced by above Mitsubishi heat pump at minus 7
# This info together with info that extra electrical heaters
# are needed if colder than -7 gives dissiapation of the home in watts
HEAT_DISSIPATION_WATTS_PER_DELTA_DEGREE = 193.0
WATT_HRS_STORED_IN_BUILDING_PER_DELTA_DEGREE = 3000.0
BUILDING_WINDCHILL_PERCENT_IMPACT = 0.20  # 20% impacted by wind

IDLE_SETTINGS = {
    "on": True,
    "mode": "heat",
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 17,
}
COMFORT_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}
HIGH_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "high",
    "targetTemperature": 22,
}
COMFORT_EATING_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedCenterRight",
    "swing": "fixedMiddle",
    "fanLevel": "medium_high",
    "targetTemperature": 21,
}


class PriceAnalyzer:
    def __init__(self, temperature_provider, heatpump_model):
        self._day_spot_prices = None
        self._cheap_hours = {}
        self._reasonably_priced_hours = None
        self._reduced_comfort_hours = None
        self._pre_heat_favorable_hours = None
        self._temperature_provider = temperature_provider
        self._heatpump_model = heatpump_model

    def cheap_morning_hour(self):
        return self._cheap_hours["morning"]

    def cheap_afternoon_hour(self):
        return self._cheap_hours["afternoon"]

    def is_hour_with_reduced_comfort(self, hour):
        return hour in self._reduced_comfort_hours

    def is_next_hour_cheaper(self, hour):
        if hour == 23:
            return True
        return (
            self._day_spot_prices[hour]["value"]
            > self._day_spot_prices[hour + 1]["value"]
        )

    def is_hour_reasonably_priced(self, hour):
        return hour in self._reasonably_priced_hours

    def is_hour_preheat_favorable(self, hour):
        return hour in self._pre_heat_favorable_hours

    def is_hour_longterm_preheat_favorable(self, early_hour, target_hour):
        if target_hour <= early_hour:
            print(
                f"Warning: unexpected longterm_preheat_favorable test {early_hour} {target_hour}"
            )
            return False

        current_hour_price = self.cost_of_early_consumed_mwh(
            self._day_spot_prices[early_hour]["value"],
            target_hour - early_hour,
            timedelta(hours=int((target_hour - early_hour) / 2)),
        )
        target_hour_price = self.cost_of_consumed_mwh(
            self._day_spot_prices[target_hour]["value"]
        )
        current_temperature = self._temperature_provider.get_outdoor_temperature()
        future_temperature = self._temperature_provider.get_forecasted_temperature(
            datetime.utcnow().replace(hour=target_hour)
        )
        if future_temperature is None:
            future_temperature = current_temperature
        current_cop = self._heatpump_model.get_cop(current_temperature)
        target_cop = self._heatpump_model.get_cop(future_temperature)
        return bool(
            (target_hour_price / target_cop) > (current_hour_price / current_cop)
        )

    def get_delta_degree_percent(self, delta, outdoor_estimated_temp):
        delta_degrees = (
            self._temperature_provider.get_indoor_temperature() - outdoor_estimated_temp
        )
        delta_degree_percent = 99.0  # Super high to disable comfort plus
        if delta_degrees > delta:
            delta_degree_percent = 1 - ((delta_degrees - delta) / delta_degrees)
        return delta_degree_percent

    def prepare_next_day(self, lookup_date):
        spot_prices = elspot.Prices("SEK")
        current_loss = self.get_delta_degree_percent(
            COMFORT_PLUS_TEMP_DELTA,
            self._temperature_provider.get_outdoor_temperature(),
        )
        print(
            f"Getting prices for {lookup_date} to find cheap hours. Plus comfort loss is: "
            + f"{round(100.0 * current_loss, 2)}"
            + " % given current outdoor temperature"
        )
        day_spot_prices = spot_prices.hourly(end_date=lookup_date, areas=[REGION])[
            "areas"
        ][REGION]["values"]

        significantly_more_expensive_after_midnight = False
        if self._day_spot_prices is not None:
            lowest_price_first_three_hours = min(
                day_spot_prices[0]["value"],
                day_spot_prices[1]["value"],
                day_spot_prices[2]["value"],
            )
            significantly_more_expensive_after_midnight = self.cost_of_consumed_mwh(
                lowest_price_first_three_hours
            ) > self.cost_of_early_consumed_mwh(self._day_spot_prices[23]["value"])
            if significantly_more_expensive_after_midnight:
                print("Prepared to boost before midnight..")
        self._day_spot_prices = day_spot_prices
        return significantly_more_expensive_after_midnight

    def cost_of_early_consumed_mwh(
        self, raw_mwh_cost, nbr_of_hours_too_early=1, temperature_time_delta=None
    ):
        outdoor_estimated_temp = None
        if temperature_time_delta is not None:
            outdoor_estimated_temp = (
                self._temperature_provider.get_forecasted_temperature(
                    datetime.utcnow() + temperature_time_delta,
                    BUILDING_WINDCHILL_PERCENT_IMPACT,
                )
            )
        if outdoor_estimated_temp is None:
            outdoor_estimated_temp = (
                self._temperature_provider.get_outdoor_temperature()
            )
        return self.cost_of_consumed_mwh(raw_mwh_cost) * (
            1
            + self.get_delta_degree_percent(
                COMFORT_PLUS_TEMP_DELTA, outdoor_estimated_temp
            )
            * nbr_of_hours_too_early
        )

    @staticmethod
    def cost_of_consumed_mwh(raw_mwh_cost):
        return TRANSFER_AND_TAX_COST_PER_MWH_EXCL_VAT + raw_mwh_cost

    def process_preheat_favourable_hour(
        self,
        previous_hour_price,
        current_hour_price,
        previous_price_period_start_hour,
    ):
        if previous_hour_price is not None and (
            self.cost_of_consumed_mwh(current_hour_price)
        ) > self.cost_of_early_consumed_mwh(
            previous_hour_price,
            temperature_time_delta=timedelta(hours=previous_price_period_start_hour),
        ):
            self._pre_heat_favorable_hours.append(previous_price_period_start_hour)

    def find_warmup_hours(self, first_comfort_range, second_comfort_range):
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
            ) or (
                second_comfort_range is not None
                and (
                    price_period_start_hour in second_comfort_range
                    or price_period_start_hour == second_comfort_range.stop
                )
            ):  # Store as comfort hour
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
            if "morning_price" not in self._cheap_hours or self.cost_of_consumed_mwh(
                hour_price
            ) < self.cost_of_early_consumed_mwh(
                self._cheap_hours["morning_price"],
                price_period_start_hour - self._cheap_hours["morning"],
                temperature_time_delta=timedelta(hours=price_period_start_hour),
            ):
                self._cheap_hours["morning_price"] = hour_price
                self._cheap_hours["morning"] = price_period_start_hour
        elif (
            EARLIEST_AFTERNOON_PREHEAT_HOUR
            <= price_period_start_hour
            <= BEGIN_AFTERNOON_HEATING_BY_HOUR
        ):
            if "afternoon_price" not in self._cheap_hours or self.cost_of_consumed_mwh(
                hour_price
            ) < self.cost_of_early_consumed_mwh(
                self._cheap_hours["afternoon_price"],
                price_period_start_hour - self._cheap_hours["afternoon"],
                temperature_time_delta=timedelta(hours=price_period_start_hour),
            ):
                self._cheap_hours["afternoon_price"] = hour_price
                self._cheap_hours["afternoon"] = price_period_start_hour

    def calculate_reduced_comfort_hours(self, comfort_hours):
        self._reduced_comfort_hours = []
        for comfort_hour_price, comfort_hour_start in sorted(
            comfort_hours.items(), reverse=True
        ):
            if comfort_hour_price > ABSOLUTE_SEK_PER_MWH_BEYOND_WHICH_TO_REDUCE_COMFORT:
                if ((comfort_hour_start - 1) in self._reduced_comfort_hours) or (
                    (comfort_hour_start + 1) in self._reduced_comfort_hours
                ):
                    continue  # Avoid reducing comfort two hours in a row
                self._reduced_comfort_hours.append(comfort_hour_start)
                if (
                    len(self._reduced_comfort_hours)
                    >= MAX_HOURS_OF_REDUCED_COMFORT_PER_DAY
                ):
                    break


class TemperatureProvider:
    def __init__(self, controller, verbose):
        self.indoor_temperature = MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE
        self.outdoor_temperature = HEATPUMP_LIMIT_COLD_OUTDOOR_TEMP
        self.last_indoor_update = None
        self.last_outdoor_update = None
        self._controller = controller
        self._last_forecast = None
        self._verbose = verbose

    def get_indoor_temperature(self):
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
            if self._verbose:
                print(f"Indoor temperature: {self.indoor_temperature:.2f}")
        except requests.exceptions.ConnectionError:
            print(
                f"Ignoring indoor temperature read error - using {self.indoor_temperature}"
            )
        return self.indoor_temperature

    @staticmethod
    def get_windchill_corrected_temp(
        temperature_forecast_impact, ws_kmph, windchill_percent
    ):
        return (temperature_forecast_impact * (1 - windchill_percent)) + (
            (
                (
                    13.12
                    + (0.6215 * temperature_forecast_impact)
                    - 11.37 * pow(ws_kmph, 0.16)
                    + 0.3965 * temperature_forecast_impact * pow(ws_kmph, 0.16)
                )
                * windchill_percent
            )
        )

    def get_forecasted_temperature(
        self, now_or_some_hours_ahead, windchill_percent=0.0
    ):
        temperature_forecast_impact = None
        if self._last_forecast is not None:
            rounded_time = now_or_some_hours_ahead.replace(
                microsecond=0, second=0, minute=0
            )
            for forecast_point in self._last_forecast:
                if forecast_point["validTime"] == rounded_time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ):
                    for param in forecast_point["parameters"]:
                        if param["name"] == "t":
                            temperature_forecast_impact = param["values"][0]
                        if (
                            param["name"] == "ws"
                            and temperature_forecast_impact is not None
                        ):
                            ws_kmph = 3.6 * param["values"][0]
                            temperature_forecast_impact = (
                                self.get_windchill_corrected_temp(
                                    temperature_forecast_impact,
                                    ws_kmph,
                                    windchill_percent,
                                )
                            )
                    break
        if self._verbose and temperature_forecast_impact is not None:
            print(
                f"Forcasted temperature {temperature_forecast_impact} at {rounded_time}"
            )
        return temperature_forecast_impact

    def update_outdoor_temperature(self):
        temperature_sum = 0.0
        sources = int(0)
        try:
            forecast_req = requests.get(url=FORECAST_URL, timeout=10.0)
            if forecast_req.status_code == 200:
                self._last_forecast = forecast_req.json()["timeSeries"]
        except requests.exceptions.ConnectionError:
            print(f"Warning: forecast read error from {FORECAST_URL}")
        for temperature_url in TEMPERATURE_URLS:
            try:
                outdoor_temperature_req = requests.get(temperature_url, timeout=10.0)
                if outdoor_temperature_req.status_code == 200:
                    try:
                        temperature_sum += float(outdoor_temperature_req.text)
                        sources += int(1)
                        self.last_outdoor_update = datetime.today()
                    except ValueError:
                        print(
                            f"Ignored {outdoor_temperature_req.text} from {temperature_url}"
                        )
            except requests.exceptions.ConnectionError:
                print(f"Ignoring outdoor temperature read error from {temperature_url}")
        forecasted_temp = self.get_forecasted_temperature(
            datetime.utcnow() + timedelta(hours=1)
        )
        if forecasted_temp is not None:
            temperature_sum += float(forecasted_temp)
            sources += int(1)
        if sources > int(0):
            self.outdoor_temperature = temperature_sum / sources
        if self._verbose:
            print(
                f"Outdoor temp (based on {sources} sources): {self.outdoor_temperature:.2f}"
            )

    def get_outdoor_temperature(self):
        if (
            self.last_outdoor_update is None
            or (self.last_outdoor_update + timedelta(minutes=5)) < datetime.today()
        ):
            self.update_outdoor_temperature()
        return self.outdoor_temperature


class SensiboController:
    def __init__(self, client, uid, verbose):
        self._verbose = verbose
        self._client = client
        self._uid = uid
        self._current_settings = {}

    def last_requested_setting(self, setting):
        if setting in self._current_settings:
            return self._current_settings[setting]
        return None

    def apply(self, settings, temp_offset=NORMAL_TEMP_OFFSET, force=False):
        adjusted_settings = copy.deepcopy(settings)
        adjusted_settings["targetTemperature"] += temp_offset
        if self._verbose:
            print(f"Applying: {adjusted_settings}")
        if force:
            self._current_settings = {}
        first_setting = True
        for setting in adjusted_settings:
            if (
                setting not in self._current_settings
                or adjusted_settings[setting] != self._current_settings[setting]
            ):
                self._current_settings[setting] = adjusted_settings[setting]
                self._client.pod_change_ac_state(
                    self._uid, None, setting, adjusted_settings[setting]
                )
                if not first_setting:
                    sleep(SECONDS_BETWEEN_COMMANDS)
                first_setting = False

    def read_temperature(self):
        return self._client.pod_measurement(self._uid)[0]["temperature"]


class HeatpumpModel:
    @staticmethod
    def interpolate_linear(mid_x, upper_x, lower_x, upper_y, lower_y):
        return lower_y + (mid_x - lower_x) * ((upper_y - lower_y) / (upper_x - lower_x))

    def get_cop(self, outside_temp):
        current_heating_cop = HEATPUMP_COP_AT_PLUS15
        if outside_temp < 15.0:
            current_heating_cop = self.interpolate_linear(
                outside_temp,
                15,
                7,
                HEATPUMP_COP_AT_PLUS15,
                HEATPUMP_COP_AT_PLUS7,
            )
        if outside_temp < 7.0:
            current_heating_cop = self.interpolate_linear(
                outside_temp,
                7,
                2,
                HEATPUMP_COP_AT_PLUS7,
                HEATPUMP_COP_AT_PLUS2,
            )
        if outside_temp < 2.0:
            current_heating_cop = self.interpolate_linear(
                outside_temp,
                2,
                -7,
                HEATPUMP_COP_AT_PLUS2,
                HEATPUMP_COP_AT_MINUS7,
            )
        if outside_temp < -7.0:
            current_heating_cop = self.interpolate_linear(
                outside_temp,
                -7,
                -15,
                HEATPUMP_COP_AT_MINUS7,
                HEATPUMP_COP_AT_MINUS15,
            )
        if outside_temp <= -15.0:
            current_heating_cop = HEATPUMP_COP_AT_MINUS15
        return current_heating_cop

    def get_current_capacity(self, outside_temp):
        current_heating_watts = HEATPUMP_HEATING_WATTS_AT_PLUS7
        if outside_temp < 7.0:
            current_heating_watts = self.interpolate_linear(
                outside_temp,
                7,
                2,
                HEATPUMP_HEATING_WATTS_AT_PLUS7,
                HEATPUMP_HEATING_WATTS_AT_PLUS2,
            )
        if outside_temp < 2.0:
            current_heating_watts = self.interpolate_linear(
                outside_temp,
                2,
                -7,
                HEATPUMP_HEATING_WATTS_AT_PLUS2,
                HEATPUMP_HEATING_WATTS_AT_MINUS7,
            )
        if outside_temp < -7.0:
            current_heating_watts = self.interpolate_linear(
                outside_temp,
                -7,
                -15,
                HEATPUMP_HEATING_WATTS_AT_MINUS7,
                HEATPUMP_HEATING_WATTS_AT_MINUS15,
            )
        if outside_temp <= -15.0:
            current_heating_watts = HEATPUMP_HEATING_WATTS_AT_MINUS15
        return current_heating_watts


class SensiboOptimizer:
    def __init__(self, verbose, heatpump_model):
        self.verbose = verbose
        self._controller = None
        self._temperature_provider = None
        self._price_analyzer = None
        self._step_1_overtemperature_distribution_active = False
        self._heatpump_model = heatpump_model
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
            self._prev_midnight + timedelta(hours=hour, minutes=minute, seconds=35)
        )  # Direct return if in the past...
        if self.verbose:
            print(f"At {hour}:{str(minute).zfill(2)}")

    def get_current_floor_temp(self):
        return self._temperature_provider.get_indoor_temperature()

    def get_current_outdoor_temp(self):
        return self._temperature_provider.get_outdoor_temperature()

    def allowed_over_temperature(self):
        target_temp = max(
            MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE,
            self._controller.last_requested_setting("targetTemperature"),
        )
        target_temp += MAX_FLOOR_SENSOR_OVER_TEMPERATURE
        return min(target_temp, HIGH_HEAT_SETTINGS["targetTemperature"])

    def manage_over_temperature(self):
        heat_distribution_settings = {
            "targetTemperature": 16,  # Ignored, but needed during restore
            "mode": "fan",
            "horizontalSwing": "fixedLeft",
            "swing": "fixedTop",
            "fanLevel": "medium",
        }
        if self.get_current_outdoor_temp() < COLD_OUTDOOR_TEMP:
            heat_distribution_settings["fanLevel"] = "medium_high"
            if self.get_current_outdoor_temp() < HEATPUMP_LIMIT_COLD_OUTDOOR_TEMP:
                heat_distribution_settings["fanLevel"] = "high"
        if self._step_1_overtemperature_distribution_active:
            self._controller.apply(heat_distribution_settings)
        else:
            if self.get_current_outdoor_temp() < COLD_OUTDOOR_TEMP:
                self._controller.apply(COMFORT_HEAT_SETTINGS, COMFORT_PLUS_TEMP_DELTA)
            else:
                self._controller.apply(COMFORT_HEAT_SETTINGS)
            self._step_1_overtemperature_distribution_active = True

    def run_boost_rampup_to_comfort(
        self, idle_hour_start, boost_hour_start, comfort_hour_start
    ):
        was_extra_boosting = False
        pre_boost_offset = None
        self.wait_for_hour(idle_hour_start)
        for pre_boost_hour in range(idle_hour_start, boost_hour_start + 1):
            preheating_for_pre_comfort_is_favorable = (
                self._price_analyzer.is_hour_longterm_preheat_favorable(
                    pre_boost_hour, comfort_hour_start - 1
                )
            )
            preheating_for_comfort_is_favorable = (
                self._price_analyzer.is_hour_longterm_preheat_favorable(
                    pre_boost_hour, comfort_hour_start
                )
            )
            preheating_for_future_comfort_is_favorable = (
                self._price_analyzer.is_hour_longterm_preheat_favorable(
                    pre_boost_hour, comfort_hour_start + 1
                )
                or self._price_analyzer.is_hour_longterm_preheat_favorable(
                    pre_boost_hour, comfort_hour_start + 2
                )
            )
            allowed_boost_degrees = (
                MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE - self.get_current_floor_temp()
            )
            if (
                preheating_for_pre_comfort_is_favorable
                or preheating_for_comfort_is_favorable
                or preheating_for_future_comfort_is_favorable
            ):
                allowed_boost_degrees += COMFORT_PLUS_TEMP_DELTA
            preboost_start_hour = boost_hour_start
            boost_capacity = 0.0
            while boost_capacity < allowed_boost_degrees:
                future_temperature = (
                    self._temperature_provider.get_forecasted_temperature(
                        datetime.utcnow().replace(hour=preboost_start_hour)
                    )
                )
                boost_capacity += self.get_current_heating_capacity(
                    1, future_temperature
                )
                preboost_start_hour -= 1
                if preboost_start_hour <= idle_hour_start:
                    preboost_start_hour = idle_hour_start
                    break

            if pre_boost_hour < preboost_start_hour:
                self.monitor_idle_period(
                    pre_boost_hour, pre_boost_hour + 1, comfort_hour_start
                )
                continue

            if self.verbose:
                cheap_boost = self._price_analyzer.is_hour_preheat_favorable(
                    pre_boost_hour
                )
                print(
                    "Boosting based on:\n"
                    + f"   comfort: {preheating_for_comfort_is_favorable}\n"
                    + f"   future comfort: {preheating_for_future_comfort_is_favorable}\n"
                    + f"   preheat during boost: {cheap_boost}\n"
                    + f"   was_extra_boosting: {was_extra_boosting}\n"
                    + f"   cold outside: {self.get_current_outdoor_temp() < COLD_OUTDOOR_TEMP}"
                )

            if (
                preheating_for_comfort_is_favorable
                and preheating_for_future_comfort_is_favorable
            ):
                pre_boost_offset = EXTRA_TEMP_OFFSET
                was_extra_boosting = True
            elif (
                self._price_analyzer.is_hour_preheat_favorable(pre_boost_hour)
                or preheating_for_comfort_is_favorable
                or preheating_for_future_comfort_is_favorable
                or self.get_current_outdoor_temp() < COLD_OUTDOOR_TEMP
                or was_extra_boosting
            ):
                was_extra_boosting = False
                pre_boost_offset = NORMAL_TEMP_OFFSET
            else:
                was_extra_boosting = False
                pre_boost_offset = REDUCED_TEMP_OFFSET
            future_temperature = self._temperature_provider.get_forecasted_temperature(
                datetime.utcnow().replace(hour=comfort_hour_start)
            )
            self.manage_pre_boost(
                pre_boost_hour,
                pre_boost_offset,
                self.get_current_heating_capacity(
                    comfort_hour_start - pre_boost_hour, future_temperature
                ),
            )
        self.wait_for_hour(boost_hour_start)

        self.monitor_idle_period(
            boost_hour_start + 1, comfort_hour_start, comfort_hour_start
        )

    def monitor_idle_period(self, idle_hour_start, idle_hour_end, comfort_hour_start):
        if idle_hour_start >= idle_hour_end:
            if self.verbose:
                print(
                    f"Skipping idle period monitoring ({idle_hour_start} >= {idle_hour_end})"
                )
            self.wait_for_hour(idle_hour_start)
        for pause_hour in range(idle_hour_start, idle_hour_end):
            for sample_minute in range(9, 60, 10):
                current_floor_sensor_value = self.get_current_floor_temp()
                if current_floor_sensor_value >= self.allowed_over_temperature():
                    self.manage_over_temperature()
                else:
                    self._step_1_overtemperature_distribution_active = False
                    idle_ends_in_comfort = idle_hour_end == comfort_hour_start
                    if (
                        idle_ends_in_comfort
                        and self._price_analyzer.is_hour_longterm_preheat_favorable(
                            pause_hour, comfort_hour_start
                        )
                    ):
                        self._controller.apply(HIGH_HEAT_SETTINGS)
                    else:
                        comfort_offset = (
                            0
                            if idle_ends_in_comfort
                            else COMFORT_TEMPERATURE_HYSTERESIS
                        )
                        self.apply_rampup_to_comfort(
                            comfort_hour_start - pause_hour, comfort_offset
                        )
                self.wait_for_hour(pause_hour, sample_minute)

    def manage_pre_boost(
        self, pre_boost_hour_start, boost_offset, current_heating_capacity
    ):
        if self.verbose:
            print(
                f"Pre boosting. Boost target offset: {boost_offset}, "
                + f"Current boosting capacity {current_heating_capacity}"
            )
        self.wait_for_hour(pre_boost_hour_start)
        for sample_minute in range(9, 60, 10):
            current_floor_sensor_value = self.get_current_floor_temp()
            if current_floor_sensor_value >= self.allowed_over_temperature():
                self._controller.apply(COMFORT_HEAT_SETTINGS)
            else:
                pre_boost_setting = copy.deepcopy(HIGH_HEAT_SETTINGS)
                pre_boost_setting["targetTemperature"] = math.ceil(
                    (pre_boost_setting["targetTemperature"] + boost_offset)
                    - current_heating_capacity
                )
                if (
                    pre_boost_setting["targetTemperature"]
                    <= IDLE_SETTINGS["targetTemperature"]
                ):
                    pre_boost_setting["targetTemperature"] = IDLE_SETTINGS[
                        "targetTemperature"
                    ]
                self._controller.apply(pre_boost_setting)
            self.wait_for_hour(pre_boost_hour_start, sample_minute)

    def get_current_heating_capacity(self, heating_hours, outside_temp=None):
        if outside_temp is None:
            outside_temp = self._temperature_provider.get_forecasted_temperature(
                datetime.utcnow() + timedelta(hours=heating_hours)
            )
            if outside_temp is None:
                outside_temp = self.get_current_outdoor_temp()
        delta_temp = MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE - outside_temp
        if delta_temp <= 0:
            return 100.0

        current_dissipation = HEAT_DISSIPATION_WATTS_PER_DELTA_DEGREE * delta_temp
        boost_watts = (
            self._heatpump_model.get_current_capacity(outside_temp)
            - current_dissipation
        )
        heating_capacity = heating_hours * (
            boost_watts / WATT_HRS_STORED_IN_BUILDING_PER_DELTA_DEGREE
        )
        if self.verbose:
            print(f"Can boost {heating_capacity:.2f} degrees in {heating_hours} hours")
        if heating_capacity <= 0.0:
            return 0.0
        return heating_capacity

    def apply_rampup_to_comfort(self, hours_remaining_til_comfort, rampup_offset=0):
        pause_setting = (
            copy.deepcopy(COMFORT_HEAT_SETTINGS)
            if self.get_current_outdoor_temp() > COLD_OUTDOOR_TEMP
            else copy.deepcopy(HIGH_HEAT_SETTINGS)
        )
        pause_setting["targetTemperature"] = math.ceil(
            COMFORT_HEAT_SETTINGS["targetTemperature"]
            - (
                self.get_current_heating_capacity(hours_remaining_til_comfort)
                + rampup_offset
            )
        )
        if pause_setting["targetTemperature"] <= IDLE_SETTINGS["targetTemperature"]:
            self._controller.apply(IDLE_SETTINGS)
        else:
            self._controller.apply(pause_setting)

    def run_workday_8_to_22_schedule(self):
        self.wait_for_hour(
            WORKDAY_MORNING["comfort_until_hour"],
            WORKDAY_MORNING["comfort_until_minute"],
        )

        self._controller.apply(IDLE_SETTINGS)

        self.run_boost_rampup_to_comfort(
            WORKDAY_MORNING["idle_monitor_from_hour"],
            self._price_analyzer.cheap_afternoon_hour(),
            WORKDAY_AFTERNOON_COMFORT_BY_HOUR,
        )

        self.manage_comfort_hours(
            [WORKDAY_AFTERNOON_COMFORT_BY_HOUR], idle_after_comfort=False
        )

        self.wait_for_hour(DINNER_HOUR)
        self._controller.apply(COMFORT_EATING_HEAT_SETTINGS)

        self.manage_comfort_hours(range(DINNER_HOUR + 1, WORKDAY_COMFORT_UNTIL_HOUR))

    def apply_comfort_boost(self, comfort_hour, boost_distance):
        if boost_distance > COMFORT_TEMPERATURE_HYSTERESIS:
            if self._price_analyzer.is_hour_preheat_favorable(comfort_hour):
                self._controller.apply(HIGH_HEAT_SETTINGS)
            else:
                self._controller.apply(
                    COMFORT_HEAT_SETTINGS, temp_offset=COMFORT_PLUS_TEMP_DELTA
                )
        else:
            if self._price_analyzer.is_hour_preheat_favorable(comfort_hour):
                self._controller.apply(
                    COMFORT_HEAT_SETTINGS, temp_offset=COMFORT_PLUS_TEMP_DELTA
                )
            else:
                self._controller.apply(
                    COMFORT_HEAT_SETTINGS, temp_offset=EXTRA_TEMP_OFFSET
                )

    def apply_cold_comfort(self, current_outdoor_temperature, preheat_favorable):
        cold_temp_offset = NORMAL_TEMP_OFFSET
        if current_outdoor_temperature < EXTREMELY_COLD_OUTDOOR_TEMP:
            cold_temp_offset = EXTRA_TEMP_OFFSET
        elif (
            current_outdoor_temperature > HEATPUMP_LIMIT_COLD_OUTDOOR_TEMP
            and not preheat_favorable
        ):
            cold_temp_offset = REDUCED_TEMP_OFFSET
        self._controller.apply(HIGH_HEAT_SETTINGS, temp_offset=cold_temp_offset)

    def apply_comfort_rampout(self, current_floor_sensor_value):
        if current_floor_sensor_value > MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
            self._controller.apply(COMFORT_HEAT_SETTINGS, REDUCED_TEMP_OFFSET)
        else:
            self._controller.apply(COMFORT_HEAT_SETTINGS)

    def manage_comfort(self, comfort_hour, sample_minute, last_comfort_hour):
        current_floor_sensor_value = self.get_current_floor_temp()
        extra_boost = self._price_analyzer.is_hour_reasonably_priced(comfort_hour) or (
            sample_minute == 59  # boost 49-59 if price will rise
            and self._price_analyzer.is_hour_preheat_favorable(comfort_hour)
        )
        if current_floor_sensor_value < self.allowed_over_temperature():
            self._step_1_overtemperature_distribution_active = False

        if self.get_current_outdoor_temp() >= MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
            self._controller.apply(IDLE_SETTINGS)
        elif last_comfort_hour:
            self.apply_comfort_rampout(current_floor_sensor_value)
        elif current_floor_sensor_value >= self.allowed_over_temperature():
            self.manage_over_temperature()
        elif self.get_current_outdoor_temp() < COLD_OUTDOOR_TEMP:
            if (
                self.get_current_outdoor_temp() > HEATPUMP_LIMIT_COLD_OUTDOOR_TEMP
            ) and self._price_analyzer.is_hour_with_reduced_comfort(comfort_hour):
                self._controller.apply(COMFORT_HEAT_SETTINGS)
            else:
                self.apply_cold_comfort(self.get_current_outdoor_temp(), extra_boost)
        elif (
            self._price_analyzer.is_next_hour_cheaper(comfort_hour)
            and (sample_minute == 59)
            or self._price_analyzer.is_hour_with_reduced_comfort(comfort_hour)
        ):
            self._controller.apply(COMFORT_HEAT_SETTINGS, REDUCED_TEMP_OFFSET)
        elif current_floor_sensor_value < MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
            self.apply_comfort_boost(
                comfort_hour,
                MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE - current_floor_sensor_value,
            )
        elif extra_boost:
            self._controller.apply(
                COMFORT_HEAT_SETTINGS, temp_offset=COMFORT_PLUS_TEMP_DELTA
            )
        else:
            self._controller.apply(COMFORT_HEAT_SETTINGS)
        self.wait_for_hour(comfort_hour, sample_minute)

    def manage_comfort_hours(self, comfort_range, idle_after_comfort=True):
        for comfort_hour in comfort_range:
            self.wait_for_hour(comfort_hour)
            for sample_minute in range(9, 60, 10):
                self.manage_comfort(
                    comfort_hour,
                    sample_minute,
                    idle_after_comfort and comfort_hour == comfort_range[-1],
                )

    def run(self, at_home_until_end_of, device_name, client):
        devices = client.devices()
        if len(devices) == 0:
            print("No devices present in account associated with API key...")
            sys.exit(0)
        print(f"----- devices -----\n{devices}")
        if device_name is None:
            print("No device selected for optimization - exiting")
            sys.exit(0)

        uid = devices[device_name]
        self._controller = SensiboController(client, uid, self.verbose)
        self._temperature_provider = TemperatureProvider(self._controller, self.verbose)
        self._price_analyzer = PriceAnalyzer(
            self._temperature_provider, self._heatpump_model
        )
        print("-" * 5, f"AC State of {device_name}", "-" * 5)
        try:
            print(client.pod_measurement(uid))
            print(client.pod_ac_state(uid))
        except IndexError:
            print(
                "Warning: Server does not know current state - try to stop/start in the Sensibo App"
            )
        self._price_analyzer.prepare_next_day(self._prev_midnight.date())
        if at_home_until_end_of is not None:
            at_home_until_end_of = datetime.strptime(at_home_until_end_of, "%Y-%m-%d")
        while True:
            optimizing_a_schoolday = (
                self._prev_midnight.date().isoweekday() in SCHOOL_DAYS
            ) and self._prev_midnight.date() not in REGION_HOLIDAYS
            optimizing_a_workday = (
                self._prev_midnight.date().isoweekday() not in AT_HOME_DAYS
            ) and self._prev_midnight.date() not in REGION_HOLIDAYS
            if (
                at_home_until_end_of is not None
                and at_home_until_end_of.date() >= self._prev_midnight.date()
            ):
                optimizing_a_workday = False
            if self.verbose:
                print(
                    f"Optimizing {self._prev_midnight.date()}. "
                    + f"Workday: {optimizing_a_workday} Schoolday: {optimizing_a_schoolday}"
                )
            comfort_first_range = (
                range(
                    WORKDAY_MORNING["comfort_by_hour"],
                    WORKDAY_MORNING_COMFORT_UNTIL_HOUR,
                )
                if optimizing_a_workday
                else range(
                    WORKDAY_MORNING["comfort_by_hour"], WEEKEND_COMFORT_UNTIL_HOUR
                )
                if optimizing_a_schoolday
                else range(
                    DAYOFF_MORNING["comfort_by_hour"], WEEKEND_COMFORT_UNTIL_HOUR
                )
            )
            comfort_second_range = (
                range(WORKDAY_AFTERNOON_COMFORT_BY_HOUR, WORKDAY_COMFORT_UNTIL_HOUR)
                if optimizing_a_workday
                else None
            )
            self._price_analyzer.find_warmup_hours(
                comfort_first_range, comfort_second_range
            )
            cheap_morning_hour = self._price_analyzer.cheap_morning_hour()

            self._controller.apply(IDLE_SETTINGS, force=True)
            self.wait_for_hour(0)

            self.run_boost_rampup_to_comfort(
                0,
                cheap_morning_hour,
                comfort_first_range.start,
            )

            self.manage_comfort_hours(
                [comfort_first_range.start], idle_after_comfort=False
            )

            self.wait_for_hour(comfort_first_range.start + 1)
            self._controller.apply(COMFORT_EATING_HEAT_SETTINGS)

            if optimizing_a_workday:
                self.run_workday_8_to_22_schedule()
            else:
                self.manage_comfort_hours(comfort_first_range[2:])
            self._controller.apply(IDLE_SETTINGS)

            significantly_more_expensive_after_midnight = (
                self._price_analyzer.prepare_next_day(
                    self._prev_midnight.date() + timedelta(days=1)
                )
            )

            if significantly_more_expensive_after_midnight:
                self.monitor_idle_period(
                    22, 23, (24 + WORKDAY_MORNING["comfort_by_hour"])
                )
                self.manage_pre_boost(
                    23,
                    NORMAL_TEMP_OFFSET,
                    self.get_current_heating_capacity(comfort_first_range[0] + 1),
                )
            self.monitor_idle_period(23, 24, (24 + WORKDAY_MORNING["comfort_by_hour"]))

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
        help="Provide at home comfort up to and including YYYY-MM-DD",
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
    optimizer = SensiboOptimizer(args.verbose, HeatpumpModel())

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
            print(f"Resetting optimizer due to error 6 {e.response.status_code}")
        except requests.exceptions.RequestException:
            print("Resetting optimizer due to error 4")
        sleep(300)
