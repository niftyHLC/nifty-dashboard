def download_and_parse_bhavcopy():
    """
    Downloads the latest NSE FO Bhavcopy ZIP and logs CSV headers for debugging.
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
                        text_wrapper = io.TextIOWrapper(f, encoding="utf-8")
                        reader = csv.DictReader(text_wrapper)
                        
                        # DEBUG: Print exact column headers found in the UDiFF CSV
                        print(f"CSV Columns detected: {reader.fieldnames}")

                        nifty_rows = []
                        spot_price = 0.0
                        count = 0

                        for row in reader:
                            clean_row = {k.strip().upper(): v.strip() if v else "" for k, v in row.items() if k}
                            
                            # Print first 2 rows of NIFTY data found to inspect values
                            symbol = clean_row.get("SYMBOL") or clean_row.get("TCKRSYMB") or clean_row.get("FININSTRMID") or ""
                            if "NIFTY" in symbol and count < 2:
                                print(f"Sample Row: {clean_row}")
                                count += 1

                            if "NIFTY" in symbol and "NIFTY IT" not in symbol and "NIFTY BANK" not in symbol:
                                strike_str = clean_row.get("STRIKE_PR") or clean_row.get("STKPRC") or clean_row.get("STRIKE_PRICE") or "0"
                                opt_type = clean_row.get("OPTION_TYP") or clean_row.get("OPTNTP") or clean_row.get("OPTION_TYPE") or ""
                                close_str = clean_row.get("CLOSE") or clean_row.get("CLSPRC") or clean_row.get("CLOSE_PRICE") or "0"
                                high_str = clean_row.get("HIGH") or clean_row.get("HGHPRC") or clean_row.get("HIGH_PRICE") or "0"
                                low_str = clean_row.get("LOW") or clean_row.get("LWPRC") or "0"
                                
                                expiry_str = (
                                    clean_row.get("EXPIRY_DT") or 
                                    clean_row.get("XPRTNDT") or 
                                    clean_row.get("TTLEXPIRDT") or 
                                    clean_row.get("EXPIRY") or 
                                    clean_row.get("EXPIRY_DATE") or ""
                                )

                                try:
                                    strike = float(strike_str)
                                    close_p = float(close_str)
                                    high_p = float(high_str)
                                    low_p = float(low_str)
                                except ValueError:
                                    continue

                                if opt_type in ["CE", "PE", "CallOptions", "PutOptions"] and expiry_str:
                                    # Normalize option type naming if necessary
                                    normalized_opt = "CE" if "CE" in opt_type.upper() else "PE"
                                    nifty_rows.append({
                                        "STRIKE_PR": strike,
                                        "OPTION_TYP": normalized_opt,
                                        "CLOSE": close_p,
                                        "HIGH": high_p,
                                        "LOW": low_p,
                                        "EXPIRY_DT": expiry_str
                                    })
                                
                                inst = clean_row.get("INSTRUMENT") or clean_row.get("SGMT") or clean_row.get("INSTRUMENT_TYPE") or ""
                                if ("FUT" in inst or "FUT" in opt_type) and spot_price == 0:
                                    und = clean_row.get("UNDERLYING_VALUE") or clean_row.get("CLSPRC") or clean_row.get("SPOT_PRICE")
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
