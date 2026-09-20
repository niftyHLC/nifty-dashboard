import csv
import datetime
import io
import json
import math
import os
import subprocess
import time
import zipfile
import holidays
import requests
import yfinance as yf

# Indian Standard Time (IST) offset
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def get_market_holidays():
    """Dynamically computes Indian public and market holidays automatically."""
    current_year = datetime.datetime.now(IST).year
    in_holidays = holidays.India(years=current_year)
    return set(in_holidays.keys())

MARKET_HOLIDAYS = get_market_holidays()


def push_to_github():
    try:
        subprocess.run(["git", "config", "--global", "user.name", "github-actions[bot]"], check=True)
        subprocess.run(["git", "config", "--global", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
        
        subprocess.run(["git", "add", "-f", "data.json"], check=False)
        if os.path.exists("bhavcopy.csv"):
            subprocess.run(["git", "add", "-f", "bhavcopy.csv"], check=False)
        
        diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        
        if diff_check.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Auto-update weekend/holiday/IV status [skip ci]"], check=True)
            subprocess.run(["git", "push", "origin", "main"], check=True)
            print("Changes pushed to GitHub successfully.")
        else:
            print("No changes detected in repository. Skipping commit.")
    except Exception as e:
        print(f"Git push failed: {e}")


def fetch_live_spot_from_yahoo():
    """Fetches real-time or end-of-day Nifty 50 High, Low, and Close prices from Yahoo Finance with fallback."""
    try:
        ticker = yf.Ticker("^NSEI")
        todays_data = ticker.history(period="1d")
        if not todays_data.empty:
            spot_close = float(todays_data["Close"].iloc[-1])
            spot_high = float(todays_data["High"].iloc[-1])
            spot_low = float(todays_data["Low"].iloc[-1])
            if spot_close > 0:
                return spot_close, spot_high, spot_low
    except Exception as e:
        print(f"Failed to fetch spot from Yahoo Finance: {e}")
    
    print("⚠️ Using fallback spot values due to Yahoo Finance connection block.")
    return 23398.10, 23448.10, 23231.40


def fetch_nse_option_chain_data(symbol="NIFTY"):
    """Fetches live option chain JSON directly from NSE using a persistent session and correct cookie headers."""
    base_url = "https://www.nseindia.com"
    api_url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive"
    }

    session = requests.Session()
    try:
        session.get(base_url, headers=headers, timeout=10)
        time.sleep(1)
        response = session.get(api_url, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            print(f"⚠️ NSE API responded with status code: {response.status_code}")
    except Exception as e:
        print(f"Failed to fetch live NSE option chain: {e}")
    return None


def get_live_iv_from_nse(hlc_atm_strike, target_expiry):
    """Searches live NSE option chain JSON to extract accurate live Implied Volatility (IV)."""
    data = fetch_nse_option_chain_data("NIFTY")
    ce_iv = 0.0
    pe_iv = 0.0

    if not data:
        print("⚠️ Live NSE option chain data returned None. IV will fall back to Bhavcopy if available.")
        return ce_iv, pe_iv

    target_dt = None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y", "%d%b%y"):
        try:
            target_dt = datetime.datetime.strptime(str(target_expiry).strip(), fmt)
            break
        except ValueError:
            continue

    try:
        records = data.get("records", {}).get("data", [])
        for item in records:
            if float(item.get("strikePrice", 0)) == float(hlc_atm_strike):
                ce_data = item.get("CE", {})
                pe_data = item.get("PE", {})
                
                if ce_data:
                    ce_exp_str = str(ce_data.get("expiryDate", "")).strip()
                    ce_dt = None
                    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y"):
                        try:
                            ce_dt = datetime.datetime.strptime(ce_exp_str, fmt)
                            break
                        except ValueError:
                            continue
                    
                    if (target_dt and ce_dt and target_dt == ce_dt) or (ce_exp_str.upper() == str(target_expiry).upper()):
                        ce_iv = float(ce_data.get("impliedVolatility", 0.0))

                if pe_data:
                    pe_exp_str = str(pe_data.get("expiryDate", "")).strip()
                    pe_dt = None
                    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y"):
                        try:
                            pe_dt = datetime.datetime.strptime(pe_exp_str, fmt)
                            break
                        except ValueError:
                            continue
                        
                    if (target_dt and pe_dt and target_dt == pe_dt) or (pe_exp_str.upper() == str(target_expiry).upper()):
                        pe_iv = float(pe_data.get("impliedVolatility", 0.0))
                break
    except Exception as e:
        print(f"Error parsing live IV from NSE data: {e}")

    print(f"🔍 Live IV Lookup for Strike {hlc_atm_strike} -> CE IV: {ce_iv}, PE IV: {pe_iv}")
    return ce_iv, pe_iv


def calculate_asymmetric_time_value(tv_atm_strike, target_expiry, bhav_map=None):
    """Calculates Asymmetric Cross-Strike Time Value using the TV ATM strike center."""
    strike_map = {}
    
    data = fetch_nse_option_chain_data("NIFTY")
    if data:
        try:
            records = data.get("records", {}).get("data", [])
            for item in records:
                strike = float(item.get("strikePrice", 0))
                expiry = item.get("CE", {}).get("expiryDate") or item.get("PE", {}).get("expiryDate")
                
                if expiry and str(expiry).strip().upper() == str(target_expiry).strip().upper():
                    ce_ltp = float(item.get("CE", {}).get("lastPrice", 0.0)) if "CE" in item else 0.0
                    pe_ltp = float(item.get("PE", {}).get("lastPrice", 0.0)) if "PE" in item else 0.0
                    if ce_ltp > 0 or pe_ltp > 0:
                        strike_map[strike] = {"ce": ce_ltp, "pe": pe_ltp}
        except Exception as e:
            print(f"Error parsing live asymmetric time value: {e}")

    if not strike_map and bhav_map:
        all_strikes = set(s for s, t in bhav_map.keys())
        for strike in all_strikes:
            ce_close = bhav_map.get((strike, "CE"), {}).get("close", 0.0)
            pe_close = bhav_map.get((strike, "PE"), {}).get("close", 0.0)
            if ce_close > 0 or pe_close > 0:
                strike_map[strike] = {"ce": ce_close, "pe": pe_close}

    if not strike_map:
        return {"total": 0.0, "ceStrike": 0, "ceLtp": 0.0, "peStrike": 0, "peLtp": 0.0}

    try:
        sorted_strikes = sorted(strike_map.keys())
        if tv_atm_strike in sorted_strikes:
            atm_idx = sorted_strikes.index(tv_atm_strike)
        else:
            atm_strike = min(sorted_strikes, key=lambda x: abs(x - tv_atm_strike))
            atm_idx = sorted_strikes.index(atm_strike)

        if atm_idx > 0 and atm_idx < len(sorted_strikes) - 1:
            ce_strike_above = sorted_strikes[atm_idx + 1]
            pe_strike_below = sorted_strikes[atm_idx - 1]

            ce_ltp = strike_map[ce_strike_above]["ce"]
            pe_ltp = strike_map[pe_strike_below]["pe"]
            total_tv = round(ce_ltp + pe_ltp, 2)

            return {
                "total": total_tv,
                "ceStrike": ce_strike_above,
                "ceLtp": ce_ltp,
                "peStrike": pe_strike_below,
                "peLtp": pe_ltp
            }
    except Exception as e:
        print(f"Error calculating asymmetric time value: {e}")

    return {"total": 0.0, "ceStrike": 0, "ceLtp": 0.0, "peStrike": 0, "peLtp": 0.0}


def get_valid_highs(candles, max_count=5):
    """Identifies unique unbroken swing highs strictly from RED candles, scanning newest to oldest."""
    valid_highs = []
    if not candles:
        return valid_highs
    
    n = len(candles)
    # Scan backwards from the newest candle to the oldest candle
    for i in range(n - 1, -1, -1):
        current = candles[i]
        
        # Filter: Skip if candle is not red (Close >= Open)
        if current['close'] >= current['open']:
            continue
            
        high_val = current['high']
        is_broken = False
        
        # Check if any subsequent candle breaks this high
        for j in range(i + 1, n):
            if candles[j]['high'] > high_val:
                is_broken = True
                break
        
        if not is_broken and high_val not in valid_highs:
            valid_highs.append(high_val)
            
        if len(valid_highs) >= max_count:
            break
            
    return valid_highs


def fetch_daily_candles_for_sellers_area():
    """Fetches recent daily candles from Yahoo Finance including open price."""
    try:
        ticker = yf.Ticker("^NSEI")
        df = ticker.history(period="6mo", interval="1d")
        if not df.empty:
            candles = []
            for _, row in df.iterrows():
                candles.append({
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"])
                })
            return candles
    except Exception as e:
        print(f"⚠️ Could not fetch daily candles: {e}")
    return []


def download_today_bhavcopy(max_retries=5, delay_seconds=60):
    """Downloads official Bhavcopy directly from NSE archives with built-in retry-and-sleep loop."""
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
    
    for attempt in range(1, max_retries + 1):
        try:
            print(f"⏳ Attempt {attempt}/{max_retries}: Checking/Downloading Bhavcopy for {now_ist.strftime('%Y-%m-%d')}...")
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

                print(f"Successfully downloaded Bhavcopy for {now_ist.strftime('%Y-%m-%d')}")
                return True
            else:
                print(f"⚠️ Server returned status {response.status_code}. File not ready yet.")
        except Exception as e:
            print(f"⚠️ Notice: Could not download bhavcopy on attempt {attempt}: {e}")
        
        if attempt < max_retries:
            print(f"Sleeping for {delay_seconds} seconds before retrying...")
            time.sleep(delay_seconds)
            
    print("ℹ️ Max retries reached for Bhavcopy. Proceeding with fallback...")
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
                    iv = float(cleaned_row.get("IV") or cleaned_row.get("IMPLIED_VOL") or cleaned_row.get("IMPL_VOL") or cleaned_row.get("CLIENT_IV") or 0.0)

                    if high > 0 or low > 0 or close > 0:
                        bhav_map[(row_strike, opt_type)] = {
                            "open": open_p, "high": high, "low": low, "close": close, "chg_oi": chg_oi, "iv": iv
                        }
    except Exception as e:
        print(f"Error reading bhavcopy into dict: {e}")

    return bhav_map


def calculate_dominance_metrics(data_dict):
    """Calculates H-C and C-L distances, applies 1.5x rule, and keeps IV cleanly structured."""
    if not data_dict:
        return {
            "high": 0.0, "close": 0.0, "low": 0.0, "iv": 0.0,
            "hc": 0.0, "cl": 0.0,
            "dominance": "NEUTRAL",
            "themeColor": "orange",
            "tagClass": "tag-neutral"
        }
    
    high = data_dict.get("high", 0.0)
    low = data_dict.get("low", 0.0)
    close = data_dict.get("close", 0.0)
    iv = data_dict.get("iv", 0.0)
    
    hc = round(high - close, 2)
    cl = round(close - low, 2)
    
    if high == low or (hc == 0 and cl == 0):
        dominance = "NEUTRAL"
        theme_color = "orange"
        tag_class = "tag-neutral"
    elif cl >= (1.5 * hc):
        dominance = "BUYERS"
        theme_color = "green"
        tag_class = "tag-buyers"
    elif hc >= (1.5 * cl):
        dominance = "SELLERS"
        theme_color = "red"
        tag_class = "tag-sellers"
    else:
        dominance = "NEUTRAL"
        theme_color = "orange"
        tag_class = "tag-neutral"
        
    return {
        "high": round(high, 2),
        "close": round(close, 2),
        "low": round(low, 2),
        "iv": round(iv, 2),
        "hc": hc,
        "cl": cl,
        "dominance": dominance,
        "themeColor": theme_color,
        "tagClass": tag_class
    }


def calculate_zone_row_one(wl, wh, bhav_map):
    """Calculates Line 1 and Line 2 according to mathematical zone formulas."""
    def get_p(s, t):
        return bhav_map.get((s, t), {}).get("close", 0.0)

    ce1 = get_p(wl, "CE")
    pe1 = get_p(wl, "PE")
    sum1 = ce1 + pe1

    ce2 = get_p(wh, "CE")
    pe2 = get_p(wh, "PE")
    sum2 = ce2 + pe2

    return {
        "line1": round(wl + sum1, 2),
        "line2": round(wh - sum2, 2)
    }


def get_display_date(now_ist):
    """Calculates active or next trading day date dynamically."""
    target = now_ist.date()
    current_year_holidays = get_market_holidays()
    
    market_closed = (now_ist.hour > 15) or (now_ist.hour == 15 and now_ist.minute >= 30)
    is_off_day = target.weekday() >= 5 or target in current_year_holidays

    if market_closed or is_off_day:
        target += datetime.timedelta(days=1)

    while True:
        if target.weekday() >= 5 or target in current_year_holidays:
            target += datetime.timedelta(days=1)
        else:
            break
            
    return target.strftime("%d %b %Y").upper()


def process_and_save_data(spot, spot_high, spot_low, force_not_ready=False):
    now_ist = datetime.datetime.now(IST)
    
    today_str = get_display_date(now_ist)
    bhavcopy_is_ready = False if force_not_ready else download_today_bhavcopy()

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

    market_closed_today = (now_ist.hour > 15) or (now_ist.hour == 15 and now_ist.minute >= 30)
    today_dt = now_ist.replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)

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
    
    if sorted_expiries_tuples:
        w_exp_dt, w_exp_str = sorted_expiries_tuples[0]
        w_exp = w_exp_str
        
        target_month = w_exp_dt.month
        target_year = w_exp_dt.year
        
        same_month_expiries = [item for item in sorted_expiries_tuples if item[0].month == target_month and item[0].year == target_year]
        m_exp = same_month_expiries[-1][1] if same_month_expiries else w_exp
    else:
        w_exp = now_ist.strftime("%d-%m-%y").upper()
        m_exp = w_exp

    w_bhav = load_bhavcopy_dict(w_exp)
    m_bhav = load_bhavcopy_dict(m_exp)

    # --- TV ATM SMALLEST STRIKE METHOD ---
    min_time_val = float('inf')
    hlc_atm_strike = int(round(spot / 50.0) * 50) if spot > 0 else 23450

    for (strike, opt_type), d_val in w_bhav.items():
        if abs(strike - spot) <= 500:
            ce_close = w_bhav.get((strike, "CE"), {}).get("close", 0.0)
            pe_close = w_bhav.get((strike, "PE"), {}).get("close", 0.0)
            if ce_close > 0 and pe_close > 0:
                time_val = ce_close + pe_close
                if time_val < min_time_val:
                    min_time_val = time_val

    candidate_strikes = []
    for (strike, opt_type), d_val in w_bhav.items():
        if abs(strike - spot) <= 500:
            ce_close = w_bhav.get((strike, "CE"), {}).get("close", 0.0)
            pe_close = w_bhav.get((strike, "PE"), {}).get("close", 0.0)
            if ce_close > 0 and pe_close > 0:
                time_val = ce_close + pe_close
                if abs(time_val - min_time_val) <= 0.5:
                    candidate_strikes.append(strike)

    if candidate_strikes:
        hlc_atm_strike = min(candidate_strikes)
    # ------------------------------------

    sniper1_atm_strike = int(round(spot / 100.0) * 100) if spot > 0 else 23400
    sniper2_atm_strike = hlc_atm_strike

    target_s1_ce_strike = sniper1_atm_strike + 100
    target_s1_pe_strike = sniper1_atm_strike - 100
    target_s2_ce_strike = sniper2_atm_strike + 100
    target_s2_pe_strike = sniper2_atm_strike - 100

    ce_dict = w_bhav.get((int(hlc_atm_strike), "CE"), {"high": 0.0, "low": 0.0, "close": 0.0, "open": 0.0, "chg_oi": 0.0, "iv": 0.0})
    pe_dict = w_bhav.get((int(hlc_atm_strike), "PE"), {"high": 0.0, "low": 0.0, "close": 0.0, "open": 0.0, "chg_oi": 0.0, "iv": 0.0})

    ce_metrics = calculate_dominance_metrics(ce_dict)
    pe_metrics = calculate_dominance_metrics(pe_dict)

    live_ce_iv, live_pe_iv = get_live_iv_from_nse(hlc_atm_strike, w_exp)
    if live_ce_iv > 0:
        ce_metrics["iv"] = round(live_ce_iv, 2)
    if live_pe_iv > 0:
        pe_metrics["iv"] = round(live_pe_iv, 2)

    asymmetric_tv_data = calculate_asymmetric_time_value(hlc_atm_strike, w_exp, w_bhav)

    s1_atm_ce_val = w_bhav.get((sniper1_atm_strike, "CE"), {}).get("close", 0.0)
    s1_atm_pe_val = w_bhav.get((sniper1_atm_strike, "PE"), {}).get("close", 0.0)
    s1_ce_val = w_bhav.get((target_s1_ce_strike, "CE"), {}).get("close", 0.0)
    s1_pe_val = w_bhav.get((target_s1_pe_strike, "PE"), {}).get("close", 0.0)

    s2_atm_ce_val = w_bhav.get((sniper2_atm_strike, "CE"), {}).get("close", 0.0)
    s2_atm_pe_val = w_bhav.get((sniper2_atm_strike, "PE"), {}).get("close", 0.0)
    s2_ce_val = w_bhav.get((target_s2_ce_strike, "CE"), {}).get("close", 0.0)
    s2_pe_val = w_bhav.get((target_s2_pe_strike, "PE"), {}).get("close", 0.0)

    sniper1_val = round((s1_ce_val + s1_pe_val) / 2.0, 2)
    sniper2_val = round((s2_ce_val + s2_pe_val) / 2.0, 2)

    min_supply_val = round(hlc_atm_strike + ce_metrics["close"], 2)
    min_demand_val = round(hlc_atm_strike - pe_metrics["close"], 2)
    max_supply_val = round(hlc_atm_strike + (ce_metrics["close"] + pe_metrics["close"]), 2)
    max_demand_val = round(hlc_atm_strike - (ce_metrics["close"] + pe_metrics["close"]), 2)

    # Fetch daily candles and grab the 5 unbroken red swing highs from newest to oldest
    daily_candles = fetch_daily_candles_for_sellers_area()
    dynamic_sellers_highs = get_valid_highs(daily_candles, max_count=5)

    wl = int(math.floor(spot / 100.0) * 100) if spot > 0 else 23300
    wh = int(math.ceil(spot / 100.0) * 100) if spot > 0 else 23400

    weekly_zones = calculate_zone_row_one(wl, wh, w_bhav)
    monthly_zones = calculate_zone_row_one(wl, wh, m_bhav)

    spot_difference = round(spot_high - spot_low, 2)
    earth_level = round(spot_difference * 0.2611, 2)

    hide_sniper2 = (sniper1_atm_strike == sniper2_atm_strike)

    payload = {
        "dataStatus": "SUCCESS",
        "bhavcopyReady": bhavcopy_is_ready,
        "currentDate": today_str,
        "expiryDate": w_exp,
        "spotPrice": spot,
        "hlcAtmStrike": hlc_atm_strike,
        "ce": ce_metrics,
        "pe": pe_metrics,
        "ceTag": ce_metrics["dominance"], 
        "ceClass": ce_metrics["tagClass"],
        "peTag": pe_metrics["dominance"], 
        "peClass": pe_metrics["tagClass"],
        "bannerTotal": round(ce_metrics["close"] + pe_metrics["close"], 2),
        "asymmetricTimeValue": asymmetric_tv_data,
        "minSupply": min_supply_val,
        "minDemand": min_demand_val,
        "maxSupply": max_supply_val,
        "maxDemand": max_demand_val,
        "sellersArea": {
            "min": min_supply_val,
            "max": max_supply_val,
            "validUnbrokenHighs": dynamic_sellers_highs
        },
        "buyersArea": {
            "min": min_demand_val,
            "max": max_demand_val
        },
        "weeklyZones": weekly_zones,
        "monthlyZones": monthly_zones,
        "spotHigh": spot_high,
        "spotLow": spot_low,
        "spotDifference": spot_difference,
        "earthLevel": earth_level,
        "sniper1": {
            "strike": sniper1_atm_strike, "ce": round(s1_atm_ce_val, 2), "pe": round(s1_atm_pe_val, 2),
            "otmCeStrike": target_s1_ce_strike, "otmPeStrike": target_s1_pe_strike,
            "otmCe": round(s1_ce_val, 2), "otmPe": round(s1_pe_val, 2), "value": sniper1_val
        },
        "sniper2": None if hide_sniper2 else {
            "strike": sniper2_atm_strike, "ce": round(s2_atm_ce_val, 2), "pe": round(s2_atm_pe_val, 2),
            "otmCeStrike": target_s2_ce_strike, "otmPeStrike": target_s2_pe_strike,
            "otmCe": round(s2_ce_val, 2), "otmPe": round(s2_pe_val, 2), "value": sniper2_val
        }
    }

    with open("data.json", "w") as f:
        json.dump(payload, f, indent=4)
        
    print(f"Data saved successfully. Active Weekly Expiry: {w_exp} | Active Monthly Expiry: {m_exp}")
    push_to_github()


if __name__ == "__main__":
    now_ist = datetime.datetime.now(IST)
    today_date = now_ist.date()
    
    is_weekend = today_date.weekday() >= 5
    current_year_holidays = get_market_holidays()
    is_holiday = today_date in current_year_holidays

    if is_weekend or is_holiday:
        reason = "WEEKEND" if is_weekend else "HOLIDAY"
        print(f"🛑 Today is a {reason}. Setting bhavcopyReady to False for dashboard notification.")
        spot, spot_high, spot_low = fetch_live_spot_from_yahoo()
        if spot <= 0:
            spot = 23398.10
            spot_high = 23448.10
            spot_low = 23231.40
        process_and_save_data(spot, spot_high, spot_low, force_not_ready=True)
        exit(0)

    if os.path.exists("data.json"):
        try:
            with open("data.json", "r") as f:
                existing_data = json.load(f)
                today_str = get_display_date(now_ist)
                
                if existing_data.get("currentDate") == today_str and existing_data.get("bhavcopyReady") is True:
                    print("✅ Today's Bhavcopy is already downloaded and processed. Stopping execution for today.")
                    exit(0)
        except Exception:
            pass

    spot, spot_high, spot_low = fetch_live_spot_from_yahoo()
    if spot > 0:
        print(f"Retrieved Spot Price from Yahoo Finance -> Close: {spot}, High: {spot_high}, Low: {spot_low}")
        process_and_save_data(spot, spot_high, spot_low, force_not_ready=False)
    else:
        print("Failed to retrieve spot price.")
