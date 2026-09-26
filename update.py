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

def nse_data(symbol="NIFTY"):
    try:
        s = requests.Session()
        s.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
        time.sleep(.4)
        r = s.get(
            f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}",
            headers=HEADERS, timeout=20
        )
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print("NSE option-chain error:", e)
        return None

def atm50(x): return int(round(float(x) / 50) * 50)
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

def live_iv(strike, expiry, kind, price, spot, data=None):
    if data is None:
        data = nse_data()
    if data:
        target = parse_date(expiry)
        for x in data.get("records", {}).get("data", []):
            try:
                if float(x.get("strikePrice", 0)) != float(strike): continue
                o = x.get(kind, {})
                if parse_date(o.get("expiryDate")) == target:
                    v = float(o.get("impliedVolatility", 0) or 0)
                    if v > 0: return v
            except Exception:
                continue
    return bs_iv(kind, price, spot, strike, expiry)

def time_value(spot, expiry, bhav, data=None):
    atm, sm = atm50(spot), {}
    if data is None:
        data = nse_data()
    if data:
        target = parse_date(expiry)
        for x in data.get("records", {}).get("data", []):
            try:
                s = float(x.get("strikePrice", 0))
                ce, pe = x.get("CE", {}), x.get("PE", {})
                if parse_date(ce.get("expiryDate") or pe.get("expiryDate")) == target:
                    sm[s] = {
                        "ce": float(ce.get("lastPrice", 0) or 0),
                        "pe": float(pe.get("lastPrice", 0) or 0)
                    }
            except Exception:
                pass
    if not sm:
        for (s, k), v in bhav.items():
                sm.setdefault(s, {"ce": 0, "pe": 0})[k.lower()] = v["close"]
    keys = sorted(sm)
    if len(keys) < 3:
        return {"total": 0, "ceStrike": 0, "ceLtp": 0, "peStrike": 0, "peLtp": 0}
    i = min(range(len(keys)), key=lambda j: abs(keys[j]-atm))
    if i == 0 or i == len(keys)-1:
        return {"total": 0, "ceStrike": 0, "ceLtp": 0, "peStrike": 0, "peLtp": 0}
    cs, ps = keys[i+1], keys[i-1]
    return {
        "total": round(sm[cs]["ce"] + sm[ps]["pe"], 2),
        "ceStrike": cs, "ceLtp": sm[cs]["ce"],
        "peStrike": ps, "peLtp": sm[ps]["pe"]
    }

def valid_highs(candles, max_count=5):
    out = []
    for i in range(len(candles)-1, -1, -1):
        c = candles[i]
        if c["close"] >= c["open"] or any(x["high"] > c["high"] for x in candles[i+1:]):
            continue
        if c["high"] not in out: out.append(c["high"])
        if len(out) >= max_count: break
    return out

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

def download_bhavcopy(trade_date=None, interval_minutes=15, final_hour=21):
    """Retry every 15 minutes until 21:00 IST; stop immediately on success."""
    d = trade_date or previous_market_day(now().date())
    ymd, ddmmyyyy = d.strftime("%Y%m%d"), d.strftime("%d%m%Y")
    urls = [
        f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip",
        f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d:%Y}/{d:%b}/fo{ddmmyyyy}bhav.csv.zip"
    ]
    attempt, last_error = 0, ""
    while True:
        current = now()
        attempt += 1
        final_attempt = current.hour >= final_hour
        print(f"Bhavcopy attempt {attempt} at {current:%Y-%m-%d %H:%M:%S} IST" +
              (" [FINAL]" if final_attempt else ""))

        for url in urls:
            try:
                r = requests.get(url, headers=HEADERS, timeout=30)
                if r.status_code != 200 or len(r.content) < 1000:
                    last_error = f"HTTP {r.status_code}, size={len(r.content)}"
                    print("Bhavcopy unavailable:", last_error)
                    continue
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    names = [x for x in z.namelist() if not x.endswith("/")]
                    if not names:
                        last_error = "ZIP contained no files"
                        continue
                    raw = z.read(names[0])
                text = raw.decode("utf-8-sig", errors="ignore")
                if len(text.strip()) < 100:
                    last_error = "Downloaded CSV is empty"
                    continue
                with open("bhavcopy.csv","w",encoding="utf-8",newline="") as f:
                    f.write(text)
                print("Bhavcopy downloaded:", url)
                return True
            except Exception as e:
                last_error = str(e)
                print("Bhavcopy download error:", e)

        current = now()
        if final_attempt or current.hour >= final_hour:
            print("21:00 IST final Bhavcopy attempt failed.")
            if last_error: print("Last error:", last_error)
            return False

        next_try = current.replace(second=0,microsecond=0)
        m = ((current.minute // interval_minutes)+1)*interval_minutes
        next_try = (next_try.replace(minute=0)+dt.timedelta(hours=1)
                    if m >= 60 else next_try.replace(minute=m))
        final_time = current.replace(hour=final_hour,minute=0,second=0,microsecond=0)
        target = min(next_try, final_time)
        wait = max(1,int((target-current).total_seconds()))
        print(f"Bhavcopy not available. Next attempt at {target:%H:%M:%S} IST")
        time.sleep(wait)


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
    dom = "BUYERS" if cl >= 1.5*hc and h != l else "SELLERS" if hc >= 1.5*cl and h != l else "NEUTRAL"
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
        return error_payload("NSE F&O Bhavcopy download failed.", spot)

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

    ivatm, s1, s2 = atm50(spot), atm100(spot), hlc
    ce, pe = dominance(wb.get((hlc,"CE"))), dominance(wb.get((hlc,"PE")))

    # Reuse one NSE option-chain request for both IV and time-value.
    chain = nse_data()

    for obj, kind in ((ce,"CE"),(pe,"PE")):
        v = wb.get((ivatm,kind),{}).get("iv",0)
        if v > 0:
            obj["iv"] = round(v,2)
        elif obj["close"] > 0:
            obj["iv"] = live_iv(
                ivatm, wexp, kind,
                wb.get((ivatm,kind),{}).get("close",0),
                spot, chain
            )

    tv = time_value(spot,wexp,wb,chain)
    get = lambda s,k: wb.get((s,k),{}).get("close",0)
    s1v = round((get(s1+100,"CE")+get(s1-100,"PE"))/2,2)
    s2v = round((get(s2+100,"CE")+get(s2-100,"PE"))/2,2)

    mins = round(hlc+ce["close"],2)
    mind = round(hlc-pe["close"],2)
    maxs = round(hlc+ce["close"]+pe["close"],2)
    maxd = round(hlc-ce["close"]-pe["close"],2)

    highs = valid_highs(daily_candles())
    highs = [round(maxs+(maxs-h)+25,2) if h <= maxs else h for h in highs]
    wl, wh = math.floor(spot/100)*100, math.ceil(spot/100)*100
    diff = round(hi-lo,2)

    payload = {
        "dataStatus":"SUCCESS", "bhavcopyReady":True,
        "currentDate":display_date(t), "expiryDate":wexp.strftime("%Y-%m-%d"),
        "spotPrice":spot, "hlcAtmStrike":hlc, "ivAtmStrike":ivatm,
        "ce":ce, "pe":pe, "ceTag":ce["dominance"], "ceClass":ce["tagClass"],
        "peTag":pe["dominance"], "peClass":pe["tagClass"],
        "bannerTotal":round(ce["close"]+pe["close"],2),
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
        process(*spot)
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

    raise SystemExit(0 if process(*spot) else 1)
