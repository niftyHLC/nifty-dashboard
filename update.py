import csv, datetime as dt, io, json, math, os, subprocess, time, zipfile
import holidays, numpy as np, requests, yfinance as yf
from scipy.optimize import brentq
from scipy.stats import norm

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/"
}
DATE_FORMATS = (
    "%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y",
    "%d-%m-%Y", "%d%b%Y", "%d%b%y"
)

def now():
    return dt.datetime.now(IST)

def parse_date(x):
    if isinstance(x, dt.datetime): return x.date()
    if isinstance(x, dt.date): return x
    s = str(x or "").strip()
    for f in DATE_FORMATS:
        try: return dt.datetime.strptime(s, f).date()
        except ValueError: pass
    try: return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception: return None

def market_holidays(year=None):
    return set(holidays.India(years=year or now().year).keys())

def is_market_day(d):
    return d.weekday() < 5 and d not in market_holidays(d.year)

def previous_market_day(d):
    d -= dt.timedelta(days=1)
    while not is_market_day(d): d -= dt.timedelta(days=1)
    return d

def next_market_day(d):
    while not is_market_day(d): d += dt.timedelta(days=1)
    return d

def display_date(t):
    d = t.date()
    if t.hour > 15 or (t.hour == 15 and t.minute >= 30):
        d += dt.timedelta(days=1)
    return next_market_day(d).strftime("%d %b %Y").upper()

def fetch_spot():
    try:
        d = yf.Ticker("^NSEI").history(period="1d")
        if not d.empty:
            c = float(d.Close.iloc[-1])
            h = float(d.High.iloc[-1])
            l = float(d.Low.iloc[-1])
            if c > 0: return c, h, l
    except Exception as e:
        print("Spot error:", e)
    return None

def nse_data(symbol="NIFTY", expiry=None):
    """Fetch NIFTY option chain from Upstox using the read-only Analytics Token."""
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "").strip()
    if not token:
        print("UPSTOX ERROR: UPSTOX_ACCESS_TOKEN is not available in the environment.")
        return None

    expiry_date = parse_date(expiry)
    if not expiry_date:
        print("UPSTOX ERROR: valid expiry date is required.")
        return None

    url = "https://api.upstox.com/v2/option/chain"
    params = {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry_date": expiry_date.strftime("%Y-%m-%d"),
    }
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        print(
            f"Upstox Option Chain HTTP: {r.status_code}, "
            f"bytes={len(r.content)}, "
            f"content-type={r.headers.get('content-type')}"
        )
        if r.status_code != 200:
            print("Upstox response:", r.text[:500].replace("\n", " "))
            return None

        payload = r.json()
        rows = payload.get("data", [])
        print(f"Upstox Option Chain records: {len(rows)}")
        if not rows:
            return None

        # Normalize Upstox into the same records/data shape used by the rest
        # of this script, so existing IV and Time Value logic stays unchanged.
        normalized = []
        for x in rows:
            try:
                strike = float(x.get("strike_price", 0) or 0)
                exp = parse_date(x.get("expiry"))
                if strike <= 0 or exp != expiry_date:
                    continue

                row = {
                    "strikePrice": strike,
                    "CE": {},
                    "PE": {},
                }

                co = x.get("call_options") or {}
                cm = co.get("market_data") or {}
                cg = co.get("option_greeks") or {}
                row["CE"] = {
                    "expiryDate": expiry_date.strftime("%Y-%m-%d"),
                    "lastPrice": float(cm.get("ltp", 0) or 0),
                    "impliedVolatility": float(cg.get("iv", cm.get("iv", 0)) or 0),
                }

                po = x.get("put_options") or {}
                pm = po.get("market_data") or {}
                pg = po.get("option_greeks") or {}
                row["PE"] = {
                    "expiryDate": expiry_date.strftime("%Y-%m-%d"),
                    "lastPrice": float(pm.get("ltp", 0) or 0),
                    "impliedVolatility": float(pg.get("iv", pm.get("iv", 0)) or 0),
                }

                normalized.append(row)
            except Exception:
                continue

        print(f"Upstox normalized records: {len(normalized)}")
        return {"records": {"data": normalized}} if normalized else None

    except Exception as e:
        print("Upstox option-chain error:", repr(e))
        return None


def atm50(x):
    """
    Select the smaller 50-point strike at the NSE option-chain ITM boundary.
    Example: underlying between 23150 and 23200 -> 23150.
    """
    return int(math.floor(float(x) / 50.0) * 50)
def iv_tv_reference_strike(spot, data=None):
    """Nearest listed 50-point ATM strike; choose smaller on an exact tie.

    NSE shading indicates the ATM/ITM/OTM boundary, not an API color field.
    Only strikes actually returned by Upstox for the selected expiry qualify.
    """
    rows = (data or {}).get("records", {}).get("data", [])
    available = set()
    for row in rows:
        try:
            strike = float(row.get("strikePrice", 0))
            if strike > 0 and strike % 50 == 0:
                available.add(int(strike))
        except (ValueError, TypeError):
            continue
    if not available:
        return None
    return min(available, key=lambda strike: (abs(strike - float(spot)), strike))



def nse_boundary_selection(spot, data, trade_day):
    """Return (selected_strike, verification) without claiming unobserved NSE colors.

    Optional trusted NSE observation:
      NSE_PUT_FIRST_ITM_STRIKE = first shaded PUT-side strike (e.g. 22700)
      NSE_BOUNDARY_TRADE_DATE = YYYY-MM-DD, the trading date of that observation.
    Both are required. Without them, retain an explicitly UNVERIFIED approximation.
    """
    rows = (data or {}).get("records", {}).get("data", [])
    strikes = sorted({
        int(float(row["strikePrice"]))
        for row in rows
        if float(row.get("strikePrice", 0) or 0) > 0
        and float(row["strikePrice"]) % 50 == 0
    })
    if not strikes:
        return None, {"status": "UNAVAILABLE", "method": "NO_LISTED_STRIKES"}

    observed = os.environ.get("NSE_PUT_FIRST_ITM_STRIKE", "").strip()
    observed_day = os.environ.get("NSE_BOUNDARY_TRADE_DATE", "").strip()
    expected_day = trade_day.strftime("%Y-%m-%d")

    if observed or observed_day:
        if not observed or observed_day != expected_day:
            return None, {
                "status": "INVALID_OBSERVATION",
                "reason": "Both NSE variables are required and the observation date must match the trade date."
            }
        try:
            first_shaded = int(float(observed))
            index = strikes.index(first_shaded)
            if index == 0:
                raise ValueError("No preceding strike available")
            selected = strikes[index - 1]
            if first_shaded - selected != 50:
                raise ValueError("Boundary strikes are not 50 points apart")
        except (ValueError, IndexError) as exc:
            return None, {"status": "INVALID_OBSERVATION", "reason": str(exc)}
        return selected, {
            "status": "VERIFIED_FROM_PROVIDED_NSE_BOUNDARY",
            "method": "PRECEDING_ROW_OF_NSE_PUT_FIRST_ITM",
            "firstShadedPutStrike": first_shaded,
            "observationTradeDate": expected_day,
            "source": "USER_SUPPLIED_NSE_BOUNDARY"
        }

    selected = iv_tv_reference_strike(spot, data=data)
    return selected, {
        "status": "UNVERIFIED",
        "method": "NEAREST_AVAILABLE_UPSTOX_STRIKE",
        "reason": "No dated NSE boundary observation was supplied; visual match cannot be guaranteed."
    }

def atm100(x): return int(round(float(x) / 100) * 100)

def bs_iv(kind, price, spot, strike, expiry):
    if min(price, spot, strike) <= 0: return 0.0
    try:
        ed = parse_date(expiry)
        if not ed: return 0.0
        T = max((ed - now().date()).days, 1) / 365
        r = .065
        intrinsic = max(0, spot-strike) if kind == "CE" else max(0, strike-spot)
        if price < intrinsic: return 0.0
        def f(s):
            d1 = (np.log(spot/strike)+(r+.5*s*s)*T)/(s*np.sqrt(T))
            d2 = d1-s*np.sqrt(T)
            p = (spot*norm.cdf(d1)-strike*np.exp(-r*T)*norm.cdf(d2)
                 if kind == "CE"
                 else strike*np.exp(-r*T)*norm.cdf(-d2)-spot*norm.cdf(-d1))
            return p-price
        return round(float(brentq(f, 1e-9, 5, maxiter=100))*100, 2)
    except Exception:
        return 0.0

def live_iv(strike, expiry, kind, price=0, spot=0, data=None):
    """Return Upstox Option Chain IV for the exact strike + expiry + CE/PE."""
    data = data or nse_data(expiry=expiry)
    if not data:
        return 0.0
    target = parse_date(expiry)
    for x in data.get("records", {}).get("data", []):
        try:
            if float(x.get("strikePrice", 0)) != float(strike):
                continue
            o = x.get(kind, {})
            if parse_date(o.get("expiryDate")) != target:
                continue
            iv = float(o.get("impliedVolatility", 0) or 0)
            if iv > 0:
                return round(iv, 2)
        except Exception:
            continue
    return 0.0

def time_value(spot, expiry, bhav=None, data=None, reference_strike=None):
    """TV = (common IV/TV strike + 50 CE LTP) + (strike - 50 PE LTP)."""
    atm = iv_tv_reference_strike(spot, data=data) if reference_strike is None else int(reference_strike)
    if atm is None:
        return {"total": 0.0, "ceStrike": None, "ceLtp": 0.0, "peStrike": None, "peLtp": 0.0}
    ce_strike, pe_strike = atm + 50, atm - 50
    ce_ltp = pe_ltp = 0.0
    data = data or nse_data(expiry=expiry)

    if data:
        target = parse_date(expiry)
        for x in data.get("records", {}).get("data", []):
            try:
                strike = float(x.get("strikePrice", 0))
                if strike == float(ce_strike):
                    ce = x.get("CE", {})
                    if parse_date(ce.get("expiryDate")) == target:
                        ce_ltp = float(ce.get("lastPrice", 0) or 0)
                if strike == float(pe_strike):
                    pe = x.get("PE", {})
                    if parse_date(pe.get("expiryDate")) == target:
                        pe_ltp = float(pe.get("lastPrice", 0) or 0)
            except Exception:
                continue

    return {
        "total": round(ce_ltp + pe_ltp, 2),
        "ceStrike": ce_strike, "ceLtp": round(ce_ltp, 2),
        "peStrike": pe_strike, "peLtp": round(pe_ltp, 2)
    }

def valid_highs(candles, max_count=5):
    """
    H1 = latest available NIFTY daily candle high (previous trading-day high
    when the dashboard is viewed before the next completed daily candle).

    H2-H5 = older bearish daily-candle highs that have NOT been exceeded by
    any later candle. Returned newest-to-oldest.
    """
    if not candles or max_count <= 0:
        return []

    # H1 must always be the latest completed/available daily high.
    out = [round(float(candles[-1]["high"]), 2)]

    # H2-H5: older valid unbroken bearish highs.
    for i in range(len(candles) - 2, -1, -1):
        c = candles[i]

        # Older level must come from a bearish candle.
        if c["close"] >= c["open"]:
            continue

        # Broken if any later candle traded ABOVE this high.
        if any(x["high"] > c["high"] for x in candles[i + 1:]):
            continue

        h = round(float(c["high"]), 2)
        if h not in out:
            out.append(h)

        if len(out) >= max_count:
            break

    return sorted(out)[:max_count]

def daily_candles():
    try:
        d = yf.Ticker("^NSEI").history(period="6mo", interval="1d")
        return [
            {"open": float(r.Open), "high": float(r.High),
             "low": float(r.Low), "close": float(r.Close)}
            for _, r in d.iterrows()
        ] if not d.empty else []
    except Exception as e:
        print("Daily data error:", e)
        return []

def download_bhavcopy(trade_date=None):
    """
    Make one fast attempt to get the requested NSE F&O Bhavcopy.

    Primary source:
      https://nsearchives.nseindia.com/content/trdops/FNO_BCDDMMYYYY.DAT

    The DAT file is converted to our existing normalized CSV layout so the
    rest of the dashboard calculations do not need to change.
    """
    d = trade_date or previous_market_day(now().date())
    ymd = d.strftime("%Y%m%d")
    ddmmyyyy = d.strftime("%d%m%Y")

    # NSE's currently published F&O Bhavcopy DAT is the primary source.
    dat_url = (
        "https://nsearchives.nseindia.com/content/trdops/"
        f"FNO_BC{ddmmyyyy}.DAT"
    )

    # Keep the older CSV ZIP locations only as fallbacks.
    zip_urls = [
        f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
        f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d:%Y}/{d:%b}/fo{ddmmyyyy}bhav.csv.zip",
    ]

    def validate_and_save(text):
        if len(text.strip()) < 100:
            print("Invalid Bhavcopy: file is empty.")
            return False

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return False

        # DAT files use NSE's positional F&O layout. Convert the fields needed
        # by the existing parser into a small normalized CSV.
        first = [str(x).strip().upper() for x in rows[0]]
        looks_headered = any(
            x in first for x in ("TCKRSYMB", "SYMBOL", "TICKERSYMBOL")
        )

        normalized = []
        if looks_headered:
            dr = csv.DictReader(io.StringIO(text))
            for raw_row in dr:
                row = _norm_row(raw_row)
                symbol = _field(row, "TCKRSYMB", "SYMBOL", "TICKERSYMBOL").upper()
                opt = _field(row, "OPTNTP", "OPTION_TYP", "OPTIONTYPE").upper()
                strike = _num(row, "STRKPRIC", "STRIKE_PR", "STRIKE")
                close = _num(row, "CLSPRIC", "CLOSE", "CLOSE_PRICE")
                if symbol != "NIFTY" or opt not in ("CE", "PE") or strike <= 0 or close <= 0:
                    continue
                normalized.append({
                    "TCKRSYMB": symbol,
                    "XPRYDT": _field(row, "XPRYDT", "EXPIRY_DT", "EXPIRY"),
                    "STRKPRIC": strike,
                    "OPTNTP": opt,
                    "OPNPRIC": _num(row, "OPNPRIC", "OPENPRIC", "OPEN"),
                    "HGHPRIC": _num(row, "HGHPRIC", "HIGH", "HIGH_PRICE"),
                    "LWPRIC": _num(row, "LWPRIC", "LOW", "LOW_PRICE"),
                    "CLSPRIC": close,
                    "CHNGINOPNINTRST": _num(row, "CHNGINOPNINTRST", "CHGINOI", "CHG_IN_OI"),
                })
        else:
            # FNO_BC*.DAT positional layout:
            # instrument, symbol, expiry, strike, option type, open, high,
            # low, close, ...  (remaining columns are not required here)
            for cols in rows:
                cols = [str(x).strip().strip('"') for x in cols]
                if len(cols) < 9:
                    continue
                instrument = cols[1].upper()
                symbol = cols[2].upper()
                opt = cols[5].upper()
                if symbol != "NIFTY" or opt not in ("CE", "PE"):
                    continue
                if instrument and instrument not in ("OPTIDX", "OPTSTK"):
                    continue
                try:
                    strike = float(cols[4].replace(",", ""))
                    opn = float(cols[10].replace(",", "") or 0)
                    high = float(cols[11].replace(",", "") or 0)
                    low = float(cols[12].replace(",", "") or 0)
                    close = float(cols[13].replace(",", "") or 0)
                except (ValueError, IndexError):
                    continue
                expiry = dt.datetime.strptime(cols[3], "%d%m%Y").date()
                if strike <= 0 or close <= 0 or not expiry:
                    continue
                normalized.append({
                    "TCKRSYMB": symbol,
                    "XPRYDT": expiry.strftime("%d-%b-%Y").upper(),
                    "STRKPRIC": strike,
                    "OPTNTP": opt,
                    "OPNPRIC": opn,
                    "HGHPRIC": high,
                    "LWPRIC": low,
                    "CLSPRIC": close,
                    "CHNGINOPNINTRST": float(cols[18].replace(",", "") or 0) if len(cols) > 18 else 0,
                })

        ce_rows = sum(1 for r in normalized if r["OPTNTP"] == "CE")
        pe_rows = sum(1 for r in normalized if r["OPTNTP"] == "PE")
        if not normalized or ce_rows == 0 or pe_rows == 0:
            print(
                "Downloaded file failed validation: "
                f"NIFTY={len(normalized)}, CE={ce_rows}, PE={pe_rows}"
            )
            return False

        fields = [
            "TCKRSYMB", "XPRYDT", "STRKPRIC", "OPTNTP",
            "OPNPRIC", "HGHPRIC", "LWPRIC", "CLSPRIC",
            "CHNGINOPNINTRST",
        ]
        with open("bhavcopy.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(normalized)

        print(
            f"BHAVCOPY READY: {len(normalized)} valid NIFTY option rows "
            f"(CE={ce_rows}, PE={pe_rows})"
        )
        return True

    # 1) Current NSE DAT source.
    try:
        print(f"Checking NSE DAT Bhavcopy for {d}:", dat_url)
        r = requests.get(dat_url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.content) >= 1000:
            dat_text = r.content.decode("utf-8-sig", errors="ignore")
            if validate_and_save(dat_text):
                return True
        else:
            print(f"DAT not ready: HTTP {r.status_code}, bytes={len(r.content)}")
    except Exception as e:
        print("DAT Bhavcopy check error:", e)

    # 2) Older ZIP sources as fallbacks.
    for url in zip_urls:
        try:
            print(f"Checking legacy NSE Bhavcopy for {d}:", url)
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200 or len(r.content) < 1000:
                print(f"Not ready: HTTP {r.status_code}, bytes={len(r.content)}")
                continue

            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                names = [x for x in z.namelist() if not x.endswith("/")]
                if not names:
                    continue
                raw = z.read(names[0])

            zip_text = raw.decode("utf-8-sig", errors="ignore")
            if validate_and_save(zip_text):
                return True

        except zipfile.BadZipFile:
            print("Downloaded response is not a valid ZIP yet.")
        except Exception as e:
            print("Legacy Bhavcopy check error:", e)

    print("Bhavcopy is not ready yet. This workflow will retry.")
    return False


def _norm_row(r):
    return {
        str(k).strip().upper().replace("\ufeff", ""): (v.strip() if v is not None else "")
        for k, v in r.items() if k
    }

def _num(r, *names):
    for n in names:
        v = r.get(n)
        if v not in (None, ""):
            try: return float(str(v).replace(",", ""))
            except ValueError: pass
    return 0.0

def _field(r, *names):
    for n in names:
        if r.get(n) not in (None, ""): return r[n]
    return ""

def parse_bhav_file(path="bhavcopy.csv"):
    out, expiries, option_rows = {}, set(), 0
    if not os.path.exists(path): return out, expiries, option_rows
    try:
        with open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
            for raw in csv.DictReader(f):
                r = _norm_row(raw)
                symbol = _field(r, "TCKRSYMB", "SYMBOL", "TICKERSYMBOL").upper()
                if symbol != "NIFTY": continue

                expiry = parse_date(_field(r, "XPRYDT", "EXPIRY_DT", "EXPIRY"))
                if not expiry: continue
                expiries.add(expiry)

                opt = _field(r, "OPTNTP", "OPTION_TYP", "OPTIONTYPE").upper()
                if opt not in ("CE", "PE"): continue

                strike = _num(r, "STRKPRIC", "STRIKE_PR", "STRIKE")
                close = _num(r, "CLSPRIC", "CLOSE", "CLOSE_PRICE")
                if strike <= 0 or close <= 0: continue

                option_rows += 1
                s_int = int(round(strike))
                out[(s_int, expiry, opt)] = {
                    "open": _num(r, "OPNPRIC", "OPENPRIC", "OPEN"),
                    "high": _num(r, "HGHPric", "HGHPRIC", "HIGH", "HIGH_PRICE") or close,
                    "low": _num(r, "LWPRIC", "LOW", "LOW_PRICE") or close,
                    "close": close,
                    "chg_oi": _num(r, "CHNGINOPNINTRST", "CHGINOI", "CHG_IN_OI"),
                    "iv": _num(r, "IV", "IMPLIED_VOL")
                }
    except Exception as e:
        print("Bhavcopy parse error:", e)
    return out, expiries, option_rows

def load_bhav(expiry, parsed_data=None):
    target = parse_date(expiry)
    if not target: return {}
    
    # Optimized: If dictionary data is already in memory, slice it directly avoiding file re-reads
    if parsed_data:
        return {(s, k): v for (s, exp, k), v in parsed_data.items() if exp == target}

    # Fallback to file parsing if memory dictionary isn't passed
    full_data, _, _ = parse_bhav_file()
    return {(s, k): v for (s, exp, k), v in full_data.items() if exp == target}

def dominance(d):
    d = d or {}
    c = d.get("close", 0); h = d.get("high", 0) or c; l = d.get("low", 0) or c
    hc, cl = round(h-c, 2), round(c-l, 2)

    # If H-C OR C-L is 35.00 points or more, force NEUTRAL.
    if hc >= 35 or cl >= 35:
        dom = "NEUTRAL"
    elif cl >= 1.5*hc and h != l:
        dom = "BUYERS"
    elif hc >= 1.5*cl and h != l:
        dom = "SELLERS"
    else:
        dom = "NEUTRAL"

    color, tag = {"BUYERS":("green","tag-buyers"), "SELLERS":("red","tag-sellers"),
                  "NEUTRAL":("orange","tag-neutral")}[dom]
    return {
        "high": round(h,2), "close": round(c,2), "low": round(l,2),
        "iv": round(d.get("iv",0),2), "hc": hc, "cl": cl,
        "dominance": dom, "themeColor": color, "tagClass": tag
    }

def zone(wl, wh, b):
    p = lambda s,k: b.get((s,k),{}).get("close",0)
    return {"line1": round(wl+p(wl,"CE")+p(wl,"PE"),2),
            "line2": round(wh-p(wh,"CE")-p(wh,"PE"),2)}

def error_payload(message, spot=None):
    payload = {
        "dataStatus": "ERROR",
        "bhavcopyReady": False,
        "error": message,
        "currentDate": display_date(now())
    }
    if spot: payload["spotPrice"] = spot
    with open("data.json","w",encoding="utf-8") as f:
        json.dump(payload,f,indent=4)
    print("ERROR:", message)
    return False

def mark_waiting(message, spot=None, trade_day=None):
    """
    Publish one WAITING state while preserving the last good dashboard values.
    This shows the warning without replacing previous values with zeros.
    """
    payload = {}
    if os.path.exists("data.json"):
        try:
            with open("data.json", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                payload = old
        except Exception:
            payload = {}

    trade_key = (
        trade_day.strftime("%Y-%m-%d")
        if isinstance(trade_day, dt.date)
        else str(trade_day or "")
    )

    already_waiting = (
        payload.get("dataStatus") == "WAITING"
        and payload.get("waitingTradeDate") == trade_key
    )

    payload["dataStatus"] = "WAITING"
    payload["bhavcopyReady"] = False
    payload["waitingMessage"] = message
    payload["waitingTradeDate"] = trade_key

    if spot and not payload.get("spotPrice"):
        payload["spotPrice"] = spot

    if not already_waiting:
        payload["waitingSince"] = now().isoformat()
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
        print("WAITING STATUS PUBLISHED:", message)
        push()
    else:
        print("WAITING STATUS ALREADY PUBLISHED:", message)

    return None

def push():
    try:
        subprocess.run(["git","config","--global","user.name","github-actions[bot]"],check=True)
        subprocess.run(["git","config","--global","user.email","github-actions[bot]@users.noreply.github.com"],check=True)
        subprocess.run(["git","add","-f","data.json"],check=False)
        if os.path.exists("bhavcopy.csv"): subprocess.run(["git","add","-f","bhavcopy.csv"],check=False)
        if subprocess.run(["git","diff","--cached","--quiet"],capture_output=True).returncode:
            subprocess.run(["git","commit","-m","Auto-update dashboard [skip ci]"],check=True)
            subprocess.run(["git","push","origin","main"],check=True)
    except Exception as e:
        print("Git push failed:",e)

def process(spot, hi, lo):
    t = now()
    trade_day = t.date() if is_market_day(t.date()) and (t.hour > 15 or (t.hour == 15 and t.minute >= 30)) else previous_market_day(t.date())
    ready = download_bhavcopy(trade_day)
    if not ready:
        message = "Data Not Ready Yet - NSE Bhavcopy is not available/valid yet."
        print("WAITING:", message)
        return mark_waiting(message, spot, trade_day)

    full_bhav, expiries, option_rows = parse_bhav_file()
    if option_rows == 0:
        return error_payload("Bhavcopy downloaded, but no NIFTY CE/PE option rows were parsed.", spot)

    today = t.date()
    closed = t.hour > 15 or (t.hour == 15 and t.minute >= 30)
    valid = sorted(x for x in expiries if x > today or (x == today and not closed))
    if not valid:
        valid = sorted(x for x in expiries if x >= trade_day)
    if not valid:
        return error_payload("No valid future NIFTY option expiry found in Bhavcopy.", spot)

    wexp = valid[0]
    same = [x for x in valid if x.month == wexp.month and x.year == wexp.year]
    mexp = same[-1] if same else wexp

    wb, mb = load_bhav(wexp, full_bhav), load_bhav(mexp, full_bhav)
    if not wb:
        return error_payload(f"No NIFTY option data found for expiry {wexp}.", spot)

    strikes = sorted({s for s,k in wb})
    hlc, mindiff = atm50(spot), float("inf")
    for s in strikes:
        ce0, pe0 = wb.get((s,"CE"),{}).get("close",0), wb.get((s,"PE"),{}).get("close",0)
        if ce0 > 0 and pe0 > 0 and abs(ce0-pe0) < mindiff:
            mindiff, hlc = abs(ce0-pe0), s

    s1, s2 = atm100(spot), hlc
    ce, pe = dominance(wb.get((hlc,"CE"))), dominance(wb.get((hlc,"PE")))

    # Fetch Upstox Option Chain once.
    # It is mandatory because IV and Time Value must come directly from NSE.
    option_chain = nse_data(expiry=wexp)
    if not option_chain:
        message = "Data Not Ready Yet - Upstox Option Chain is unavailable."
        print("WAITING:", message, "Will retry in 5 minutes.")
        return mark_waiting(message, spot, trade_day)

    ivatm, boundary_verification = nse_boundary_selection(spot, option_chain, trade_day)
    if ivatm is None:
        message = "Data Not Ready Yet - IV/TV boundary unavailable: " + str(boundary_verification)
        print("WAITING:", message)
        return mark_waiting(message, spot, trade_day)
    print(f"IV/TV reference strike: {ivatm}; NSE boundary verification: {boundary_verification}")

    # IV: use the common IV/TV reference strike and weekly expiry.
    ce["iv"] = live_iv(ivatm, wexp, "CE", data=option_chain)
    pe["iv"] = live_iv(ivatm, wexp, "PE", data=option_chain)

    # TV: (IV ATM + 50 CE LTP) + (IV ATM - 50 PE LTP), Upstox Option Chain.
    tv = time_value(spot, wexp, data=option_chain, reference_strike=ivatm)

    # Never publish a SUCCESS payload with missing/zero NSE IV values.
    if ce["iv"] <= 0 or pe["iv"] <= 0:
        message = (
            f"Data Not Ready Yet - Upstox IV incomplete at strike {ivatm}. "
            f"CE IV={ce['iv']}, PE IV={pe['iv']}."
        )
        print("WAITING:", message, "Will retry in 5 minutes.")
        return mark_waiting(message, spot, trade_day)

    # Never publish a SUCCESS payload with missing/zero NSE TV leg prices.
    if tv["ceLtp"] <= 0 or tv["peLtp"] <= 0:
        message = (
            "Data Not Ready Yet - Upstox Time Value prices incomplete. "
            f"CE {tv['ceStrike']} LTP={tv['ceLtp']}, "
            f"PE {tv['peStrike']} LTP={tv['peLtp']}."
        )
        print("WAITING:", message, "Will retry in 5 minutes.")
        return mark_waiting(message, spot, trade_day)
    get = lambda s,k: wb.get((s,k),{}).get("close",0)
    s1v = round((get(s1+100,"CE")+get(s1-100,"PE"))/2,2)
    s2v = round((get(s2+100,"CE")+get(s2-100,"PE"))/2,2)

    mins = round(hlc+ce["close"],2)
    mind = round(hlc-pe["close"],2)
    maxs = round(hlc+ce["close"]+pe["close"],2)
    maxd = round(hlc-ce["close"]-pe["close"],2)

    # Keep only valid historical highs ABOVE Maximum Supply.
    # Then display the five nearest levels above Maximum Supply, smallest first.
    highs = [h for h in valid_highs(daily_candles(), max_count=100) if h > maxs]
    highs = sorted(highs)[:5]
    wl, wh = math.floor(spot/100)*100, math.ceil(spot/100)*100
    diff = round(hi-lo,2)

    payload = {
        "dataStatus":"SUCCESS", "bhavcopyReady":True,
        "currentDate":display_date(t), "expiryDate":wexp.strftime("%Y-%m-%d"),
        "spotPrice":spot, "hlcAtmStrike":hlc, "ivAtmStrike":ivatm,
        "ivTvBoundaryVerification":boundary_verification,
        "ce":ce, "pe":pe, "ceTag":ce["dominance"], "ceClass":ce["tagClass"],
        "peTag":pe["dominance"], "peClass":pe["tagClass"],
        "bannerTotal": round(abs(ce["close"] - pe["close"]), 2),
        "asymmetricTimeValue":tv,
        "minSupply":mins, "minDemand":mind, "maxSupply":maxs, "maxDemand":maxd,
        "sellersArea":{"min":mins,"max":maxs,"validUnbrokenHighs":highs},
        "buyersArea":{"min":mind,"max":maxd},
        "weeklyZones":zone(wl,wh,wb), "monthlyZones":zone(wl,wh,mb),
        "spotHigh":hi, "spotLow":lo, "spotDifference":diff,
        "earthLevel":round(diff*.2611,2),
        "sniper1":{
            "strike":s1,"ce":get(s1,"CE"),"pe":get(s1,"PE"),
            "otmCeStrike":s1+100,"otmPeStrike":s1-100,
            "otmCe":get(s1+100,"CE"),"otmPe":get(s1-100,"PE"),"value":s1v
        },
        "sniper2":None if s1==s2 else {
            "strike":s2,"ce":get(s2,"CE"),"pe":get(s2,"PE"),
            "otmCeStrike":s2+100,"otmPeStrike":s2-100,
            "otmCe":get(s2+100,"CE"),"otmPe":get(s2-100,"PE"),"value":s2v
        },
        "optionRowsParsed": option_rows,
        "bhavcopyTradeDate": trade_day.strftime("%Y-%m-%d")
    }

    with open("data.json","w",encoding="utf-8") as f:
        json.dump(payload,f,indent=4)
    print(f"SUCCESS: {option_rows} NIFTY option rows parsed; expiry={wexp}; HLC ATM={hlc}")
    push()
    return True

if __name__ == "__main__":
    t = now()
    spot = fetch_spot()
    if not spot:
        print("ERROR: NIFTY spot unavailable; no fallback price used.")
        raise SystemExit(1)

    if not is_market_day(t.date()):
        print("Non-market day. Nothing to update.")
        raise SystemExit(0)

    if os.path.exists("data.json"):
        try:
            with open("data.json",encoding="utf-8") as f:
                old = json.load(f)
            if old.get("currentDate") == display_date(t) and old.get("dataStatus") == "SUCCESS":
                print("Today's data already processed. Exiting successfully.")
                raise SystemExit(0)
        except SystemExit:
            raise
        except Exception:
            pass

    result = process(*spot)
    if result is None:
        # Exit 75 means: valid NSE Bhavcopy is not ready yet; workflow should retry.
        raise SystemExit(75)
    raise SystemExit(0 if result else 1)
