import json
import math
import os
import sys
import time
import csv
from datetime import datetime
import requests
import yfinance as yf


def fetch_nse_data():
    """Fetch option chain and spot price directly from NSE India APIs."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    session = requests.Session()
    session.headers.update(headers)

    try:
        # Establish session cookies
        session.get("https://www.nseindia.com", timeout=10)
        
        # Fetch NIFTY Option Chain Data
        oc_url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
        response = session.get(oc_url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            records = data.get("records", {})
            spot_price = records.get("underlyingValue", 0)
            expiry_dates = records.get("expiryDates", [])
            
            if not expiry_dates or spot_price == 0:
                return None
                
            target_expiry = expiry_dates[0]
            option_data = data.get("filtered", {}).get("data", [])
            
            return {
                "spot_price": spot_price,
                "expiry_date": target_expiry,
                "chain": option_data,
                "source": "NSE"
            }
    except Exception as e:
        print(f"NSE fetch failed: {e}")
    return None


def fetch_yfinance_data():
    """Fallback: Fetch NIFTY spot and option chain data via Yahoo Finance."""
    try:
        ticker = yf.Ticker("^NSEI")
        history = ticker.history(period="1d")
        if history.empty:
            return None
            
        spot_price = history["Close"].iloc[-1]
        expirations = ticker.options
        
        if not expirations:
            return None
            
        target_expiry = expirations[0]
        opt = ticker.option_chain(target_expiry)
        
        # Convert yfinance DataFrames to standard dict structure
        chain = []
        calls = opt.calls.set_index("strike")
        puts = opt.puts.set_index("strike")
        
        all_strikes = sorted(list(set(calls.index).union(set(puts.index))))
        for strike in all_strikes:
            item = {"strikePrice": strike}
            if strike in calls.index:
                row = calls.loc[strike]
                item["CE"] = {
                    "lastPrice": row.get("lastPrice", 0),
                    "highPrice": row.get("high", row.get("lastPrice", 0)),
                    "lowPrice": row.get("low", row.get("lastPrice", 0)),
                    "closePrice": row.get("lastPrice", 0)
                }
            if strike in puts.index:
                row = puts.loc[strike]
                item["PE"] = {
                    "lastPrice": row.get("lastPrice", 0),
                    "highPrice": row.get("high", row.get("lastPrice", 0)),
                    "lowPrice": row.get("low", row.get("lastPrice", 0)),
                    "closePrice": row.get("lastPrice", 0)
                }
            chain.append(item)
            
        return {
            "spot_price": spot_price,
            "expiry_date": target_expiry,
            "chain": chain,
            "source": "yfinance"
        }
    except Exception as e:
        print(f"yfinance fetch failed: {e}")
    return None


def calculate_dashboard_data(market_data):
    """Process market raw data into the structured schema required by index.html."""
    spot_price = market_data["spot_price"]
    chain = market_data["chain"]
    
    # 1. Determine HLC ATM Strike (Nearest 50 step)
    hlc_atm_strike = round(spot_price / 50) * 50
    # 2. Determine Round 100 Strike
    round_100_strike = round(spot_price / 100) * 100

    ce_data = {"close": 0, "high": 0, "low": 0}
    pe_data = {"close": 0, "high": 0, "low": 0}
    
    # Extract CE and PE for ATM strike
    for row in chain:
        if row.get("strikePrice") == hlc_atm_strike:
            if "CE" in row:
                ce = row["CE"]
                ce_data = {
                    "close": ce.get("closePrice") or ce.get("lastPrice", 0),
                    "high": ce.get("highPrice") or ce.get("lastPrice", 0),
                    "low": ce.get("lowPrice") or ce.get("lastPrice", 0)
                }
            if "PE" in row:
                pe = row["PE"]
                pe_data = {
                    "close": pe.get("closePrice") or pe.get("lastPrice", 0),
                    "high": pe.get("highPrice") or pe.get("lastPrice", 0),
                    "low": pe.get("lowPrice") or pe.get("lastPrice", 0)
                }
            break

    # Tags & Classes
    ce_hc = ce_data["high"] - ce_data["close"]
    ce_cl = ce_data["close"] - ce_data["low"]
    pe_hc = pe_data["high"] - pe_data["close"]
    pe_cl = pe_data["close"] - pe_data["low"]

    ce_tag = "BUYERS" if ce_cl > ce_hc else ("SELLERS" if ce_hc > ce_cl else "NEUTRAL")
    ce_class = "tag-buyers" if ce_tag == "BUYERS" else ("tag-sellers" if ce_tag == "SELLERS" else "tag-neutral")

    pe_tag = "BUYERS" if pe_cl > pe_hc else ("SELLERS" if pe_hc > pe_cl else "NEUTRAL")
    pe_class = "tag-buyers" if pe_tag == "BUYERS" else ("tag-sellers" if pe_tag == "SELLERS" else "tag-neutral")

    straddle_sum = ce_data["close"] + pe_data["close"]

    # Construct JSON response
    output = {
        "bhavcopyReady": True,
        "spotPrice": round(spot_price, 2),
        "currentDate": datetime.now().strftime("%d %b %Y").upper(),
        "expiryDate": market_data["expiry_date"],
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
    return output


def main():
    print("Starting NIFTY Market Data Update...")
    
    # Try primary source (NSE)
    market_data = fetch_nse_data()
    
    # Fallback source (yfinance)
    if not market_data:
        print("Falling back to yfinance...")
        market_data = fetch_yfinance_data()

    # Raise exception safely if both fail (No combined syntax on exception line)
    if not market_data:
        raise Exception("Fatal Error: Could not fetch option chain or spot price from both NSE and yfinance.")

    # Calculate values and construct JSON
    dashboard_json = calculate_dashboard_data(market_data)
    
    # Write output to data.json
    output_path = "data.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(dashboard_json, f, indent=2)
        
    print(f"Successfully updated {output_path} via {market_data['source']} at {datetime.now()}")


if __name__ == "__main__":
    main()
