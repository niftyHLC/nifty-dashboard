import csv
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timedelta
import requests


def get_latest_trading_day():
    """Find the most recent weekday to attempt Bhavcopy download."""
    date = datetime.now()
    # If today is weekend, rollback to Friday
    if date.weekday() == 5:  # Saturday
        date -= timedelta(days=1)
    elif date.weekday() == 6:  # Sunday
        date -= timedelta(days=2)
    return date


def download_and_parse_bhavcopy():
    """
    Downloads the latest NSE FO Bhavcopy ZIP file and parses NIFTY options.
    Tries current date first, then steps backward day-by-day until a valid file is found.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    target_date = get_latest_trading_day()

    for _ in range(5):  # Try up to 5 days back (handles trading holidays)
        day_str = target_date.strftime("%d")
        month_str = target_date.strftime("%b").upper()
        year_str = target_date.strftime("%Y")
        date_formatted = target_date.strftime("%d-%b-%Y").upper()

        # NSE FO Bhavcopy URL format (udr_fo_bhav.csv / foDDMMMYYYYbhav.csv)
        # Using NSE Archives zip endpoint:
        zip_url = f"https://archives.nseindia.com/content/historical/DERIVATIVES/{year_str}/{month_str}/fo{day_str}{month_str}{year_str}bhav.csv.zip"

        print(f"Attempting to fetch Bhavcopy for {date_formatted} from {zip_url}...")

        try:
            res = requests.get(zip_url, headers=headers, timeout=15)
            if res.status_code == 200:
                print(f"Successfully downloaded Bhavcopy for {date_formatted}")

                # Extract CSV from ZIP in memory
                with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                    csv_filename = z.namelist()[0]
                    with z.open(csv_filename) as f:
                        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
                        
                        nifty_rows = []
                        spot_price = 0.0

                        for row in reader:
                            # Filter only NIFTY Index Options
                            if row.get("INSTRUMENT") in ["OPTIDX", "OPTSTK"] and row.get("SYMBOL") == "NIFTY":
                                nifty_rows.append(row)
                            elif row.get("INSTRUMENT") == "FUTIDX" and row.get("SYMBOL") == "NIFTY":
                                # Use underlying spot or near-month future price as reference
                                underlying = row.get("UNDERLYING_VALUE") or row.get("CLOSE")
                                if underlying and float(underlying) > 0:
                                    spot_price = float(underlying)

                        if nifty_rows:
                            return {
                                "date": date_formatted,
                                "spot_price": spot_price,
                                "rows": nifty_rows
                            }
            else:
                print(f"Bhavcopy not available for {date_formatted} (Status: {res.status_code})")
        except Exception as e:
            print(f"Failed fetching Bhavcopy for {date_formatted}: {e}")

        # Step back 1 day
        target_date -= timedelta(days=1)

    return None


def process_bhavcopy_data(bhav_data):
    """Parses raw Bhavcopy rows into the structure required by index.html."""
    rows = bhav_data["rows"]
    
    # 1. Get nearest Expiry Date
    expiries = sorted(list(set(r["EXPIRY_DT"] for r in rows)))
    if not expiries:
        return None
    nearest_expiry = expiries[0]

    # Filter for nearest expiry only
    expiry_rows = [r for r in rows if r["EXPIRY_DT"] == nearest_expiry]

    # Calculate Spot Price if missing
    spot_price = bhav_data["spot_price"]
    if spot_price == 0:
        # Estimate spot from median strike in option chain
        strikes = [float(r["STRIKE_PR"]) for r in expiry_rows if float(r.get("CLOSE", 0)) > 0]
        spot_price = sum(strikes) / len(strikes) if strikes else 24500.0

    hlc_atm_strike = round(spot_price / 50) * 50
    round_100_strike = round(spot_price / 100) * 100

    ce_data = {"close": 0.0, "high": 0.0, "low": 0.0}
    pe_data = {"close": 0.0, "high": 0.0, "low": 0.0}

    # Extract ATM Strike Data
    for r in expiry_rows:
        strike = float(r["STRIKE_PR"])
        option_type = r["OPTION_TYP"]

        if strike == hlc_atm_strike:
            data_dict = {
                "close": float(r.get("CLOSE", 0) or r.get("LAST", 0)),
                "high": float(r.get("HIGH", 0)),
                "low": float(r.get("LOW", 0))
            }
            if option_type == "CE":
                ce_data = data_dict
            elif option_type == "PE":
                pe_data = data_dict

    ce_hc = ce_data["high"] - ce_data["close"]
    ce_cl = ce_data["close"] - ce_data["low"]
    pe_hc = pe_data["high"] - pe_data["close"]
    pe_cl = pe_data["close"] - pe_data["low"]

    ce_tag = "BUYERS" if ce_cl > ce_hc else ("SELLERS" if ce_hc > ce_cl else "NEUTRAL")
    ce_class = "tag-buyers" if ce_tag == "BUYERS" else ("tag-sellers" if ce_tag == "SELLERS" else "tag-neutral")

    pe_tag = "BUYERS" if pe_cl > pe_hc else ("SELLERS" if pe_hc > pe_cl else "NEUTRAL")
    pe_class = "tag-buyers" if pe_tag == "BUYERS" else ("tag-sellers" if pe_tag == "SELLERS" else "tag-neutral")

    straddle_sum = ce_data["close"] + pe_data["close"]

    return {
        "bhavcopyReady": True,
        "spotPrice": round(spot_price, 2),
        "currentDate": bhav_data["date"],
        "expiryDate": nearest_expiry,
        "hlcAtmStrike": str(hlc_atm_strike),
        "ce": ce_data,
        "pe": pe_data,
        "ceTag": ce_tag,
        "ceClass": ce_class,
        "peTag": pe_tag,
        "peClass": pe_class,
        "bannerTotal": round(straddle_sum, 2),
        "minSupply": round(spot_price - straddle_sum, 2),
        "minDemand": round(spot_price + straddle_sum, 2),
        "maxSupply": round(spot_price - (straddle_sum * 1.5), 2),
        "maxDemand": round(spot_price + (straddle_sum * 1.5), 2),
        "weeklyZones": {
            "line1": round(spot_price + (straddle_sum * 0.8), 2),
            "line2": round(spot_price - (straddle_sum * 0.8), 2)
        },
        "monthlyZones": {
            "line1": round(spot_price + (straddle_sum * 2.0), 2),
            "line2": round(spot_price - (straddle_sum * 2.0), 2)
        },
        "sniper1": {
            "strike": str(round_100_strike),
            "ce": ce_data["close"],
            "pe": pe_data["close"],
            "otmCeStrike": str(round_100_strike + 200),
            "otmPeStrike": str(round_100_strike - 200),
            "otmCe": round(ce_data["close"] * 0.4, 2),
            "otmPe": round(pe_data["close"] * 0.4, 2),
            "value": round(straddle_sum, 2)
        },
        "sniper2": {
            "strike": str(hlc_atm_strike),
            "ce": ce_data["close"],
            "pe": pe_data["close"],
            "otmCeStrike": str(hlc_atm_strike + 200),
            "otmPeStrike": str(hlc_atm_strike - 200),
            "otmCe": round(ce_data["close"] * 0.35, 2),
            "otmPe": round(pe_data["close"] * 0.35, 2),
            "value": round(straddle_sum * 0.95, 2)
        }
    }


def main():
    print("Starting Bhavcopy Update Process...")

    bhav_data = download_and_parse_bhavcopy()

    if bhav_data:
        dashboard_json = process_bhavcopy_data(bhav_data)
        if dashboard_json:
            with open("data.json", "w", encoding="utf-8") as f:
                json.dump(dashboard_json, f, indent=2)
            print("Successfully updated data.json via official NSE Bhavcopy!")
            sys.exit(0)

    print("Warning: Could not process Bhavcopy. Keeping existing data.json.")
    
    # Graceful fallback flag if Bhavcopy is pending
    if os.path.exists("data.json"):
        with open("data.json", "r") as f:
            existing = json.load(f)
        existing["bhavcopyReady"] = False
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

    sys.exit(0)


if __name__ == "__main__":
    main()
