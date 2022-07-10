#!/usr/bin/env python3

"""
usage:
python3 -m pip install nordpool
python3 ellevio_hourly_costs.py
tested only with winter time exports and windows lineendings with a blank line at the end
"""

from datetime import date
from math import isinf
import csv
from nordpool import elspot

CET_CEST_00_TO_01 = 0
CET_CEST_06_TO_07 = 6
CET_CEST_17_TO_18 = 17
CET_CEST_20_TO_21 = 20
MW_TO_KW = 1000
SEK_TO_ORE = 100
REGION = "SE3"
ELLEVIO_HOURLY_DATA = "ellevio-export.csv"


def print_and_calc_move_saving(
    last_avg_price_17_to_20, lowest_price_17_to_07, savings_per_moved_kwh_in_period
):
    """
    Sums up the savings made by moving consumption, and outputs info for the last days savings
    """
    if last_avg_price_17_to_20 is not None:
        last_days_savings = last_avg_price_17_to_20 - lowest_price_17_to_07
        savings_per_moved_kwh_in_period += last_days_savings
        last_days_savings = int(last_days_savings * SEK_TO_ORE)
        if last_days_savings > 0:
            print(
                "Varje kWh som flyttas från 17-20 till billigaste timmen"
                + f" kommande natt sparar {last_days_savings}öre."
            )
    return savings_per_moved_kwh_in_period


def update_cheapest_hour(cheapest_hour_analysis, day_spot_prices):
    """
    Analyses what hour 00-07 on average is the cheapest in the analysed period
    """
    cheapest_hour_price = float("inf")
    cheapest_hour = 0
    curr_hour = 0
    for hour_price in day_spot_prices:
        if hour_price["value"] < cheapest_hour_price:
            cheapest_hour_price = hour_price["value"]
            cheapest_hour = curr_hour
        curr_hour = curr_hour + 1
        if curr_hour > CET_CEST_06_TO_07:
            break
    if cheapest_hour not in cheapest_hour_analysis:
        cheapest_hour_analysis[cheapest_hour] = 1
    else:
        cheapest_hour_analysis[cheapest_hour] = (
            cheapest_hour_analysis[cheapest_hour] + 1
        )
    return cheapest_hour_analysis


def print_last_day_info(
    day_spot_prices,
    most_expensive_hour_sek_cost,
    most_expensive_hour,
    prev_day,
    day_cost,
):
    """
    Prints the cost info from last day
    """
    most_expensive_hour_price = int(
        float(day_spot_prices[most_expensive_hour]["value"]) / MW_TO_KW * SEK_TO_ORE
    )
    most_expensive_hour_cost = int(most_expensive_hour_sek_cost * SEK_TO_ORE)
    print(
        f"\n{prev_day} kostade {round(day_cost, 2)}kr:"
        + f"\nDin dyraste timmen började {most_expensive_hour}:00 "
        + f"och förbrukningen denna timme kostade dig {most_expensive_hour_cost}öre."
        + f" ({most_expensive_hour_price} öre/kWh)"
    )


def analyze_ellevio_hourly_costs(csv_file_name, region):
    """
    Parses all rows in an Ellevio hourly consumption data export
    and looks up the raw costs from Nordpool
    File shall have windows line and use comma as separator
    Collumn A shall contain the start hour and be on syntax "2022-01-01 0:00"
    Collumn B shall contain the hours kilowatt usage on syntax "1.67"
    """
    print(
        "Kostnader i SEK utan certifikat, moms, påslag, skatter och elnät vid timmätt debitering"
    )

    spot_prices = elspot.Prices("SEK")

    with open(csv_file_name, mode="r", newline="", encoding="UTF-8") as csvfile:
        datareader = csv.reader(csvfile, delimiter=",")
        prev_day = None
        first_day = None
        most_expensive_hour = None
        most_expensive_hour_sek_cost = 0
        day_cost = 0
        total_cost = 0
        this_hour = None
        curr_avg_price_17_to_20 = None
        last_avg_price_17_to_20 = None
        lowest_price_17_to_07 = None
        savings_per_moved_kwh_in_period = 0
        day_spot_prices = {}
        cheapest_hour_analysis = {}
        for consumption_row in datareader:
            century = consumption_row[0][:2]
            if century != "20":
                # Skip labels
                continue

            this_day = date.fromisoformat(consumption_row[0].split()[0])
            this_hour_kw = float(consumption_row[1])
            if prev_day != this_day:
                last_avg_price_17_to_20 = curr_avg_price_17_to_20
                curr_avg_price_17_to_20 = 0
                if prev_day is not None:
                    print_last_day_info(
                        day_spot_prices,
                        most_expensive_hour_sek_cost,
                        most_expensive_hour,
                        prev_day,
                        day_cost,
                    )
                else:
                    first_day = this_day
                this_hour = CET_CEST_00_TO_01
                most_expensive_hour = this_hour
                day_spot_prices = spot_prices.hourly(end_date=this_day, areas=[region])[
                    "areas"
                ][region]["values"]
                cheapest_hour_analysis = update_cheapest_hour(
                    cheapest_hour_analysis, day_spot_prices
                )
                most_expensive_hour_sek_cost = (
                    this_hour_kw * float(day_spot_prices[this_hour]["value"]) / MW_TO_KW
                )
                total_cost = total_cost + day_cost
                day_cost = most_expensive_hour_sek_cost
                prev_day = this_day
            else:
                this_hour = this_hour + 1
                this_hour_cost = (
                    this_hour_kw * float(day_spot_prices[this_hour]["value"]) / MW_TO_KW
                )

                if isinf(this_hour_cost):
                    # Ignore spring summertime error
                    this_hour_cost = 0

                day_cost = day_cost + this_hour_cost

                if this_hour_cost > most_expensive_hour_sek_cost:
                    most_expensive_hour_sek_cost = this_hour_cost
                    most_expensive_hour = this_hour

            this_hour_price = float(day_spot_prices[this_hour]["value"]) / MW_TO_KW
            if CET_CEST_17_TO_18 <= this_hour <= CET_CEST_20_TO_21:
                curr_avg_price_17_to_20 += this_hour_price
                if this_hour != CET_CEST_17_TO_18:
                    curr_avg_price_17_to_20 = curr_avg_price_17_to_20 / 2
                if this_hour == CET_CEST_17_TO_18:
                    lowest_price_17_to_07 = this_hour_price
            if this_hour > CET_CEST_17_TO_18 or this_hour <= CET_CEST_06_TO_07:
                if (
                    lowest_price_17_to_07 is not None
                    and this_hour_price < lowest_price_17_to_07
                ):
                    lowest_price_17_to_07 = this_hour_price
            if this_hour == CET_CEST_06_TO_07:
                savings_per_moved_kwh_in_period = print_and_calc_move_saving(
                    last_avg_price_17_to_20,
                    lowest_price_17_to_07,
                    savings_per_moved_kwh_in_period,
                )

        print_last_day_info(
            day_spot_prices,
            most_expensive_hour_sek_cost,
            most_expensive_hour,
            prev_day,
            day_cost,
        )

        last_avg_price_17_to_20 = curr_avg_price_17_to_20
        savings_per_moved_kwh_in_period = print_and_calc_move_saving(
            last_avg_price_17_to_20,
            lowest_price_17_to_07,
            savings_per_moved_kwh_in_period,
        )

        total_cost = total_cost + day_cost
        savings_per_moved_kwh_in_period = int(savings_per_moved_kwh_in_period)
        print(
            f"\n\nTotal kostnad för perioden {first_day} tom {this_day}: {int(total_cost)}kr\n"
            + "Total besparing för varje daglig flyttad kWh från eftermiddag till kväll:"
            + f" (i perioden) {savings_per_moved_kwh_in_period}kr"
        )

        for cheapest_hour in sorted(cheapest_hour_analysis):
            print(
                f"Timmen som börjar {cheapest_hour}:00 var billigast"
                + f" {cheapest_hour_analysis[cheapest_hour]} dagar i perioden"
            )


if __name__ == "__main__":
    analyze_ellevio_hourly_costs(ELLEVIO_HOURLY_DATA, REGION)
