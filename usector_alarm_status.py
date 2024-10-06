"""
Micropython class to check if sector alarm is armed or not.
"""
import time
import requests  # mip module

USER_NAME = "user@example.com"
PASSWORD = "YOUR_PASS"
USER_HOME = "https://minasidor.sectoralarm.se"

WEB_APP_CLIENT_ID = "keCW6wogC4jMscX1CdZ1WAKmLhcGdlHo"
COMPANY_HOME = "https://minside.sectoralarm.no"
API_TIMEOUT = 10.0

OAUTH_ENDPOINT = "https://login.sectoralarm.com/oauth/token"
USER_ENDPOINT = "https://mypagesapi.sectoralarm.net/api/Login/GetUser"
PANEL_STATUS_ENDPOINT = (
    "https://mypagesapi.sectoralarm.net/api/Panel/GetPanelStatus?panelId="
)
FULLY_ARMED_STATUS_CODE = 3  # 2 == partial, 1 == not armed


class AlarmStatusProvider:
    def __init__(self):
        self.login_request_body = {
            "password": PASSWORD,
            "username": USER_NAME,
            "client_id": WEB_APP_CLIENT_ID,
            "grant_type": "password",
            "redirect_uri": USER_HOME,
            "audience": COMPANY_HOME,
        }
        self.default_panel = None
        self.api_access_token = None
        self.api_access_token_expiration = None
        try:
            self.access_token_housekeeping()
        except Exception as EXCEPT:
            print(f"Login failed: {EXCEPT}")
        else:
            if self.api_access_token is not None:
                self.fetch_default_panel()

    def fetch_default_panel(self):
        try:
            response = requests.get(
                USER_ENDPOINT, headers=self.api_header, timeout=API_TIMEOUT
            )
            if response.status_code == 200:
                self.default_panel = response.json()["DefaultPanelId"]
            else:
                print(f"Failed to get default panel {response.text}")
        except Exception as EXCEPT:
            print(f"Failed to get alarm panel: {EXCEPT}")

    def access_token_housekeeping(self):
        if self.api_access_token_expiration is not None:
            print(
                f"{time.time()}: current token valid til {self.api_access_token_expiration}"
            )
            if self.api_access_token_expiration > (time.time() + 60):
                return  # Valid another 60 sec
        try:
            # print(f"Token refresh with: {self.login_request_body}")
            response = requests.post(
                OAUTH_ENDPOINT, json=self.login_request_body, timeout=API_TIMEOUT
            )
            if response.status_code != 200:
                print(f"Error signing in: {response.text}")
            else:
                resp_json = response.json()
                self.api_access_token = resp_json["access_token"]
                self.api_access_token_expiration = time.time() + resp_json["expires_in"]
                self.api_header = {
                    "accept": "application/json, text/plain, */*",
                    "authorization": self.api_access_token,
                }
        except OSError as req_err:
            if req_err.args[0] == 110:  # ETIMEDOUT
                self.access_token_housekeeping()  # retry

    def is_fully_armed(self):
        if self.default_panel is None:
            return False
        self.access_token_housekeeping()
        try:
            response = requests.get(
                PANEL_STATUS_ENDPOINT + self.default_panel,
                headers=self.api_header,
                timeout=API_TIMEOUT,
            )
        except OSError as req_err:
            if (
                req_err.args[0] == 110 or req_err.args[0] == 115
            ):  # ETIMEDOUT / EINPROGRESS
                print("Ignoring alarm read timeout")
                return False
            raise req_err

        return (
            response.status_code == 200
            and response.json()["Status"] == FULLY_ARMED_STATUS_CODE
        )
