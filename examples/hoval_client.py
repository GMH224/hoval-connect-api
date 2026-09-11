"""
Hoval Connect API Client - Python Example (v1.0.0)

Independent audit finding (2026-09, "more" report, finding #12): the
previous version of this example was badly stale — it lacked the custom
User-Agent this API now requires (running it unmodified would get the
exact blanket HTTP 403 documented in docs/audit-v0.24.0.md), implemented
several telemetry endpoints (get_live_values, get_weather,
get_plant_events) this integration deliberately stopped polling in
v1.0.0 (see docs/audit-v1.0.0.md), and checked the legacy `selectable`
field instead of the v3-guaranteed `isSelectable` (see finding #1 in the
"more" report). This version demonstrates the CURRENT, control-only
surface this integration actually uses: authentication, plant/circuit
discovery, reading a circuit's program schedule and weather-impact
settings, and the write operations (temporary-change / reset).

If you just want to read live telemetry (temperatures, energy, etc.),
this project's own recommendation (see README.md) is to get that from a
CAN-bus-based source instead — this API's own live-values/weather/event
endpoints are no longer used by the integration and are not demonstrated
here for that reason, not because they've stopped existing.

Usage:
    from hoval_client import HovalClient
    client = HovalClient("email@example.com", "password")
    for plant in client.get_plants():
        print(plant)
"""

from __future__ import annotations

import time

import requests

# Empirically required (see docs/audit-v0.24.0.md): the API's gateway
# blocks requests.Session()'s own default User-Agent string outright. Any
# non-default, distinctive value works; this one matches the same
# constant used by the shipped integration (const.py's USER_AGENT) so
# this example's traffic is trivially identifiable as this example, not a
# request pretending to be the official app.
USER_AGENT = "HovalConnectHomeAssistant/1.0 (+https://github.com/hoval-connect/hoval-connect-api)"


class HovalClient:
    BASE_URL = "https://azure-iot-prod.hoval.com/core"
    IDP_URL = "https://akwc5scsc.accounts.ondemand.com/oauth2/token"
    CLIENT_ID = "991b54b2-7e67-47ef-81fe-572e21c59899"
    # (connect, read) timeouts in seconds so a stalled call cannot hang forever.
    TIMEOUT = (8, 20)

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._session = requests.Session()
        self._id_token = None
        self._id_token_exp = 0
        self._pat_cache: dict[str, tuple[str, float]] = {}

    def _get_id_token(self) -> str:
        if self._id_token and time.time() < self._id_token_exp - 60:
            return self._id_token

        resp = self._session.post(
            self.IDP_URL,
            data={
                "grant_type": "password",
                "client_id": self.CLIENT_ID,
                "username": self.email,
                "password": self.password,
                "scope": "openid",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        self._id_token = data["id_token"]
        self._id_token_exp = time.time() + data.get("expires_in", 1800)
        return self._id_token

    def _get_plant_access_token(self, plant_id: str) -> str:
        cached = self._pat_cache.get(plant_id)
        if cached and time.time() < cached[1] - 60:
            return cached[0]

        resp = self._session.get(
            f"{self.BASE_URL}/v1/plants/{plant_id}/settings",
            headers={"Authorization": f"Bearer {self._get_id_token()}", "User-Agent": USER_AGENT},
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        self._pat_cache[plant_id] = (token, time.time() + 900)
        return token

    def _headers(self, plant_id: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._get_id_token()}", "User-Agent": USER_AGENT}
        if plant_id:
            h["X-Plant-Access-Token"] = self._get_plant_access_token(plant_id)
        return h

    def get_plants(self) -> list:
        """List plants. Paginated (size/page) since Hoval's May 2026 change."""
        resp = self._session.get(
            f"{self.BASE_URL}/api/my-plants",
            params={"size": "12", "page": "0"},
            headers=self._headers(),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("content", [])

    def get_circuits(self, plant_id: str) -> list:
        """List a plant's circuits (the v3 endpoint — v1 was removed 2026-04-21)."""
        resp = self._session.get(
            f"{self.BASE_URL}/v3/plants/{plant_id}/circuits",
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("content", [])

    @staticmethod
    def is_circuit_selectable(circuit: dict) -> bool:
        """Whether a circuit is selectable, per the v3 contract.

        `isSelectable` is REQUIRED in docs/openapi-v3.json's CircuitV3DTO;
        the older `selectable` field is only optional. Prefer the
        guaranteed field — see finding #1 in the "more" report for why
        checking `selectable` alone can silently miss a valid circuit.
        """
        is_selectable = circuit.get("isSelectable")
        if is_selectable is None:
            is_selectable = circuit.get("selectable", False)
        return bool(is_selectable)

    def get_programs(self, plant_id: str, circuit_path: str) -> dict:
        """Get a circuit's time-program schedule (week1/week2/etc.)."""
        resp = self._session.get(
            f"{self.BASE_URL}/v3/plants/{plant_id}/circuits/{circuit_path}/programs",
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def get_circuit_settings(self, plant_id: str, circuit_path: str) -> dict:
        """Get a circuit's settings, including weatherImpact (HK circuits)."""
        resp = self._session.get(
            f"{self.BASE_URL}/v3/plants/{plant_id}/circuits/{circuit_path}/settings",
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def set_temporary_change(
        self, plant_id: str, circuit_path: str, value: float, duration: str = "fourHours"
    ) -> None:
        """Set a temporary value override. duration: "fourHours" or "midnight"."""
        resp = self._session.post(
            f"{self.BASE_URL}/v3/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
            json={"value": value, "duration": duration},
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()

    def reset_temporary_change(self, plant_id: str, circuit_path: str) -> None:
        """Cancel an active temporary override and resume the underlying program."""
        resp = self._session.delete(
            f"{self.BASE_URL}/v3/plants/{plant_id}/circuits/{circuit_path}/temporary-change",
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()

    def set_program(self, plant_id: str, circuit_path: str, program: str) -> None:
        """Activate a program: constant, ecoMode, standby, week1, week2, manual."""
        resp = self._session.post(
            f"{self.BASE_URL}/v3/plants/{plant_id}/circuits/{circuit_path}/programs/{program}",
            headers=self._headers(plant_id),
            timeout=self.TIMEOUT,
        )
        resp.raise_for_status()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print(f"Usage: python {sys.argv[0]} <email> <password>")
        sys.exit(1)

    client = HovalClient(sys.argv[1], sys.argv[2])

    plants = client.get_plants()
    print(f"Plants: {plants}")

    for plant in plants:
        pid = plant["plantExternalId"]
        print(
            f"\n--- Plant {pid} ({plant.get('description')}) — online={plant.get('isOnline')} ---"
        )

        circuits = client.get_circuits(pid)
        for circuit in circuits:
            if not client.is_circuit_selectable(circuit):
                continue
            path = circuit["path"]
            ctype = circuit["type"]
            print(f"\nCircuit: {circuit.get('name', ctype)} ({path}, type={ctype})")
            print(f"  activeProgram: {circuit.get('activeProgram')}")
            print(f"  targetValue: {circuit.get('targetValue')}")

            programs = client.get_programs(pid, path)
            print(
                f"  programs: week1={programs.get('week1', {}).get('name')} "
                f"week2={programs.get('week2', {}).get('name')}"
            )

            if ctype == "HK":
                settings = client.get_circuit_settings(pid, path)
                print(f"  weatherImpact: {settings.get('weatherImpact')}")
