def download_and_parse_bhavcopy():
    """
    Downloads the latest NSE FO Bhavcopy ZIP and dynamically maps UDiFF columns.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "*/*"
    }

    target_date = get_latest_trading_day()

    for _ in range(5):
        ymd_str = target_date.strftime("%Y%m%d")
        date_formatted = target_date.strftime("%d-%b-%Y").upper()

        zip_url = f"https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd_str}_F_0000.csv.zip"
        
        print(f"Fetching Bhavcopy for {date_formatted} from {zip_url}...")
        try:
            res = requests.get(zip_url, headers=headers, timeout=15)
            
            if res.status_code == 200 and res.content[:4] == b'PK\x03\x04':
                print(f"Successfully retrieved valid ZIP Bhavcopy for {date_formatted}")

                with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                    csv_filename = z.namelist()[0]
                    with z.open(csv_filename) as f:
                        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
                        fieldnames = reader.fieldnames or []
                        
                        def find_key(keywords):
                            for fn in fieldnames:
                                fn_upper = fn.strip().upper()
                                if any(kw in fn_upper for kw in keywords):
                                    return fn
                            return None

                        # UDiFF & Legacy flexible header matching
                        sym_key = find_key(["TCKR", "SYMBOL", "SCTYSR", "FININSTRMID"])
                        strike_key = find_key(["STRK", "STRIKE", "STK"])
                        opt_key = find_key(["OPTN", "OPTION", "TYP"])
                        close_key = find_key(["CLS", "CLOSE"])
                        high_key = find_key(["HGH", "HIGH"])
                        low_key = find_key(["LW", "LOW"])
                        expiry_key = find_key(["XPRY", "EXPIRY", "XPRT", "EXPR"])
                        inst_key = find_key(["FININSTRMTP", "INSTRUMENT", "SGMT"])
                        und_key = find_key(["UNDRLYG", "UNDERLYING", "SPOT"])

                        nifty_rows = []
                        spot_price = 0.0

                        for row in reader:
                            symbol = row.get(sym_key, "").strip().upper() if sym_key else ""
                            inst = row.get(inst_key, "").strip().upper() if inst_key else ""

                            if "NIFTY" in symbol and "NIFTY IT" not in symbol and "NIFTY BANK" not in symbol:
                                try:
                                    strike = float(row.get(strike_key, 0)) if strike_key else 0.0
                                    close_p = float(row.get(close_key, 0)) if close_key else 0.0
                                    high_p = float(row.get(high_key, 0)) if high_key else 0.0
                                    low_p = float(row.get(low_key, 0)) if low_key else 0.0
                                    opt_type = row.get(opt_key, "").strip().upper() if opt_key else ""
                                    expiry_str = row.get(expiry_key, "").strip().upper() if expiry_key else ""
                                except ValueError:
                                    continue

                                normalized_opt = "CE" if "CE" in opt_type else ("PE" if "PE" in opt_type else "")

                                if normalized_opt in ["CE", "PE"] and expiry_str:
                                    nifty_rows.append({
                                        "STRIKE_PR": strike,
                                        "OPTION_TYP": normalized_opt,
                                        "CLOSE": close_p,
                                        "HIGH": high_p,
                                        "LOW": low_p,
                                        "EXPIRY_DT": expiry_str
                                    })
                                elif ("FUT" in inst or "FUT" in opt_type) and spot_price == 0:
                                    if und_key and row.get(und_key):
                                        try:
                                            spot_price = float(row.get(und_key))
                                        except ValueError:
                                            pass

                        if nifty_rows:
                            print(f"Successfully parsed {len(nifty_rows)} NIFTY option records.")
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
