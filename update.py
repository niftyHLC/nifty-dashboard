import csv
import datetime
import io
import json
import math
import os
import subprocess
import zipfile
import requests

# Indian Standard Time (IST) offset
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# Shared headers for direct NSE web requests
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/"
}


def get_next_trading_day(d):
    """Calculates the next trading day, skipping weekends (Saturday and Sunday)."""
    next_day = d + datetime.timedelta(days=1)
    while next_day.weekday() >= 5:  # 5 is Saturday, 6 is Sunday
        next_day += datetime.timedelta(days=1)
    return next_day


def push_to_github():
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        
        subprocess.run(["git", "add", "-f", "data.json"], check=False)
        if os.path.exists("bhavcopy.csv"):
            subprocess.run(["git", "add", "-f", "bhavcopy.csv"], check=False)
        
        diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        
        if diff_check.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Auto-update dashboard and bhavcopy status [skip ci]"], check=True)
            subprocess.run(["git", "push", "origin", "main"], check=True)
            print("Changes pushed to GitHub successfully.")
        else:
            print("No changes detected in repository. Skipping commit.")
    except Exception as e:
        print(f"Git push failed: {e}")


def get_nse_session():
    """Creates a requests session initialized with cookies from NSE home page."""
    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=10)
    except Exception as e:
        print(f"Warning initializing NSE session: {e}")
    return session


def fetch_nse_option_chain():
    """Fetches live option chain data and spot price directly from NSE."""
    url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
    try:
        session = get_nse_session()
        response = session.get(url, headers=NSE_HEADERS, timeout=15)
        if response.status_code == 200:
            data = response.json()
            spot = float(data.get("records", {}).get("underlyingValue", 0.0))
            return data, spot
    except Exception as e:
        print(f"Failed to fetch option chain from NSE: {e}")
    return None, 0.0


def get_expiries_from_chain(chain_raw_data):
    """Extracts weekly and monthly expiries from direct NSE option chain data."""
    now_ist = datetime.datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    
    records = chain_raw_data.get("records", {})
    expiry_dates = records.get("expiryDates", [])
    
    if not expiry_dates:
        return today_str, today_str
        
    formatted_expiries = []
    for exp in expiry_dates:
        try:
            dt = datetime.datetime.strptime(exp, "%d-%b-%Y")
            formatted_expiries.append(dt.strftime("%Y-%m-%d"))
        except Exception:
            continue
            
    formatted_expiries = sorted(list(set(formatted_expiries)))
    future_exp = [e for e in formatted_expiries if e >= today_str]
    w_exp = future_exp[0] if future_exp else (formatted_expiries[-1] if formatted_expiries else today_str)
    
    m_exp = [e for e in formatted_expiries if e.startswith(today_str[:7])]
    m_exp = m_exp[-1] if m_exp else w_exp
    
    return w_exp, m_exp


def download_today_bhavcopy():
    """Downloads daily Bhavcopy directly from public NSE archives."""
    now_ist = datetime.datetime.now(IST)
    yyyy = now_ist.strftime("%Y")
    mm = now_ist.strftime("%m")
    dd = now_ist.strftime("%d")
    
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyy}{mm}{dd}_F_0000.csv.zip"
    try:
        session = get_nse_session()
        response = session.get(url, headers=NSE_HEADERS, timeout=30)
        
        if response.status_code == 200 and len(response.content) > 1000:
            if os.path.exists("bhavcopy.csv"):
                try: os.remove("bhavcopy.csv")
                except Exception: pass

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
    except Exception as e:
        print(f"Today's Bhavcopy is not available yet: {e}")
        
    return False


def load_bhavcopy_dict(target_expiry_str):
    bhav_map = {}
    if not os.path.exists("bhavcopy.csv"):
        return bhav_map

    possible_expiries = set()
    clean_target = target_expiry_str.strip().upper()
    possible_expiries.add(clean_target)
    
    try:
        dt_obj = datetime.datetime.strptime(clean_target, "%Y-%m-%d")
        possible_expiries.add(dt_obj.strftime("%Y-%m-%d"))
        possible_expiries.add(dt_obj.strftime("%d-%b-%Y").upper())
        possible_expiries.add(dt_obj.strftime("%d-%B-%Y").upper())
        possible_expiries.add(dt_obj.strftime("%d%b%Y").upper())
        possible_expiries.add(dt_obj.strftime("%d%b%y").upper())
    except Exception:
        pass

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


def get_strike_close_price(bhav_map, item, strike, opt_type):
    data_dict = bhav_map.get((strike, opt_type), {})
    if data_dict.get("close", 0.0) > 0:
        return data_dict["close"]
    
    opts = item.get("CE", {}) if opt_type == "CE" else item.get("PE", {})
    return float(opts.get("closePrice") or opts.get("lastPrice") or 0.0)


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


def calculate_zone_row_one(wl, wh, bhav_map, chain_data):
    """Calculates Row 1 Upper and Lower Zones using floor (wl) and ceiling (wh) strikes only."""
    def get_p(s, t):
        p = bhav_map.get((s, t), {}).get("close", 0.0)
        if p > 0: return p
        if not chain_data: return 0.0
        data_list = chain_data.get("records", {}).get("data", [])
        for item in data_list:
            if int(round(float(item.get("strikePrice", 0)))) == s:
                opts = item.get("CE", {}) if t == "CE" else item.get("PE", {})
                return float(opts.get("closePrice") or opts.get("lastPrice") or 0.0)
        return 0.0

    ce_close = get_p(wh, "CE")  # CE close at Ceiling Strike (wh)
    pe_close = get_p(wl, "PE")  # PE close at Floor Strike (wl)

    return {
        "line1": round(wh + ce_close, 2),  # Upper Zone: Ceiling Strike + CE Close
        "line2": round(wl - pe_close, 2)   # Lower Zone: Floor Strike - PE Close
    }


def process_and_save_data(chain_raw_data, spot, w_exp, m_exp):
    data = chain_raw_data.get("records", {}).get("data", [])
    if not data or spot <= 0:
        print("Invalid data or spot price received.")
        return

    now_ist = datetime.datetime.now(IST)
    actual_today_str = now_ist.strftime("%d %b %Y").upper()
    
    # Calculate next trading day for display
    next_trading_date = get_next_trading_day(now_ist)
    display_date_str = next_trading_date.strftime("%d %b %Y").upper()

    bhavcopy_is_ready = download_today_bhavcopy()
    w_bhav = load_bhavcopy_dict(w_exp)
    m_bhav = load_bhavcopy_dict(m_exp)

    min_diff = float('inf')
    hlc_atm_strike = int(round(spot / 50.0) * 50)

    for item in data:
        item_strike = item.get("strikePrice")
        if item_strike is None: continue
        s_val = int(round(float(item_strike)))
        if abs(s_val - spot) > 500: continue

        ce_close = get_strike_close_price(w_bhav, item, s_val, "CE")
        pe_close = get_strike_close_price(w_bhav, item, s_val, "PE")

        if ce_close > 0 and pe_close > 0:
            diff = abs(ce_close - pe_close)
            if diff < min_diff:
                min_diff = diff
                hlc_atm_strike = s_val

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

    s1_atm_ce_val, s1_atm_pe_val = 0.0, 0.0
    s2_atm_ce_val, s2_atm_pe_val = 0.0, 0.0
    s1_ce_val, s1_pe_val = 0.0, 0.0
    s2_ce_val, s2_pe_val = 0.0, 0.0

    for item in data:
        item_strike = item.get("strikePrice")
        if item_strike is None: continue
        s_val = int(round(float(item_strike)))
        
        call_opts = item.get("CE", {})
        put_opts = item.get("PE", {})

        if s_val == hlc_atm_strike:
            if ce_close == 0.0: ce_close = float(call_opts.get("closePrice") or call_opts.get("lastPrice") or 0.0)
            if ce_high == 0.0: ce_high = float(call_opts.get("highPrice") or ce_close)
            if ce_low == 0.0: ce_low = float(call_opts.get("lowPrice") or ce_close)
            ce_dict["close"], ce_dict["high"], ce_dict["low"] = ce_close, ce_high, ce_low

            if pe_close == 0.0: pe_close = float(put_opts.get("closePrice") or put_opts.get("lastPrice") or 0.0)
            if pe_high == 0.0: pe_high = float(put_opts.get("highPrice") or pe_close)
            if pe_low == 0.0: pe_low = float(put_opts.get("lowPrice") or pe_close)
            pe_dict["close"], pe_dict["high"], pe_dict["low"] = pe_close, pe_high, pe_low

        if s_val == sniper1_atm_strike:
            s1_atm_ce_val = get_strike_close_price(w_bhav, item, s_val, "CE")
            s1_atm_pe_val = get_strike_close_price(w_bhav, item, s_val, "PE")
        if s_val == target_s1_ce_strike: s1_ce_val = get_strike_close_price(w_bhav, item, s_val, "CE")
        if s_val == target_s1_pe_strike: s1_pe_val = get_strike_close_price(w_bhav, item, s_val, "PE")

        if s_val == sniper2_atm_strike:
            s2_atm_ce_val = get_strike_close_price(w_bhav, item, s_val, "CE")
            s2_atm_pe_val = get_strike_close_price(w_bhav, item, s_val, "PE")
        if s_val == target_s2_ce_strike: s2_ce_val = get_strike_close_price(w_bhav, item, s_val, "CE")
        if s_val == target_s2_pe_strike: s2_pe_val = get_strike_close_price(w_bhav, item, s_val, "PE")

    ce_tag, ce_class = get_market_sentiment_tag(ce_dict)
    pe_tag, pe_class = get_market_sentiment_tag(pe_dict)

    sniper1_val = round((s1_ce_val + s1_pe_val) / 2.0, 2)
    sniper2_val = round((s2_ce_val + s2_pe_val) / 2.0, 2)

    min_supply_val = round(hlc_atm_strike + ce_close, 2)
    min_demand_val = round(hlc_atm_strike - pe_close, 2)
    max_supply_val = round(hlc_atm_strike + (ce_close + pe_close), 2)
    max_demand_val = round(hlc_atm_strike - (ce_close + pe_close), 2)

    # Compute Floor (wl) and Ceiling (wh) strikes for Row 1
    wl = int(math.floor(spot / 100.0) * 100)
    wh = int(math.ceil(spot / 100.0) * 100)

    weekly_zones = calculate_zone_row_one(wl, wh, w_bhav, chain_raw_data)
    monthly_zones = calculate_zone_row_one(wl, wh, m_bhav, chain_raw_data)

    payload = {
        "dataStatus": "SUCCESS",
        "bhavcopyReady": bhavcopy_is_ready,
        "currentDate": display_date_str,
        "lastExecutionDate": actual_today_str,
        "expiryDate": datetime.datetime.strptime(w_exp, "%Y-%m-%d").strftime("%d-%b-%Y").upper(),
        "spotPrice": spot,
        "hlcAtmStrike": hlc_atm_strike,
        "ce": {"high": round(ce_high, 2), "close": round(ce_close, 2), "low": round(ce_low, 2)},
        "pe": {"high": round(pe_high, 2), "close": round(pe_close, 2), "low": round(pe_low, 2)},
        "ceTag": ce_tag, "ceClass": ce_class,
        "peTag": pe_tag, "peClass": pe_class,
        "bannerTotal": round(abs(ce_close - pe_close), 2),  # Direct absolute CE - PE difference
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
        
    print(f"Data saved. Bhavcopy status: {bhavcopy_is_ready}")
    push_to_github()


if __name__ == "__main__":
    if os.path.exists("data.json"):
        try:
            with open("data.json", "r") as f:
                existing_data = json.load(f)
                now_ist = datetime.datetime.now(IST)
                actual_today_str = now_ist.strftime("%d %b %Y").upper()
                
                if existing_data.get("lastExecutionDate") == actual_today_str and existing_data.get("bhavcopyReady") is True:
                    print("✅ Bhavcopy already successfully fetched and saved for today. Skipping execution.")
                    exit(0)
        except Exception:
            pass

    chain_raw_data, spot = fetch_nse_option_chain()
    if chain_raw_data and spot > 0:
        w_exp, m_exp = get_expiries_from_chain(chain_raw_data)
        process_and_save_data(chain_raw_data, spot, w_exp, m_exp)
    else:
        print("Failed to fetch live option chain or spot price from NSE.")
