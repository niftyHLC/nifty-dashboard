import csv
import datetime
import io
import json
import math
import os
import subprocess
import time
import holidays
import numpy as np
import requests
from scipy.optimize import brentq
from scipy.stats import norm
import yfinance as yf
import zipfile

# Indian Standard Time (IST) offset
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

NSE_HOME_URL = "https://www.nseindia.com"
NSE_HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"

def get_nse_headers():
    """Common browser-like headers used for NSE requests."""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/153.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": NSE_HOME_URL + "/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

def parse_any_date(value):
    """Parse common NSE/Yahoo date representations into a date object."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value

    value = str(value).strip()
    if not value:
        return None

    formats = (
        "%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y",
        "%d-%m-%y", "%d-%m-%Y", "%d%b%Y",
        "%d%b%y", "%Y/%m/%d", "%d/%m/%Y",
    )
    for fmt in formats:
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None

def get_market_holidays(year=None):
    """
    Gets the actual NSE trading-holiday calendar first.
    Falls back to holidays.India() only if NSE is unavailable.
    """
    year = year or datetime.datetime.now(IST).year
    holidays_set = set()

    try:
        session = requests.Session()
        headers = get_nse_headers()
        session.get(NSE_HOME_URL, headers=headers, timeout=10)
        response = session.get(NSE_HOLIDAY_API, headers=headers, timeout=10)

        if response.status_code == 200:
            payload = response.json()

            def collect_dates(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        key_l = str(key).lower()
                        if key_l in {"tradingdate", "date", "trading_date"}:
                            parsed = parse_any_date(value)
                            if parsed and parsed.year == year:
                                holidays_set.add(parsed)
                        else:
                            collect_dates(value)
                elif isinstance(node, list):
                    for item in node:
                        collect_dates(item)

            collect_dates(payload)

        if holidays_set:
            return holidays_set

    except Exception as e:
        print(f"⚠️ NSE holiday API unavailable: {e}")

    # Conservative fallback only. This is not treated as the primary calendar.
    try:
        in_holidays = holidays.India(years=year)
        holidays_set = {
            d for d in in_holidays.keys()
            if isinstance(d, datetime.date)
        }
    except Exception as e:
        print(f"⚠️ India holiday fallback unavailable: {e}")

    return holidays_set

MARKET_HOLIDAYS = get_market_holidays()

def fetch_live_spot_from_yahoo():
    """
    Fetches the latest available NIFTY 50 OHLC from Yahoo Finance.
    Never returns hard-coded/stale fallback prices.
    """
    try:
        ticker = yf.Ticker("^NSEI")
        df = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
            actions=False,
        )

        if df is None or df.empty:
            raise RuntimeError("Yahoo returned no NIFTY data.")

        # Prefer today's IST date when a row is available.
        today = datetime.datetime.now(IST).date()
        selected = None

        for idx in reversed(df.index):
            try:
                row_date = idx.to_pydatetime().date()
            except Exception:
                row_date = getattr(idx, "date", lambda: None)()

            if row_date == today:
                selected = df.loc[idx]
                break

        if selected is None:
            selected = df.iloc[-1]
            print("ℹ️ Yahoo has no row for today; using the latest available trading-day close.")

        spot_close = float(selected["Close"])
        spot_high = float(selected["High"])
        spot_low = float(selected["Low"])

        if not all(math.isfinite(v) and v > 0 for v in (spot_close, spot_high, spot_low)):
            raise ValueError("Yahoo returned invalid NIFTY OHLC values.")

        return spot_close, spot_high, spot_low

    except Exception as e:
        print(f"❌ Failed to fetch NIFTY spot from Yahoo Finance: {e}")
        return None, None, None

def fetch_nse_option_chain_data(symbol="NIFTY", max_retries=3):
    """Fetches NSE option-chain JSON with session warm-up and retries."""
    api_url = f"{NSE_HOME_URL}/api/option-chain-indices?symbol={symbol}"

    for attempt in range(1, max_retries + 1):
        try:
            session = requests.Session()
            headers = get_nse_headers()
            headers["X-Requested-With"] = "XMLHttpRequest"

            home = session.get(NSE_HOME_URL, headers=headers, timeout=10)
            if home.status_code not in (200, 403):
                print(f"⚠️ NSE home-page status: {home.status_code}")

            response = session.get(api_url, headers=headers, timeout=15)

            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload:
                    return payload

            print(
                f"⚠️ NSE option-chain attempt {attempt}/{max_retries} "
                f"returned HTTP {response.status_code}."
            )

        except Exception as e:
            print(
                f"⚠️ NSE option-chain attempt {attempt}/{max_retries} failed: {e}"
            )

        if attempt < max_retries:
            time.sleep(2 * attempt)

    return None

def get_iv_atm_strike(spot):
    """Finds the true IV ATM strike by rounding the spot price to the nearest 50."""
    atm = int(round(spot / 50.0) * 50)
    print(f"🔍 True IV ATM Strike calculated from spot {spot} -> {atm}")
    return atm

def calculate_black_scholes_iv(option_type, price, spot, strike, expiry_date_str):
    """Calculates Implied Volatility using the Black-Scholes model as a robust mathematical fallback."""
    if price <= 0 or spot <= 0 or strike <= 0:
        return 0.0

    try:
        target_dt = None
        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y", "%d%b%y"):
            try:
                target_dt = datetime.datetime.strptime(str(expiry_date_str).strip(), fmt)
                break
            except ValueError:
                continue

        if not target_dt:
            return 0.0

        today_dt = datetime.datetime.now(IST).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)
        days_to_expiry = (target_dt - today_dt).days
        T = max(days_to_expiry, 1) / 365.0

        r = 0.065  

        intrinsic = max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
        if price < intrinsic:
            return 0.0

        def objective_function(sigma):
            d1 = (np.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
            d2 = d1 - sigma * np.sqrt(T)
            if option_type == "CE":
                bs_price = spot * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2)
            else:
                bs_price = strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)
            return bs_price - price

        implied_vol = brentq(objective_function, 1e-9, 5.0, maxiter=100)
        return round(float(implied_vol) * 100, 2)
    except Exception:
        return 0.0

def get_live_iv_from_nse(atm_strike, target_expiry, option_type, price, spot):
    """Searches live NSE option chain JSON to extract accurate live IV, falling back to Black-Scholes if needed."""
    data = fetch_nse_option_chain_data("NIFTY")
    iv_val = 0.0

    if data:
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
                if float(item.get("strikePrice", 0)) == float(atm_strike):
                    opt_data = item.get(option_type, {})
                    if opt_data:
                        exp_str = str(opt_data.get("expiryDate", "")).strip()
                        opt_dt = None
                        for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y"):
                            try:
                                opt_dt = datetime.datetime.strptime(exp_str, fmt)
                                break
                            except ValueError:
                                continue
                     
                        if (target_dt and opt_dt and target_dt == opt_dt) or (exp_str.upper() == str(target_expiry).upper()):
                            iv_val = float(opt_data.get("impliedVolatility", 0.0))
                        break
        except Exception as e:
            print(f"Error parsing live IV from NSE data: {e}")

    if iv_val <= 0.0:
        iv_val = calculate_black_scholes_iv(option_type, price, spot, atm_strike, target_expiry)

    return iv_val

def calculate_asymmetric_time_value(spot, target_expiry, bhav_map=None):
    iv_atm_strike = get_iv_atm_strike(spot)
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
            print(f"Error parsing live time value: {e}")

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
        
        if iv_atm_strike in sorted_strikes:
            atm_idx = sorted_strikes.index(iv_atm_strike)
        else:
            closest_atm = min(sorted_strikes, key=lambda x: abs(x - iv_atm_strike))
            atm_idx = sorted_strikes.index(closest_atm)

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
        print(f"Error calculating time value: {e}")

    return {"total": 0.0, "ceStrike": 0, "ceLtp": 0.0, "peStrike": 0, "peLtp": 0.0}

def get_valid_highs(candles, max_count=5):
    valid_highs = []
    if not candles:
        return valid_highs
    
    n = len(candles)
    for i in range(n - 1, -1, -1):
        current = candles[i]
        if current['close'] >= current['open']:
            continue
            
        high_val = current['high']
        is_broken = False
        
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

def download_today_bhavcopy(max_retries=3, delay_seconds=30):
    headers = get_nse_headers()
    headers["Accept"] = "text/csv,application/zip,application/octet-stream,*/*"

    now_ist = datetime.datetime.now(IST)
    yyyy = now_ist.strftime("%Y")
    mm = now_ist.strftime("%m")
    dd = now_ist.strftime("%d")
    
    url = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyy}{mm}{dd}_F_0000.csv.zip"
    
    for attempt in range(1, max_retries + 1):
        try:
            print(f"⏳ Attempt {attempt}/{max_retries}: Downloading Bhavcopy for {now_ist.strftime('%Y-%m-%d')}...")
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

def load_bhavcopy_dict(target_expiry_input):
    bhav_map = {}
    if not os.path.exists("bhavcopy.csv"):
        return bhav_map

    target_dt = None
    date_formats = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y", "%d%b%y")
    
    if isinstance(target_expiry_input, datetime.date) and not isinstance(target_expiry_input, datetime.datetime):
        target_dt = target_expiry_input
    else:
        clean_target = str(target_expiry_input).strip()
        for fmt in date_formats:
            try:
                target_dt = datetime.datetime.strptime(clean_target, fmt).date()
                break
            except ValueError:
                continue

    if not target_dt:
        return bhav_map

    try:
        with open("bhavcopy.csv", mode="r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cleaned_row = {k.strip().upper(): (v.strip() if v else "") for k, v in row.items() if k}
                symbol = cleaned_row.get("TCKRSYMB") or cleaned_row.get("SYMBOL") or ""
                if "NIFTY" not in symbol.upper():
                    continue

                strike_raw = cleaned_row.get("STRKPRIC") or cleaned_row.get("STRIKE_PR") or cleaned_row.get("STRIKE") or "0"
                try:
                    row_strike = int(round(float(strike_raw)))
                except ValueError:
                    continue

                opt_type_raw = cleaned_row.get("OPTNTP") or cleaned_row.get("OPTION_TYP") or ""
                opt_type = "CE" if "CE" in opt_type_raw.upper() else "PE" if "PE" in opt_type_raw.upper() else ""

                if not opt_type:
                    continue

                expiry_raw = (cleaned_row.get("XPRYDT") or cleaned_row.get("EXPIRY_DT") or "").strip()
                row_dt = None
                for fmt in date_formats:
                    try:
                        row_dt = datetime.datetime.strptime(expiry_raw, fmt).date()
                        break
                    except ValueError:
                        continue
                
                # STRICT MATCH: Ensure row expiry matches target expiry exactly
                if row_dt and row_dt == target_dt:
                    open_p = float(cleaned_row.get("OPENPRIC") or cleaned_row.get("OPEN") or 0.0)
                    high = float(cleaned_row.get("HGHPRIC") or cleaned_row.get("HIGH") or 0.0)
                    low = float(cleaned_row.get("LWPRIC") or cleaned_row.get("LOW") or 0.0)
                    close = float(cleaned_row.get("CLSPRIC") or cleaned_row.get("CLOSE") or 0.0)
                    chg_oi = float(cleaned_row.get("CHGINOI") or cleaned_row.get("CHG_IN_OI") or 0.0)
                    iv = float(cleaned_row.get("IV") or cleaned_row.get("IMPLIED_VOL") or 0.0)

                    if close > 0:
                        bhav_map[(row_strike, opt_type)] = {
                            "open": open_p, 
                            "high": high if high > 0 else close, 
                            "low": low if low > 0 else close, 
                            "close": close, 
                            "chg_oi": chg_oi, 
                            "iv": iv
                        }
    except Exception as e:
        print(f"Error reading bhavcopy into dict: {e}")

    return bhav_map

def calculate_dominance_metrics(data_dict):
    if not data_dict:
        return {
            "high": 0.0, "close": 0.0, "low": 0.0, "iv": 0.0,
            "hc": 0.0, "cl": 0.0,
            "dominance": "NEUTRAL",
            "themeColor": "orange",
            "tagClass": "tag-neutral"
        }
    
    close = data_dict.get("close", 0.0)
    high = data_dict.get("high", 0.0)
    if high <= 0:
        high = close
        
    low = data_dict.get("low", 0.0)
    if low <= 0:
        low = close
        
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
                    exp_clean = exp.strip()
                    dt_parsed = None
                    date_formats = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y", "%d%b%y")
                    for fmt in date_formats:
                        try:
                            dt_parsed = datetime.datetime.strptime(exp_clean, fmt).date()
                            break
                        except ValueError:
                            continue
                    if dt_parsed:
                        all_expiries_dt.add((dt_parsed, exp_clean))

    market_closed_today = (now_ist.hour > 15) or (now_ist.hour == 15 and now_ist.minute >= 30)
    today_date_obj = now_ist.date()

    valid_tuples = []
    for item in all_expiries_dt:
        exp_date = item[0]
        if exp_date > today_date_obj:
            valid_tuples.append(item)
        elif exp_date == today_date_obj and not market_closed_today:
            valid_tuples.append(item)

    if not valid_tuples:
        valid_tuples = list(all_expiries_dt)

    sorted_expiries_tuples = sorted(valid_tuples, key=lambda x: x[0])
    
    if sorted_expiries_tuples:
        w_exp_dt, w_exp_str = sorted_expiries_tuples[0]
        w_exp = w_exp_dt
        
        target_month = w_exp_dt.month
        target_year = w_exp_dt.year
        
        same_month_expiries = [item for item in sorted_expiries_tuples if item[0].month == target_month and item[0].year == target_year]
        m_exp = same_month_expiries[-1][0] if same_month_expiries else w_exp
    else:
        w_exp = today_date_obj
        m_exp = w_exp

    w_bhav = load_bhavcopy_dict(w_exp)
    m_bhav = load_bhavcopy_dict(m_exp)

    if not w_bhav:
        print(
            f"⚠️ No NIFTY option Bhavcopy rows found for active expiry {w_exp}. "
            "Publishing a DATA_UNAVAILABLE payload instead of zero-filled signals."
        )
        write_error_payload(
            f"No NIFTY option Bhavcopy data found for active expiry {w_exp}."
        )
        return

    # --- UPDATED HLC ATM STRIKE LOGIC: Finds smallest CE-PE Difference across available strikes ---
    available_strikes = sorted(list(set(s for s, t in w_bhav.keys())))
    min_diff = float('inf')
    hlc_atm_strike = int(round(spot / 50.0) * 50) if spot > 0 else 23150

    if available_strikes:
        for strike in available_strikes:
            ce_close = w_bhav.get((strike, "CE"), {}).get("close", 0.0)
            pe_close = w_bhav.get((strike, "PE"), {}).get("close", 0.0)
            if ce_close > 0 and pe_close > 0:
                diff = abs(ce_close - pe_close)
                if diff < min_diff:
                    min_diff = diff
                    hlc_atm_strike = strike
    else:
        true_atm = int(round(spot / 50.0) * 50) if spot > 0 else 23150
        search_candidates = [true_atm - 150, true_atm - 100, true_atm - 50, true_atm, true_atm + 50, true_atm + 100, true_atm + 150]
        for strike in search_candidates:
            ce_close = w_bhav.get((strike, "CE"), {}).get("close", 0.0)
            pe_close = w_bhav.get((strike, "PE"), {}).get("close", 0.0)
            if ce_close > 0 and pe_close > 0:
                diff = abs(ce_close - pe_close)
                if diff < min_diff:
                    min_diff = diff
                    hlc_atm_strike = strike

    print(f"🎯 Selected HLC ATM Strike: {hlc_atm_strike} (Min CE-PE Difference: {min_diff:.2f})")

    iv_atm_strike = get_iv_atm_strike(spot)

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

    iv_bhav_ce = w_bhav.get((int(iv_atm_strike), "CE"), {}).get("iv", 0.0)
    iv_bhav_pe = w_bhav.get((int(iv_atm_strike), "PE"), {}).get("iv", 0.0)

    if iv_bhav_ce > 0:
        ce_metrics["iv"] = round(iv_bhav_ce, 2)
    if iv_bhav_pe > 0:
        pe_metrics["iv"] = round(iv_bhav_pe, 2)

    if ce_metrics["iv"] == 0.0:
        ce_price = ce_dict.get("close", 0.0)
        ce_metrics["iv"] = get_live_iv_from_nse(iv_atm_strike, w_exp, "CE", ce_price, spot)

    if pe_metrics["iv"] == 0.0:
        pe_price = pe_dict.get("close", 0.0)
        pe_metrics["iv"] = get_live_iv_from_nse(iv_atm_strike, w_exp, "PE", pe_price, spot)

    asymmetric_tv_data = calculate_asymmetric_time_value(spot, w_exp, w_bhav)

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

    daily_candles = fetch_daily_candles_for_sellers_area()
    dynamic_sellers_highs = get_valid_highs(daily_candles, max_count=5)

    adjusted_sellers_highs = []
    for h in dynamic_sellers_highs:
        if h <= max_supply_val:
            adjusted_sellers_highs.append(round(max_supply_val + (max_supply_val - h) + 25.0, 2))
        else:
            adjusted_sellers_highs.append(h)
    dynamic_sellers_highs = adjusted_sellers_highs

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
        "timestampIST": now_ist.isoformat(),
        "expiryDate": w_exp.strftime("%Y-%m-%d"),
        "spotPrice": spot,
        "hlcAtmStrike": hlc_atm_strike,
        "ivAtmStrike": iv_atm_strike,
        "atmDefinitions": {
            "hlc": "Strike with the smallest absolute CE-close minus PE-close difference in the active weekly expiry.",
            "iv": "Nearest 50-point strike to NIFTY spot.",
            "sniper1": "Nearest 100-point strike to NIFTY spot.",
            "sniper2": "Same strike as HLC ATM."
        },
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

def write_error_payload(message):
    """Writes a truthful error state instead of publishing stale/hard-coded prices."""
    now_ist = datetime.datetime.now(IST)
    payload = {
        "dataStatus": "ERROR",
        "bhavcopyReady": False,
        "currentDate": get_display_date(now_ist),
        "timestampIST": now_ist.isoformat(),
        "error": str(message),
        "spotPrice": None,
        "spotHigh": None,
        "spotLow": None,
    }
    try:
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
        print(f"❌ Error payload saved: {message}")
    except Exception as e:
        print(f"❌ Could not save error payload: {e}")


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
        if spot is None:
            write_error_payload("NIFTY spot data unavailable on weekend/holiday.")
            exit(1)
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
    if spot is not None:
        print(
            f"Retrieved NIFTY spot from Yahoo Finance -> "
            f"Close: {spot}, High: {spot_high}, Low: {spot_low}"
        )
        process_and_save_data(
            spot, spot_high, spot_low, force_not_ready=False
        )
    else:
        write_error_payload("NIFTY spot data unavailable. No fake fallback price was used.")
        exit(1)
