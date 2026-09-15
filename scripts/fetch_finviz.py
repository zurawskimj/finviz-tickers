#!/usr/bin/env python3
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pandas_market_calendars as mcal
from playwright.sync_api import sync_playwright

BASE_URL = "https://finviz.com/screener?v=111&f=exch_nasd,sh_price_u10,ta_change_u,ta_highlow20d_nh,ta_highlow50d_nh,ta_highlow52w_nh,ta_perf_dup&ft=4"
OUTPUT = Path(os.environ.get("OUTPUT_FILE", "finviz_test_output.txt"))
FORCE_RUN = os.environ.get("FORCE_RUN", "0") == "1"
UPDATE_PRODUCTION = os.environ.get("UPDATE_PRODUCTION", "0") == "1"


def scheduled_window_ok() -> tuple[bool, str]:
    if FORCE_RUN:
        return True, "FORCE_RUN=1"
    now = datetime.now(timezone.utc)
    cal = mcal.get_calendar("NASDAQ")
    sched = cal.schedule(start_date=now.date(), end_date=now.date())
    if sched.empty:
        return False, "NASDAQ closed today"
    close = sched.iloc[0]["market_close"].to_pydatetime()
    target = close.timestamp() - 70 * 60
    diff = abs(now.timestamp() - target)
    return diff <= 8 * 60, f"now={now.isoformat()} close={close.isoformat()} diff_to_target={diff/60:.1f}min"


def with_r(url: str, r: int) -> str:
    p = urlparse(url)
    q = parse_qs(p.query)
    q["r"] = [str(r)]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), p.fragment))


def extract_total(text: str) -> int | None:
    patterns = [
        r"\bTotal\s*:?\s*(\d+)\b",
        r"\b1\s*-\s*\d+\s+of\s+(\d+)\b",
        r"\bof\s+(\d+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return int(m.group(1))
    return None


def extract_tickers(page) -> list[str]:
    vals = page.locator('a[href*="quote.ashx?t="]').evaluate_all(
        "els => els.map(e => new URL(e.href).searchParams.get('t'))"
    )
    out = []
    seen = set()
    for v in vals:
        if not v:
            continue
        v = v.strip().upper()
        if re.fullmatch(r"[A-Z0-9.-]{1,10}", v) and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def main() -> int:
    ok, why = scheduled_window_ok()
    print(f"Schedule check: {ok} ({why})")
    if not ok:
        print("SKIP: outside the 70-minute window.")
        return 0

    all_tickers: list[str] = []
    seen = set()
    total = None
    pages = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        r = 1
        while True:
            url = BASE_URL if r == 1 else with_r(BASE_URL, r)
            print(f"Opening: {url}")
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if resp is None or resp.status >= 400:
                raise RuntimeError(f"HTTP problem at r={r}: {None if resp is None else resp.status}")
            page.wait_for_timeout(2500)
            body = page.locator("body").inner_text(timeout=15000)
            lowered = body.lower()
            if any(x in lowered for x in ["captcha", "access denied", "temporarily blocked", "verify you are human"]):
                raise RuntimeError("Finviz blocking/challenge detected")

            if total is None:
                total = extract_total(body)
                print(f"Reported total: {total}")
                if total is None:
                    raise RuntimeError("Could not determine total result count from Finviz page")

            tickers = extract_tickers(page)
            pages += 1
            print(f"Page {pages}: extracted {len(tickers)} ticker candidates")
            if not tickers:
                raise RuntimeError(f"No tickers extracted from page r={r}")

            before = len(all_tickers)
            for t in tickers:
                if t not in seen:
                    seen.add(t)
                    all_tickers.append(t)
            added = len(all_tickers) - before
            print(f"Page {pages}: added {added} unique tickers; cumulative={len(all_tickers)}")

            if len(all_tickers) >= total:
                break
            r += 20
            if pages > 100:
                raise RuntimeError("Pagination safety limit exceeded")

        browser.close()

    if total != len(all_tickers):
        raise RuntimeError(f"Validation failed: Finviz total={total}, unique tickers={len(all_tickers)}")

    OUTPUT.write_text("\n".join(all_tickers) + "\n", encoding="utf-8")
    print(f"SUCCESS: total={total}, pages={pages}, unique={len(all_tickers)}")
    print("Tickers:")
    print("\n".join(all_tickers))

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## Finviz test result\n\n")
            f.write(f"- Reported results: **{total}**\n")
            f.write(f"- Unique tickers: **{len(all_tickers)}**\n")
            f.write(f"- Pages read: **{pages}**\n")
            f.write(f"- Production update requested: **{UPDATE_PRODUCTION}**\n\n")
            f.write("```text\n" + "\n".join(all_tickers) + "\n```\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
