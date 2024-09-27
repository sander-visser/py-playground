#!/usr/bin/env python

"""
Fetch Sector Alarm armed status


Usage:
Install needed pip packages (see below pip module imports)
"""

import requests

USER_NAME = "user@example.com"
PASSWORD = "secret"
USER_HOME = "https://minasidor.sectoralarm.se"

WEB_APP_CLIENT_ID = "keCW6wogC4jMscX1CdZ1WAKmLhcGdlHo"
COMPANY_HOME = "https://minside.sectoralarm.no"
API_TIMEOUT = 10.0
HTTPS_VERIFY = False  # Set True unless behind unsupported proxy

OAUTH_ENDPOINT = "https://login.sectoralarm.com/oauth/token"
USER_ENDPOINT = "https://mypagesapi.sectoralarm.net/api/Login/GetUser"
PANEL_STATUS_ENDPOINT = (
    "https://mypagesapi.sectoralarm.net/api/Panel/GetPanelStatus?panelId="
)
request_body = {
    "password": PASSWORD,
    "username": USER_NAME,
    "client_id": WEB_APP_CLIENT_ID,
    "grant_type": "password",
    "redirect_uri": USER_HOME,
    "audience": COMPANY_HOME,
}
headers = {"Content-Type": "application/x-www-form-urlencoded"}
response = requests.post(
    OAUTH_ENDPOINT, data=request_body, verify=HTTPS_VERIFY, timeout=API_TIMEOUT
)
if (response.status_code != 200)
{
    print(response.text)
}
api_access_token = response.json()["access_token"]

api_header = {
    "accept": "application/json, text/plain, */*",
    "authorization": api_access_token,
}
response = requests.get(
    USER_ENDPOINT, headers=api_header, verify=HTTPS_VERIFY, timeout=API_TIMEOUT
)

default_panel = response.json()["DefaultPanelId"]

response = requests.get(
    PANEL_STATUS_ENDPOINT + default_panel,
    headers=api_header,
    verify=HTTPS_VERIFY,
    timeout=API_TIMEOUT,
)

status_code = response.json()["Status"]
STATUS_TEXT = (
    "disarmed"
    if status_code == 1
    else (
        "fully armed"
        if status_code == 3
        else "partially armed" if status_code == 2 else f"code: {status_code}"
    )
)

print(f"Status: {STATUS_TEXT} since {response.json()['StatusTime']}")
