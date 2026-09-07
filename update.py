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
    if date.weekday() == 5:    # Saturday -> Friday
        date -= timedelta(days=1)
    elif date.weekday() == 6:  # Sunday -> Friday
        date -= timedelta(days=2)
    return date


def download_and_parse_bhavcopy():
    """
    Downloads the latest NSE FO Bhavcopy ZIP file using modern UDiFF & Legacy URL structures.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "*/*"
    }

    target_date = get_latest_trading_day()

    for _ in range(5):  # Try up to 5 days back
        day_str = target_date.strftime("%d")
        month_str = target_date.strftime("%b").upper()
        year_str = target_date.strftime("%Y")
        ymd_str = target_date.strftime("%Y%m%d")
        date_formatted = target_date.strftime("%d-%b-%Y").upper()

        urls_to_try = [
            f"https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd_str}_F_0000.csv.zip",
            f"https://www.nseindia.com/content/historical/DERIVATIVES/{year_str}/{month_str}/fo{day_str}{month_str}{year_str}bhav.csv.zip"
        ]

        for zip_url in urls_to_try:
            print(f"Fetching Bhavcopy for {date_formatted} from {zip_url}...")
            try:
                res = requests.get(zip_url, headers=headers, timeout=15)
                
                if res.status_code == 200 and res.content[:4] == b'PK\x03\x04':
                    print(f"Successfully retrieved valid ZIP Bhavcopy for {date_formatted}")

                    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                        csv_filename = z.namelist()[0]
                        with z.open(csv_filename) as f:
                            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
                            
                            nifty_rows = []
                            spot_price = 0.0

                            for row in reader:
                                # Clean keys (strip whitespace/bom) and map keys case-insensitively
                                clean_row = {k.strip().upper(): v.strip() if v else "" for k, v in row.items() if k}

                                symbol = clean_row.get("SYMBOL") or clean_row.get("TCKRSYMB") or clean_row.get("FININSTRMID") or ""
                                inst = clean_row.get("INSTRUMENT") or clean_row.get("SGMT") or ""

                                if "NIFTY" in symbol and "NIFTY IT" not in symbol and "NIFTY BANK" not in symbol:
                                    strike_str = clean_row.get("STRIKE_PR") or clean_row.get("STKPRC") or "0"
                                    opt_type = clean_row.get("OPTION_TYP") or clean_row.get("OPTNTP") or ""
                                    close_str = clean_row.get("CLOSE") or clean_row.get("CLSPRC") or "0"
                                    high_str = clean_row.get("HIGH") or clean_row.get("HGHPRC") or "0"
                                    low_str = clean_row.get("LOW") or clean_row.get("LWPRC") or "0"
                                    
                                    # Comprehensive UDiFF and legacy expiry field checks
                                    expiry_str = (
                                        clean_row.get("EXPIRY_DT") or 
                                        clean_row.get("XPRTNDT") or 
                                        clean_row.get("TTLEXPIRDT") or 
                                        clean_row.get("EXPIRY") or ""
                                    )

                                    try:
                                        strike = float(strike_str)
                                        close_p = float(close_str)
                                        high_p = float(high_str)
                                        low_p = float(low_str)
                                    except ValueError:
                                        continue

                                    if opt_type in ["CE", "PE"] and expiry_str:
                                        nifty_rows.append({
                                            "STRIKE_PR": strike,
                                            "OPTION_TYP": opt_type,
                                            "CLOSE": close_p,
                                            "HIGH": high_p,
                                            "LOW": low_p,
                                            "EXPIRY_DT": expiry_str
                                        })
                                    elif ("FUT" in inst or "FUT" in opt_type) and spot_price == 0:
                                        und = clean_row.get("UNDERLYING_VALUE") or clean_row.get("CLSPRC")
                                        try:
                                            if und and float(und) > 0:
                                                spot_price = float(und)
                                        except ValueError:
                                            pass

                            if nifty_rows:
                                print(f"Found {len(nifty_rows)} valid NIFTY option records with expiries.")
                                return {
                                    "date": date_formatted,
                                    "spot_price": spot_price,
                                    "rows": nifty_rows
                                }
                            else:
                                print("Downloaded zip, but columns didn't match required option type/expiry format.")
                else:
                    print(f"Response status: {res.status_code}")

            except Exception as e:
                print(f"URL attempt failed: {e}")

        target_date -= timedelta(days=1)

    return None


def process_bhavcopy_data(bhav_data):
    """Parses raw Bhavcopy rows into structured dashboard data."""
    rows = bhav_data["rows"]
    
    expiries = list(set(r["EXPIRY_DT"] for r in rows if r["EXPIRY_DT"]))
    if not expiries:
        print("No expiry dates found in NIFTY rows.")
        return None

    nearest_expiry = sorted(expiries)[0]
    print(f"Selected nearest expiry date: {nearest_expiry}")
    expiry_rows = [r for r in rows if r["EXPIRY_DT"] == nearest_expiry]

    spot_price = bhav_data["spot_price"]
    if spot_price == 0:
        strikes = [r["STRIKE_PR"] for r in expiry_rows if r["CLOSE"] > 0]
        spot_price = sum(strikes) / len(strikes) if strikes else 24500.0

    hlc_atm_strike = round(spot_price / 50) * 50
    round_100_strike = round(spot_price / 100) * 100

    ce_data = {"close": 0.0, "high": 0.0, "low": 0.0}
    pe_data = {"close": 0.0, "high": 0.0, "low": 0.0}

    for r in expiry_rows:
        if r["STRIKE_PR"] == hlc_atm_strike:
            data_dict = {
                "close": r["CLOSE"],
                "high": r["HIGH"],
                "low": r["LOW"]
            }
            if r["OPTION_TYP"] == "CE":
                ce_data = data_dict
            elif r["OPTION_TYP"] == "PE":
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
    
    if os.path.exists("data.json"):
        with open("data.json", "r") as f:
            existing = json.load(f)
        existing["bhavcopyReady"] = False
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)

    sys.exit(0)


if __name__ == "__main__":
    main()
