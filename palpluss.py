import os
import ipaddress
import re
from urllib.parse import quote, urlparse

import requests


class PalPlussError(Exception):
    pass


class PalPlussClient:
    BASE_URL = "https://api.palpluss.com/v1"

    def __init__(self):
        self.api_key = os.getenv("PALPLUSS_API_KEY", "").strip()
        if not self.api_key:
            raise PalPlussError("PALPLUSS_API_KEY is not configured.")

        self.session = requests.Session()
        self.session.auth = (self.api_key, "")
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _request(self, method, endpoint, **kwargs):
        try:
            response = self.session.request(
                method,
                self.BASE_URL + endpoint,
                timeout=30,
                **kwargs
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("success") is False:
                raise PalPlussError("PalPluss rejected the request or returned an unexpected response.")
            return payload
        except requests.RequestException as exc:
            raise PalPlussError(
                "PalPluss request failed. Check connectivity and provider configuration."
            ) from exc
        except ValueError as exc:
            raise PalPlussError("PalPluss returned an invalid response.") from exc

    def service_balance(self):
        return self._request("GET", "/wallets/service/balance")

    def initiate_stk(self, amount, phone, reference, callback_url):
        amount = int(amount)
        if amount < 1:
            raise PalPlussError("Payment amount must be at least KSh 1.")

        phone = str(phone).replace(" ", "").replace("-", "")
        if phone.startswith("+"):
            phone = phone[1:]
        if phone.startswith("0"):
            phone = "254" + phone[1:]
        if not re.fullmatch(r"254(?:7|1)\d{8}", phone):
            raise PalPlussError("Enter a valid Kenyan mobile number.")

        reference = str(reference).strip()
        if not re.fullmatch(r"[A-Za-z0-9]{1,12}", reference):
            raise PalPlussError("Account reference must be 1–12 letters or digits.")

        parsed = urlparse(str(callback_url))
        if parsed.scheme != "https" or not parsed.hostname:
            raise PalPlussError("Callback URL must use public HTTPS.")
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith(".localhost"):
            raise PalPlussError("Callback URL cannot use localhost.")
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                raise PalPlussError("Callback URL must use a public address.")
        except ValueError:
            pass

        payload = {
            "amount": amount,
            "phone": phone,
            "accountReference": reference,
            "transactionDesc": "TIKETI360",
            "callbackUrl": callback_url,
        }
        channel_id = os.getenv("PALPLUSS_CHANNEL_ID", "").strip()
        if channel_id:
            payload["channelId"] = channel_id
        return self._request("POST", "/payments/stk", json=payload)

    def get_transaction(self, transaction_id):
        safe_id = quote(str(transaction_id), safe="")
        return self._request("GET", f"/transactions/{safe_id}")
