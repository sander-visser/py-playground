#!/usr/bin/env python3

"""
usage:
python3 -m pip install nordpool
python3 ellevio_hourly_costs.py
tested only with winter time exports and windows lineendings with a blank line at the end
"""

from datetime import date
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
                    most_expensive_hour_cost = int(
                        most_expensive_hour_sek_cost * SEK_TO_ORE
                    )
                    print(
                        f"\n{prev_day} kostade {round(day_cost, 2)}kr:"
                        + f"\nDyraste timmen började {most_expensive_hour}:00 "
                        + f"och förbrukningen kostade {most_expensive_hour_cost}öre."
                    )
                else:
                    first_day = this_day
                this_hour = CET_CEST_00_TO_01
                most_expensive_hour = this_hour
                day_spot_prices = spot_prices.hourly(end_date=this_day, areas=[region])[
                    "areas"
                ][region]["values"]
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

        most_expensive_hour_cost = int(most_expensive_hour_sek_cost * SEK_TO_ORE)
        print(
            f"\n{prev_day} kostade {round(day_cost, 2)}kr:"
            + f"\nDyraste timmen började {most_expensive_hour}:00 "
            + f"och förbrukningen kostade {most_expensive_hour_cost}öre"
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
            + "Total besparing för flyttad kWh från eftermiddag till kväll i perioden:"
            + f" {savings_per_moved_kwh_in_period}kr"
        )


if __name__ == "__main__":
    analyze_ellevio_hourly_costs(ELLEVIO_HOURLY_DATA, REGION)
