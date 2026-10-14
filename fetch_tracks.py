#!/usr/bin/env python3
"""
Download recent historical tracks for one flight from the OpenSky REST API
and ADD them to a JSON file for the closest-approach web page.
Existing entries are kept, so running this daily builds up a long history.

Usage:
  export OPENSKY_CLIENT_ID=...
  export OPENSKY_CLIENT_SECRET=...
  python fetch_tracks.py --dep EDDM --arr KJFK --callsign DLH400 --days 28

  --dep / --arr   ICAO airport codes (EDDM = Munich, KJFK = New York JFK)
  --callsign      ICAO-style callsign (airline ICAO code + number, e.g. DLH400, not LH400)
  --days          how many past days to scan (REST tracks only go back ~30 days)

Output: tracks/<DEP>-<ARR>-<CALLSIGN>.json
  [{"date": "2026-10-01", "icao24": "3c6444",
    "track": [{"t": 0.0, "lat": 48.35, "lon": 11.78}, ...]}, ...]
  t = minutes since the first airborne waypoint (approx. wheels-up).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network"
             "/protocol/openid-connect/token")
API = "https://opensky-network.org/api"


class Auth:
    def __init__(self):
        self.cid = os.environ.get("OPENSKY_CLIENT_ID")
        self.secret = os.environ.get("OPENSKY_CLIENT_SECRET")
        if not self.cid or not self.secret:
            sys.exit("Set OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET first.")
        self.token = None
        self.expires = 0

    def headers(self):
        if not self.token or time.time() > self.expires:
            r = requests.post(TOKEN_URL, data={
                "grant_type": "client_credentials",
                "client_id": self.cid,
                "client_secret": self.secret,
            }, timeout=30)
            r.raise_for_status()
            d = r.json()
            self.token = d["access_token"]
            self.expires = time.time() + d.get("expires_in", 1800) - 60
        return {"Authorization": f"Bearer {self.token}"}


auth = Auth()


def get(path, params):
    """GET with token refresh on 401 and a wait on 429. Returns JSON or None on 404."""
    for _ in range(4):
        r = requests.get(f"{API}{path}", params=params,
                         headers=auth.headers(), timeout=60)
        if r.status_code == 404:
            return None
        if r.status_code == 401:
            auth.token = None
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("X-Rate-Limit-Retry-After-Seconds", 60))
            print(f"  rate limited, waiting {wait}s", file=sys.stderr)
            time.sleep(min(wait, 3600))
            continue
        r.raise_for_status()
        print(f"  {path} credits left: {r.headers.get('X-Rate-Limit-Remaining', '?')}")
        return r.json()
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dep", required=True)
    ap.add_argument("--arr", required=True)
    ap.add_argument("--callsign", required=True)
    ap.add_argument("--days", type=int, default=28)
    a = ap.parse_args()

    dep, arr, cs = a.dep.upper(), a.arr.upper(), a.callsign.upper()
    out = f"tracks/{dep}-{arr}-{cs}.json"

    # Keep what we already have, so repeated runs accumulate history.
    existing = []
    if os.path.exists(out):
        with open(out) as fh:
            existing = json.load(fh)
    seen = {(e["date"], e["icao24"]) for e in existing}
    added = 0

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Flights are batch-processed overnight, so start with yesterday.
    for back in range(1, a.days + 1):
        day = today - timedelta(days=back)
        begin = int(day.timestamp())
        end = begin + 86399  # stay inside one calendar day (cheapest credit bucket)
        print(f"{day:%Y-%m-%d}: looking for {cs} {dep}->{arr}")
        flights = get("/flights/departure", {"airport": dep, "begin": begin, "end": end})
        for f in flights or []:
            if (f.get("callsign") or "").strip().upper() != cs:
                continue
            if f.get("estArrivalAirport") != arr:
                continue
            key = (f"{day:%Y-%m-%d}", f["icao24"])
            if key in seen:
                print("  already saved, skipping")
                continue
            mid = (f["firstSeen"] + f["lastSeen"]) // 2
            tr = get("/tracks", {"icao24": f["icao24"], "time": mid})
            if not tr or not tr.get("path"):
                print("  no track available")
                continue
            pts = [p for p in tr["path"] if p[1] is not None and p[2] is not None]
            airborne = [p for p in pts if not p[5]]
            if len(airborne) < 3:
                print("  track too short, skipping")
                continue
            t0 = airborne[0][0]
            existing.append({
                "date": key[0],
                "icao24": f["icao24"],
                "track": [{"t": round((p[0] - t0) / 60, 2), "lat": p[1], "lon": p[2]}
                          for p in airborne],
            })
            seen.add(key)
            added += 1
            print(f"  saved track with {len(airborne)} waypoints")
        time.sleep(1)

    existing.sort(key=lambda e: e["date"])
    os.makedirs("tracks", exist_ok=True)
    with open(out, "w") as fh:
        json.dump(existing, fh)
    print(f"\nAdded {added} new tracks; {len(existing)} total in {out}")


if __name__ == "__main__":
    main()
