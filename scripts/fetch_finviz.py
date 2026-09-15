#!/usr/bin/env python3
import os, re, sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import pandas_market_calendars as mcal
from playwright.sync_api import sync_playwright

BASE_URL = "https://finviz.com/screener?v=111&f=exch_nasd,sh_price_u10,ta_change_u,ta_highlow20d_nh,ta_highlow50d_nh,ta_highlow52w_nh,ta_perf_dup&ft=4"
OUTPUT = Path(os.environ.get("OUTPUT_FILE", "finviz_test_output.txt"))
DEBUG = Path("finviz_debug.txt")
FORCE_RUN = os.environ.get("FORCE_RUN", "0") == "1"


def scheduled_window_ok():
    if FORCE_RUN:
        return True, "FORCE_RUN=1"
    now = datetime.now(timezone.utc)
    sched = mcal.get_calendar("NASDAQ").schedule(start_date=now.date(), end_date=now.date())
    if sched.empty:
        return False, "NASDAQ closed today"
    close = sched.iloc[0]["market_close"].to_pydatetime()
    diff = abs(now.timestamp() - (close.timestamp() - 70 * 60))
    return diff <= 8 * 60, f"now={now.isoformat()} close={close.isoformat()} diff={diff/60:.1f}min"


def with_r(url, r):
    p = urlparse(url); q = parse_qs(p.query); q["r"] = [str(r)]
    return urlunparse((p.scheme,p.netloc,p.path,p.params,urlencode(q,doseq=True),p.fragment))


def extract_total(text):
    compact = re.sub(r"[\u00a0\s]+", " ", text)
    for pat in [
        r"#\s*\d+\s*/\s*(\d+)\s*Total",
        r"\bTotal\s*:?\s*(\d+)\b",
        r"\bResults?\s*:?\s*(\d+)\b",
        r"\bShowing\s+\d+\s*-\s*\d+\s+of\s+(\d+)\b",
    ]:
        m = re.search(pat, compact, re.I)
        if m: return int(m.group(1))
    return None


def unique_valid(values):
    out=[]; seen=set()
    for v in values:
        if not v: continue
        v=v.strip().upper()
        if re.fullmatch(r"[A-Z0-9.-]{1,10}", v) and v not in seen:
            seen.add(v); out.append(v)
    return out


def extract_tickers(page):
    # Try URL parameters first.
    vals = page.locator('a').evaluate_all("els => els.map(e => e.getAttribute('href')).filter(Boolean)")
    from_links=[]
    for href in vals:
        m=re.search(r"(?:quote\.ashx|quote)/?\?[^#]*\bt=([A-Za-z0-9.\-]+)", href, re.I)
        if m: from_links.append(m.group(1))
    got=unique_valid(from_links)
    if got: return got

    # Current Finviz can render the screener rows without classic quote anchors.
    # Look at table rows and take the cell immediately after the numeric row number.
    rows = page.locator("table tr").evaluate_all("rows => rows.map(r => Array.from(r.cells).map(c => c.innerText.trim()))")
    candidates=[]
    for cells in rows:
        if len(cells) < 2: continue
        if re.fullmatch(r"\d+", cells[0] or ""):
            for cell in cells[1:4]:
                if re.fullmatch(r"[A-Z0-9.-]{1,10}", cell or ""):
                    candidates.append(cell); break
    return unique_valid(candidates)


def save_debug(body,page,tickers):
    hrefs=page.locator('a').evaluate_all("els => els.map(e => [e.innerText, e.getAttribute('href')]).filter(x=>x[1])")
    tables=page.locator('table').evaluate_all("ts => ts.map((t,i)=>'TABLE '+i+'\\n'+t.innerText)")
    DEBUG.write_text(
        "=== TICKERS ===\n"+"\n".join(tickers)+
        "\n\n=== QUOTE-LIKE LINKS ===\n"+
        "\n".join(f"{a} -> {h}" for a,h in hrefs if 'quote' in h.lower())+
        "\n\n=== TABLES ===\n"+"\n\n".join(tables[-12:])+
        "\n\n=== FULL BODY ===\n"+body+
        "\n\n=== URL ===\n"+page.url+"\n", encoding="utf-8")


def main():
    ok,why=scheduled_window_ok(); print(f"Schedule check: {ok} ({why})")
    if not ok: return 0
    all_tickers=[]; seen=set(); total=None; pages=0; prev=None
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={"width":1440,"height":1600}, locale="en-US")
        r=1
        while True:
            url=BASE_URL if r==1 else with_r(BASE_URL,r)
            print("Opening:",url)
            resp=page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if not resp or resp.status>=400: raise RuntimeError(f"HTTP {None if not resp else resp.status}")
            page.wait_for_timeout(5000)
            body=page.locator('body').inner_text(timeout=15000)
            if any(x in body.lower() for x in ['captcha','access denied','temporarily blocked','verify you are human']):
                save_debug(body,page,[]); raise RuntimeError('Finviz challenge detected')
            if total is None:
                total=extract_total(body); print('Reported total:',total)
            tickers=extract_tickers(page); pages+=1
            print(f"Page {pages}: {tickers}")
            save_debug(body,page,tickers)
            if not tickers: break
            if tickers==prev: break
            prev=tickers
            for t in tickers:
                if t not in seen: seen.add(t); all_tickers.append(t)
            if total is not None and len(all_tickers)>=total: break
            r+=20
            if pages>=10: break
        browser.close()
    OUTPUT.write_text("\n".join(all_tickers)+("\n" if all_tickers else ""),encoding='utf-8')
    if not all_tickers: raise RuntimeError('No ticker symbols could be extracted; see finviz_debug.txt')
    if total is None: raise RuntimeError(f'Extracted {len(all_tickers)} tickers but could not parse total')
    if len(all_tickers)!=total: raise RuntimeError(f'Validation failed: total={total}, unique={len(all_tickers)}')
    print(f"SUCCESS: total={total}, pages={pages}, unique={len(all_tickers)}")
    print("\n".join(all_tickers)); return 0

if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as e:
        print('ERROR:',e,file=sys.stderr); raise
