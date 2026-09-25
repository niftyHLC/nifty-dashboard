import csv, datetime as dt, io, json, math, os, subprocess, time, zipfile
import holidays, numpy as np, requests, yfinance as yf
from scipy.optimize import brentq
from scipy.stats import norm

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DATE_FORMATS = ("%Y-%m-%d", "%d-%b-%Y", "%d-%B-%Y", "%d-%m-%y", "%d-%m-%Y", "%d%b%Y", "%d%b%y")
HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122 Safari/537.36", "Accept-Language":"en-US,en;q=0.9", "Referer":"https://www.nseindia.com/"}


def now(): return dt.datetime.now(IST)
def parse_date(x):
    if isinstance(x, dt.datetime): return x.date()
    if isinstance(x, dt.date): return x
    for f in DATE_FORMATS:
        try: return dt.datetime.strptime(str(x).strip(), f).date()
        except ValueError: pass
    return None

def market_holidays(): return set(holidays.India(years=now().year).keys())
MARKET_HOLIDAYS = market_holidays()

def next_market_date(d):
    while d.weekday() >= 5 or d in MARKET_HOLIDAYS: d += dt.timedelta(days=1)
    return d

def display_date(t):
    d = t.date()
    if t.hour > 15 or (t.hour == 15 and t.minute >= 30) or d.weekday() >= 5 or d in MARKET_HOLIDAYS:
        d += dt.timedelta(days=1)
    return next_market_date(d).strftime("%d %b %Y").upper()


def fetch_spot():
    try:
        d = yf.Ticker("^NSEI").history(period="1d")
        if not d.empty:
            c,h,l = map(float, (d.Close.iloc[-1], d.High.iloc[-1], d.Low.iloc[-1]))
            if c > 0: return c,h,l
    except Exception as e: print("Spot error:", e)
    return None


def nse_data(symbol="NIFTY"):
    try:
        s = requests.Session(); s.get("https://www.nseindia.com", headers=HEADERS, timeout=10); time.sleep(.5)
        r = s.get(f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}", headers=HEADERS, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print("NSE error:", e); return None


def atm50(spot): return int(round(spot / 50) * 50)
def atm100(spot): return int(round(spot / 100) * 100)


def bs_iv(kind, price, spot, strike, expiry):
    if min(price, spot, strike) <= 0: return 0.0
    try:
        days = max((parse_date(expiry) - now().date()).days, 1); T = days / 365; r = .065
        intrinsic = max(0, spot-strike) if kind == "CE" else max(0, strike-spot)
        if price < intrinsic: return 0.0
        def f(s):
            d1=(np.log(spot/strike)+(r+.5*s*s)*T)/(s*np.sqrt(T)); d2=d1-s*np.sqrt(T)
            p = spot*norm.cdf(d1)-strike*np.exp(-r*T)*norm.cdf(d2) if kind=="CE" else strike*np.exp(-r*T)*norm.cdf(-d2)-spot*norm.cdf(-d1)
            return p-price
        return round(float(brentq(f,1e-9,5,maxiter=100))*100,2)
    except Exception: return 0.0


def live_iv(strike, expiry, kind, price, spot, data=None):
    data = data or nse_data()
    if data:
        target = parse_date(expiry)
        for x in data.get("records",{}).get("data",[]):
            if float(x.get("strikePrice",0)) != float(strike): continue
            o=x.get(kind,{})
            if parse_date(o.get("expiryDate")) == target:
                v=float(o.get("impliedVolatility",0) or 0)
                if v>0: return v
    return bs_iv(kind,price,spot,strike,expiry)


def time_value(spot, expiry, bhav, data=None):
    atm=atm50(spot); sm={}
    data=data or nse_data()
    if data:
        target=parse_date(expiry)
        for x in data.get("records",{}).get("data",[]):
            s=float(x.get("strikePrice",0)); ce=x.get("CE",{}); pe=x.get("PE",{})
            if parse_date(ce.get("expiryDate") or pe.get("expiryDate"))==target:
                sm[s]={"ce":float(ce.get("lastPrice",0) or 0),"pe":float(pe.get("lastPrice",0) or 0)}
    if not sm:
        for (s,k),v in bhav.items(): sm.setdefault(s,{"ce":0,"pe":0})[k.lower()]=v["close"]
    keys=sorted(sm)
    if len(keys)<3: return {"total":0,"ceStrike":0,"ceLtp":0,"peStrike":0,"peLtp":0}
    i=min(range(len(keys)),key=lambda j:abs(keys[j]-atm))
    if i==0 or i==len(keys)-1: return {"total":0,"ceStrike":0,"ceLtp":0,"peStrike":0,"peLtp":0}
    cs,ps=keys[i+1],keys[i-1]; ce,pe=sm[cs]["ce"],sm[ps]["pe"]
    return {"total":round(ce+pe,2),"ceStrike":cs,"ceLtp":ce,"peStrike":ps,"peLtp":pe}


def valid_highs(candles, max_count=5):
    out=[]
    for i in range(len(candles)-1,-1,-1):
        c=candles[i]
        if c["close"]>=c["open"] or any(x["high"]>c["high"] for x in candles[i+1:]): continue
        if c["high"] not in out: out.append(c["high"])
        if len(out)>=max_count: break
    return out


def daily_candles():
    try:
        d=yf.Ticker("^NSEI").history(period="6mo",interval="1d")
        return [{"open":float(r.Open),"high":float(r.High),"low":float(r.Low),"close":float(r.Close)} for _,r in d.iterrows()] if not d.empty else []
    except Exception as e: print("Daily data error:",e); return []


def download_bhavcopy(retries=3, delay=20):
    t=now(); url=f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{t:%Y%m%d}_F_0000.csv.zip"
    for n in range(1,retries+1):
        try:
            r=requests.get(url,headers=HEADERS,timeout=30)
            if r.status_code==200 and len(r.content)>1000:
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    text=z.read(z.namelist()[0]).decode("utf-8",errors="ignore").splitlines()
                rows=[text[0]]+[x for x in text[1:] if "NIFTY" in x.upper()]
                with open("bhavcopy.csv","w",encoding="utf-8") as f: f.write("\n".join(rows))
                return True
            print(f"Bhavcopy attempt {n}: HTTP {r.status_code}")
        except Exception as e: print(f"Bhavcopy attempt {n}:",e)
        if n<retries: time.sleep(delay)
    return False


def load_bhav(expiry):
    out={}; target=parse_date(expiry)
    if not target or not os.path.exists("bhavcopy.csv"): return out
    try:
        with open("bhavcopy.csv",encoding="utf-8",errors="ignore") as f:
            for r in csv.DictReader(f):
                r={k.strip().upper():(v.strip() if v else "") for k,v in r.items() if k}
                if "NIFTY" not in (r.get("TCKRSYMB") or r.get("SYMBOL") or "").upper(): continue
                s=int(round(float(r.get("STRKPRIC") or r.get("STRIKE_PR") or r.get("STRIKE") or 0)))
                ot=r.get("OPTNTP") or r.get("OPTION_TYP") or ""; k="CE" if "CE" in ot.upper() else "PE" if "PE" in ot.upper() else ""
                if not k or parse_date(r.get("XPRYDT") or r.get("EXPIRY_DT"))!=target: continue
                g=lambda *a: float(next((r[x] for x in a if r.get(x)),0) or 0)
                close=g("CLSPRIC","CLOSE")
                if close>0: out[(s,k)]={"open":g("OPENPRIC","OPEN"),"high":g("HGHPRIC","HIGH") or close,"low":g("LWPRIC","LOW") or close,"close":close,"chg_oi":g("CHGINOI","CHG_IN_OI"),"iv":g("IV","IMPLIED_VOL")}
    except Exception as e: print("Bhavcopy parse error:",e)
    return out


def dominance(d):
    d=d or {}; c=d.get("close",0); h=d.get("high",0) or c; l=d.get("low",0) or c; iv=d.get("iv",0)
    hc=round(h-c,2); cl=round(c-l,2)
    dom="BUYERS" if cl>=1.5*hc and h!=l else "SELLERS" if hc>=1.5*cl and h!=l else "NEUTRAL"
    tag={"BUYERS":("green","tag-buyers"),"SELLERS":("red","tag-sellers"),"NEUTRAL":("orange","tag-neutral")}[dom]
    return {"high":round(h,2),"close":round(c,2),"low":round(l,2),"iv":round(iv,2),"hc":hc,"cl":cl,"dominance":dom,"themeColor":tag[0],"tagClass":tag[1]}


def zone(wl,wh,b):
    p=lambda s,k:b.get((s,k),{}).get("close",0)
    return {"line1":round(wl+p(wl,"CE")+p(wl,"PE"),2),"line2":round(wh-p(wh,"CE")-p(wh,"PE"),2)}


def push():
    try:
        subprocess.run(["git","config","--global","user.name","github-actions[bot]"],check=True)
        subprocess.run(["git","config","--global","user.email","github-actions[bot]@users.noreply.github.com"],check=True)
        subprocess.run(["git","add","-f","data.json"],check=False)
        if os.path.exists("bhavcopy.csv"): subprocess.run(["git","add","-f","bhavcopy.csv"],check=False)
        if subprocess.run(["git","diff","--cached","--quiet"],capture_output=True).returncode:
            subprocess.run(["git","commit","-m","Auto-update dashboard [skip ci]"],check=True); subprocess.run(["git","push","origin","main"],check=True)
    except Exception as e: print("Git push failed:",e)


def process(spot,hi,lo,force_not_ready=False):
    t=now(); ready=False if force_not_ready else download_bhavcopy(); expiries=set()
    if os.path.exists("bhavcopy.csv"):
        with open("bhavcopy.csv",encoding="utf-8",errors="ignore") as f:
            for r in csv.DictReader(f):
                x=parse_date(r.get("XPRYDT") or r.get("EXPIRY_DT") or r.get("EXPIRY"));
                if x: expiries.add(x)
    closed=t.hour>15 or (t.hour==15 and t.minute>=30); today=t.date()
    valid=sorted(x for x in expiries if x>today or (x==today and not closed)) or sorted(expiries)
    wexp=valid[0] if valid else today; same=[x for x in valid if x.month==wexp.month and x.year==wexp.year]; mexp=same[-1] if same else wexp
    wb,mb=load_bhav(wexp),load_bhav(mexp)
    strikes=sorted({s for s,k in wb}); hlc=atm50(spot); mind=float("inf")
    for s in strikes:
        ce,pe=wb.get((s,"CE"),{}).get("close",0),wb.get((s,"PE"),{}).get("close",0)
        if ce>0 and pe>0 and abs(ce-pe)<mind: mind=abs(ce-pe); hlc=s
    ivatm=atm50(spot); s1=atm100(spot); s2=hlc
    ce=dominance(wb.get((hlc,"CE"))); pe=dominance(wb.get((hlc,"PE")))
    for k,kind in (("ce","CE"),("pe","PE")):
        v=wb.get((ivatm,kind),{}).get("iv",0)
        ce["iv"] if k=="ce" else pe["iv"]
        if v>0: (ce if k=="ce" else pe)["iv"]=round(v,2)
        elif (ce if k=="ce" else pe)["iv"]==0: (ce if k=="ce" else pe)["iv"]=live_iv(ivatm,wexp,kind,wb.get((hlc,kind),{}).get("close",0),spot)
    tv=time_value(spot,wexp,wb)
    get=lambda s,k: wb.get((s,k),{}).get("close",0)
    s1v=round((get(s1+100,"CE")+get(s1-100,"PE"))/2,2); s2v=round((get(s2+100,"CE")+get(s2-100,"PE"))/2,2)
    mins=round(hlc+ce["close"],2); mind=round(hlc-pe["close"],2); maxs=round(hlc+ce["close"]+pe["close"],2); maxd=round(hlc-ce["close"]-pe["close"],2)
    highs=valid_highs(daily_candles())
    highs=[round(maxs+(maxs-h)+25,2) if h<=maxs else h for h in highs]
    wl,wh=math.floor(spot/100)*100,math.ceil(spot/100)*100; diff=round(hi-lo,2)
    payload={"dataStatus":"SUCCESS","bhavcopyReady":ready,"currentDate":display_date(t),"expiryDate":wexp.strftime("%Y-%m-%d"),"spotPrice":spot,"hlcAtmStrike":hlc,"ivAtmStrike":ivatm,"ce":ce,"pe":pe,"ceTag":ce["dominance"],"ceClass":ce["tagClass"],"peTag":pe["dominance"],"peClass":pe["tagClass"],"bannerTotal":round(ce["close"]+pe["close"],2),"asymmetricTimeValue":tv,"minSupply":mins,"minDemand":mind,"maxSupply":maxs,"maxDemand":maxd,"sellersArea":{"min":mins,"max":maxs,"validUnbrokenHighs":highs},"buyersArea":{"min":mind,"max":maxd},"weeklyZones":zone(wl,wh,wb),"monthlyZones":zone(wl,wh,mb),"spotHigh":hi,"spotLow":lo,"spotDifference":diff,"earthLevel":round(diff*.2611,2),"sniper1":{"strike":s1,"ce":get(s1,"CE"),"pe":get(s1,"PE"),"otmCeStrike":s1+100,"otmPeStrike":s1-100,"otmCe":get(s1+100,"CE"),"otmPe":get(s1-100,"PE"),"value":s1v},"sniper2":None if s1==s2 else {"strike":s2,"ce":get(s2,"CE"),"pe":get(s2,"PE"),"otmCeStrike":s2+100,"otmPeStrike":s2-100,"otmCe":get(s2+100,"CE"),"otmPe":get(s2-100,"PE"),"value":s2v}}
    with open("data.json","w") as f: json.dump(payload,f,indent=4)
    push()


if __name__=="__main__":
    t=now(); off=t.weekday()>=5 or t.date() in MARKET_HOLIDAYS
    spot=fetch_spot()
    if not spot: raise SystemExit("ERROR: NIFTY spot unavailable; no fallback prices used.")
    if off: process(*spot,force_not_ready=True); raise SystemExit
    if os.path.exists("data.json"):
        try:
            with open("data.json") as f: old=json.load(f)
            if old.get("currentDate")==display_date(t) and old.get("bhavcopyReady") is True: raise SystemExit("Today's data already processed.")
        except SystemExit: raise
        except Exception: pass
    process(*spot)
