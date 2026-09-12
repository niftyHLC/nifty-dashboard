import csv
import datetime
import io
import json
import math
import os
import subprocess
import zipfile
import requests
import yfinance as yf

# Indian Standard Time (IST) offset
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def push_to_github():
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        
        # Force add both data.json and bhavcopy.csv to ensure they are tracked
        subprocess.run(["git", "add", "-f", "data.json"], check=False)
        if os.path.exists("bhavcopy.csv"):
            subprocess.run(["git", "add", "-f", "bhavcopy.csv"], check=False)
        
        diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        
        if diff_check.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Auto-update dashboard and bhavcopy file [skip ci]"], check=True)
            subprocess.run(["git", "push", "origin", "main"], check=True)
            print("Changes pushed to GitHub successfully.")
        else:
            print("No changes detected in repository. Skipping commit.")
    except Exception as e:
        print(f"Test/Git push failed: {e}")


def fetch_live_spot_from_yahoo():
    """Fetches real-time Nifty 50 spot price from Yahoo Finance."""
    try:
        ticker = yf.Ticker("^NSEI")
        todays_data = ticker.history(period="1d")
        if not todays_data.empty:
            spot = float(todays_data["Close"].iloc[-1])
            return spot
    except Exception as e:
        print(f"Failed to fetch spot from Yahoo Finance: {e}")
    return 0.0


def download_today_bhavcopy():
    """Downloads official Bhavcopy directly from NSE archives."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/"
    }
    now_ist = datetime.datetime.now(IST)
    yyyy = now_ist.strftime("%Y")
    mm = now_ist.strftime("%m")
    dd = now_ist.strftime("%d")
    
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyy}{mm}{dd}_F_0000.csv.zip"
    try:
        response = requests.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200 and len(response.content) > 1000:
            if os.path.exists("bhavcopy.csv"):
                try: 
                    os.remove("bhavcopy.csv")
                except Exception: 
                    pass

            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                csv_filename = z.namelist()[0]
                with z.open(csv_filename) as csv_file:
                    content = csv_file.read().decode('utf-8', errors='ignore')
                    lines = content.splitlines()
                    
                    nifty_lines = []
                    if lines:
                        nifty_lines.append(lines[0])
                        for line in lines[1:]:
                            if "NIFTY" in line.upper():
                                nifty_lines.append(line)
                    
                    with open("bhavcopy.csv", "w", encoding="utf-8") as f:
                        f.write("\n".join(nifty_lines))

            print(f"Successfully downloaded TODAY'S Bhavcopy for {now_ist.strftime('%Y-%m-%d')}")
            return True
        else:
            print("---------------------------------------------------")
            print(f"NOTICE: Today's Bhavcopy is NOT READY yet from NSE (Status Code: {response.status_code}).")
            print("Continuing script execution using available local/fallback data.")
            print("---------------------------------------------------")
    except Exception as e:
        print("---------------------------------------------------")
        print(f"NOTICE: Today's Bhavcopy is NOT READY yet from NSE: {e}")
        print("Continuing script execution using available local/fallback data.")
        print("---------------------------------------------------")
        
    return False


def load_bhavcopy_dict(target_expiry_str):
    bhav_map = {}
    if not os.path.exists("bhavcopy.csv"):
        return bhav_map

    possible_expiries = set()
    clean_target = target_expiry_str.strip().upper()
    possible_expiries.add(clean_target)
    
    date_formats = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y", "%d%b%y")
    
    dt_obj = None
    for fmt in date_formats:
        try:
            dt_obj = datetime.datetime.strptime(clean_target, fmt)
            break
        except ValueError:
            continue

    if dt_obj:
        for fmt in date_formats:
            possible_expiries.add(dt_obj.strftime(fmt).upper())

    try:
        with open("bhavcopy.csv", mode="r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cleaned_row = {k.strip().upper(): (v.strip() if v else "") for k, v in row.items() if k}
                symbol = cleaned_row.get("TCKRSYMB") or cleaned_row.get("SYMBOL") or cleaned_row.get("FININSTRNM") or ""
                if "NIFTY" not in symbol.upper():
                    continue

                strike_raw = (cleaned_row.get("STRKPRIC") or cleaned_row.get("STRIKEPRIC") or 
                           cleaned_row.get("STRIKE_PR") or cleaned_row.get("STRIKE") or "0")
                try:
                    row_strike = int(round(float(strike_raw)))
                except ValueError:
                    continue

                opt_type_raw = (cleaned_row.get("OPTNTP") or cleaned_row.get("OPTION_TYP") or 
                                cleaned_row.get("OPTIONTYPE") or "")
                opt_type = "CE" if "CE" in opt_type_raw.upper() else "PE" if "PE" in opt_type_raw.upper() else ""

                if not opt_type:
                    continue

                expiry_raw = (cleaned_row.get("XPRYDT") or cleaned_row.get("EXPIRY_DT") or 
                              cleaned_row.get("EXPIRY") or "").strip().upper()
                
                if any(expiry_raw == exp for exp in possible_expiries):
                    open_p = float(cleaned_row.get("OPENPRIC") or cleaned_row.get("OPEN") or 0.0)
                    high = float(cleaned_row.get("HGHPRIC") or cleaned_row.get("HIGH") or 0.0)
                    low = float(cleaned_row.get("LWPRIC") or cleaned_row.get("LOW") or 0.0)
                    close = float(cleaned_row.get("CLSPRIC") or cleaned_row.get("CLOSE") or cleaned_row.get("SETTLE_PR") or 0.0)
                    chg_oi = float(cleaned_row.get("CHGINOI") or cleaned_row.get("CHG_IN_OI") or 0.0)

                    if high > 0 or low > 0 or close > 0:
                        bhav_map[(row_strike, opt_type)] = {
                            "open": open_p, "high": high, "low": low, "close": close, "chg_oi": chg_oi
                        }
    except Exception as e:
        print(f"Error reading bhavcopy into dict: {e}")

    return bhav_map


def get_market_sentiment_tag(data_dict):
    if not data_dict:
        return "NEUTRAL", "tag-neutral"
    
    close = data_dict.get("close", 0.0)
    open_p = data_dict.get("open", 0.0)
    high = data_dict.get("high", 0.0)
    low = data_dict.get("low", 0.0)
    chg_oi = data_dict.get("chg_oi", 0.0)

    price_up = close >= open_p
    if chg_oi > 0:
        if price_up or (high - low > 0 and (close - low) / (high - low) > 0.5):
            return "BUYERS", "tag-buyers"
        else:
            return "SELLERS", "tag-sellers"
            
    return "NEUTRAL", "tag-neutral"


def calculate_zone_row_one(wl, wh, bhav_map):
    def get_p(s, t):
        return bhav_map.get((s, t), {}).get("close", 0.0)

    ce1, pe1 = get_p(wl, "CE"), get_p(wl, "PE")
    ce2, pe2 = get_p(wh, "CE"), get_p(wh, "PE")

    return {
        "line1": round(wh + ce2, 2),  
        "line2": round(wl - pe1, 2)    
    }


def get_display_date(now_ist):
    """Calculates next trading day date if run on a weekend (Sat/Sun)."""
    wday = now_ist.weekday()
    if wday == 5:  # Saturday -> shifts to Monday
        target = now_ist + datetime.timedelta(days=2)
    elif wday == 6:  # Sunday -> shifts to Monday
        target = now_ist + datetime.timedelta(days=1)
    else:
        target = now_ist
    return target.strftime("%d %b %Y").upper()


def process_and_save_data(spot):
    if spot <= 0:
        print("Invalid spot price received.")
        return

    now_ist = datetime.datetime.now(IST)
    today_str = get_display_date(now_ist)

    bhavcopy_is_ready = download_today_bhavcopy()

    all_expiries_dt = set()
    if os.path.exists("bhavcopy.csv"):
        with open("bhavcopy.csv", mode="r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cleaned_row = {k.strip().upper(): (v.strip() if v else "") for k, v in row.items() if k}
                exp = cleaned_row.get("XPRYDT") or cleaned_row.get("EXPIRY_DT") or cleaned_row.get("EXPIRY")
                if exp:
                    exp_clean = exp.strip().upper()
                    dt_parsed = None
                    date_formats = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y", "%d%b%y")
                    for fmt in date_formats:
                        try:
                            dt_parsed = datetime.datetime.strptime(exp_clean, fmt)
                            break
                        except ValueError:
                            continue
                    if dt_parsed:
                        all_expiries_dt.add((dt_parsed, exp_clean))

    # Check if market has closed for today (Past 3:30 PM IST / 15:30)
    market_closed_today = (now_ist.hour > 15) or (now_ist.hour == 15 and now_ist.minute >= 30)
    
    # Make today_dt offset-naive to match CSV parsed dates
    today_dt = now_ist.replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)

    # Filter expiries: exclude today's expiry if market is already closed
    valid_tuples = []
    for item in all_expiries_dt:
        exp_date = item[0]
        if exp_date > today_dt:
            valid_tuples.append(item)
        elif exp_date == today_dt and not market_closed_today:
            valid_tuples.append(item)

    if not valid_tuples:
        valid_tuples = list(all_expiries_dt)

    sorted_expiries_tuples = sorted(valid_tuples, key=lambda x: x[0])
    sorted_expiries = [item[1] for item in sorted_expiries_tuples]
    
    if sorted_expiries:
        w_exp = sorted_expiries[0]
        
        # --- TUESDAY 3:30 PM ROLLOVER RULE ---
        # If today is Tuesday and it's past 3:30 PM, roll over to the next expiry index if available
        if now_ist.weekday() == 1 and market_closed_today:
            if len(sorted_expiries) > 1:
                w_exp = sorted_expiries[1]
                print("⏰ Tuesday past 3:30 PM detected: Rolled over active expiry to next week.")
    else:
        w_exp = now_ist.strftime("%d-%m-%y").upper()

    m_exp = sorted_expiries[-1] if sorted_expiries else w_exp

    w_bhav = load_bhavcopy_dict(w_exp)
    m_bhav = load_bhavcopy_dict(m_exp)

    min_diff = float('inf')
    hlc_atm_strike = int(round(spot / 50.0) * 50)

    for (strike, opt_type), d_val in w_bhav.items():
        if abs(strike - spot) <= 500:
            ce_close = w_bhav.get((strike, "CE"), {}).get("close", 0.0)
            pe_close = w_bhav.get((strike, "PE"), {}).get("close", 0.0)
            if ce_close > 0 and pe_close > 0:
                diff = abs(ce_close - pe_close)
                if diff < min_diff:
                    min_diff = diff
                    hlc_atm_strike = strike

    sniper1_atm_strike = int(round(spot / 100.0) * 100)
    sniper2_atm_strike = hlc_atm_strike

    target_s1_ce_strike = sniper1_atm_strike + 100
    target_s1_pe_strike = sniper1_atm_strike - 100
    target_s2_ce_strike = sniper2_atm_strike + 100
    target_s2_pe_strike = sniper2_atm_strike - 100

    ce_dict = w_bhav.get((int(hlc_atm_strike), "CE"), {"high": 0.0, "low": 0.0, "close": 0.0, "open": 0.0, "chg_oi": 0.0})
    pe_dict = w_bhav.get((int(hlc_atm_strike), "PE"), {"high": 0.0, "low": 0.0, "close": 0.0, "open": 0.0, "chg_oi": 0.0})

    ce_high, ce_low, ce_close = ce_dict.get("high", 0.0), ce_dict.get("low", 0.0), ce_dict.get("close", 0.0)
    pe_high, pe_low, pe_close = pe_dict.get("high", 0.0), pe_dict.get("low", 0.0), pe_dict.get("close", 0.0)

    s1_atm_ce_val = w_bhav.get((sniper1_atm_strike, "CE"), {}).get("close", 0.0)
    s1_atm_pe_val = w_bhav.get((sniper1_atm_strike, "PE"), {}).get("close", 0.0)
    s1_ce_val = w_bhav.get((target_s1_ce_strike, "CE"), {}).get("close", 0.0)
    s1_pe_val = w_bhav.get((target_s1_pe_strike, "PE"), {}).get("close", 0.0)

    s2_atm_ce_val = w_bhav.get((sniper2_atm_strike, "CE"), {}).get("close", 0.0)
    s2_atm_pe_val = w_bhav.get((sniper2_atm_strike, "PE"), {}).get("close", 0.0)
    s2_ce_val = w_bhav.get((target_s2_ce_strike, "CE"), {}).get("close", 0.0)
    s2_pe_val = w_bhav.get((target_s2_pe_strike, "PE"), {}).get("close", 0.0)

    ce_tag, ce_class = get_market_sentiment_tag(ce_dict)
    pe_tag, pe_class = get_market_sentiment_tag(pe_dict)

    sniper1_val = round((s1_ce_val + s1_pe_val) / 2.0, 2)
    sniper2_val = round((s2_ce_val + s2_pe_val) / 2.0, 2)

    min_supply_val = round(hlc_atm_strike + ce_close, 2)
    min_demand_val = round(hlc_atm_strike - pe_close, 2)
    max_supply_val = round(hlc_atm_strike + (ce_close + pe_close), 2)
    max_demand_val = round(hlc_atm_strike - (ce_close + pe_close), 2)

    wl = int(math.floor(spot / 100.0) * 100)
    wh = int(math.ceil(spot / 100.0) * 100)

    weekly_zones = calculate_zone_row_one(wl, wh, w_bhav)
    monthly_zones = calculate_zone_row_one(wl, wh, m_bhav)

    payload = {
        "dataStatus": "SUCCESS",
        "bhavcopyReady": bhavcopy_is_ready,
        "currentDate": today_str,
        "expiryDate": w_exp,
        "spotPrice": spot,
        "hlcAtmStrike": hlc_atm_strike,
        "ce": {"high": round(ce_high, 2), "close": round(ce_close, 2), "low": round(ce_low, 2)},
        "pe": {"high": round(pe_high, 2), "close": round(pe_close, 2), "low": round(pe_low, 2)},
        "ceTag": ce_tag, "ceClass": ce_class,
        "peTag": pe_tag, "peClass": pe_class,
        "bannerTotal": round(ce_close + pe_close, 2),
        "minSupply": min_supply_val,
        "minDemand": min_demand_val,
        "maxSupply": max_supply_val,
        "maxDemand": max_demand_val,
        "weeklyZones": weekly_zones,
        "monthlyZones": monthly_zones,
        "spotHigh": spot,
        "spotLow": spot,
        "sniper1": {
            "strike": sniper1_atm_strike, "ce": round(s1_atm_ce_val, 2), "pe": round(s1_atm_pe_val, 2),
            "otmCeStrike": target_s1_ce_strike, "otmPeStrike": target_s1_pe_strike,
            "otmCe": round(s1_ce_val, 2), "otmPe": round(s1_pe_val, 2), "value": sniper1_val
        },
        "sniper2": {
            "strike": sniper2_atm_strike, "ce": round(s2_atm_ce_val, 2), "pe": round(s2_atm_pe_val, 2),
            "otmCeStrike": target_s2_ce_strike, "otmPeStrike": target_s2_pe_strike,
            "otmCe": round(s2_ce_val, 2), "otmPe": round(s2_pe_val, 2), "value": sniper2_val
        }
    }

    with open("data.json", "w") as f:
        json.dump(payload, f, indent=4)
        
    print(f"Data saved successfully. Spot: {spot}, Bhavcopy status: {bhavcopy_is_ready}")
    push_to_github()


if __name__ == "__main__":
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r") as f:
                existing_data = json.load(f)
                now_ist = datetime.datetime.now(IST)
                today_str = get_display_date(now_ist)
                
                if existing_data.get("currentDate") == today_str and existing_data.get("bhavcopyReady") is True:
                    print("✅ Today's Bhavcopy is already downloaded and processed. Stopping execution for today.")
                    exit(0)
        except Exception:
            pass

    spot = fetch_live_spot_from_yahoo()
    if spot > 0:
        print(f"Retrieved Spot Price from Yahoo Finance: {spot}")
        process_and_save_data(spot)
    else:
        print("Failed to retrieve spot price.")
