from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pandas_market_calendars as mcal
from bs4 import BeautifulSoup
from curl_cffi import requests

BASE_URL = "https://finviz.com/screener.ashx"
FILTERS = (
    "exch_nasd,sh_price_u10,ta_change_u,ta_highlow20d_nh,"
    "ta_highlow50d_nh,ta_highlow52w_nh,ta_perf_dup"
)
PAGE_SIZE = 20
TARGET_MINUTES_BEFORE_CLOSE = 70
SCHEDULE_DELAY_TOLERANCE_MINUTES = 45

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://finviz.com/",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/152.0.0.0 Safari/537.36"
    ),
}

TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,16}$")
TOTAL_PATTERNS = (
    re.compile(r"#?\s*\d+\s*/\s*([\d,]+)\s+Total", re.IGNORECASE),
    re.compile(r"\b([\d,]+)\s+Total\b", re.IGNORECASE),
)


def should_run_now(force: bool) -> bool:
    if force:
        print("Manual run: bypassing the NASDAQ timing check.")
        return True

    now = datetime.now(timezone.utc)
    today = now.date()
    nasdaq = mcal.get_calendar("NASDAQ")
    schedule = nasdaq.schedule(start_date=today, end_date=today)

    if schedule.empty:
        print(f"SKIP: {today} is not a NASDAQ trading session.")
        return False

    market_close = schedule.iloc[0]["market_close"].to_pydatetime()
    if market_close.tzinfo is None:
        market_close = market_close.replace(tzinfo=timezone.utc)

    target = market_close - timedelta(minutes=TARGET_MINUTES_BEFORE_CLOSE)
    delay = now - target

    print(f"NASDAQ close (UTC): {market_close.isoformat()}")
    print(f"Target time  (UTC): {target.isoformat()}")
    print(f"Current time (UTC): {now.isoformat()}")

    if delay.total_seconds() < 0:
        print("SKIP: this workflow fired before the target time.")
        return False

    if delay > timedelta(minutes=SCHEDULE_DELAY_TOLERANCE_MINUTES):
        print("SKIP: this workflow is too far past the 70-minutes-before-close target.")
        return False

    return True


def request_page(session: requests.Session, start_row: int) -> str:
    params = {
        "v": "111",
        "f": FILTERS,
        "ft": "4",
    }
    if start_row > 1:
        params["r"] = str(start_row)

    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            response = session.get(
                BASE_URL,
                params=params,
                headers=HEADERS,
                impersonate="chrome",
                timeout=30,
            )
            response.raise_for_status()
            html = response.text
            lower = html.lower()

            if (
                "cf-chl-" in lower
                or "just a moment" in lower
                or "attention required" in lower
                or "access denied" in lower
            ):
                raise RuntimeError("Finviz/Cloudflare returned a challenge or access-denied page")

            if "finviz" not in lower:
                raise RuntimeError("Response does not look like a Finviz page")

            return html
        except Exception as exc:  # network / HTTP / anti-bot response
            last_error = exc
            if attempt < 3:
                wait_seconds = attempt * 5
                print(
                    f"Page r={start_row}: attempt {attempt}/3 failed: {exc}. "
                    f"Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)

    raise RuntimeError(f"Could not download Finviz page r={start_row}: {last_error}")


def ticker_from_href(href: str) -> str | None:
    try:
        values = parse_qs(urlsplit(href).query).get("t")
    except Exception:
        return None

    if not values:
        return None

    ticker = values[0].strip().upper()
    if not TICKER_RE.fullmatch(ticker):
        return None
    return ticker


def extract_tickers(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    tickers: list[str] = []

    # Prefer actual screener table rows (first cell is normally the row number).
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells:
            continue

        first_cell = cells[0].get_text(" ", strip=True)
        if not re.fullmatch(r"\d+", first_cell):
            continue

        for link in row.select('a[href*="quote.ashx?t="]'):
            ticker = ticker_from_href(link.get("href", ""))
            if ticker and ticker not in tickers:
                tickers.append(ticker)
                break

    # Fallback for future Finviz markup changes.
    if not tickers:
        for link in soup.select('a[href*="quote.ashx?t="]'):
            ticker = ticker_from_href(link.get("href", ""))
            if ticker and ticker not in tickers:
                tickers.append(ticker)

    return tickers


def extract_total(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)

    for pattern in TOTAL_PATTERNS:
        match = pattern.search(page_text)
        if match:
            return int(match.group(1).replace(",", ""))

    raise RuntimeError("Could not determine the Finviz 'Total' result count; refusing to overwrite ticki.txt")


def fetch_all_tickers() -> list[str]:
    session = requests.Session()

    first_html = request_page(session, 1)
    total = extract_total(first_html)
    first_page = extract_tickers(first_html)

    print(f"Finviz reports {total} total result(s).")
    print(f"Page 1: {len(first_page)} ticker(s).")

    if total == 0:
        if first_page:
            raise RuntimeError("Finviz reports 0 results but ticker links were found")
        return []

    if not first_page:
        raise RuntimeError("Finviz reports results but no tickers were parsed from page 1")

    all_tickers: list[str] = []
    for ticker in first_page:
        if ticker not in all_tickers:
            all_tickers.append(ticker)

    for start_row in range(PAGE_SIZE + 1, total + 1, PAGE_SIZE):
        time.sleep(2)
        html = request_page(session, start_row)
        page_tickers = extract_tickers(html)
        print(f"Page r={start_row}: {len(page_tickers)} ticker(s).")

        if not page_tickers:
            raise RuntimeError(f"No tickers parsed from expected Finviz page r={start_row}")

        for ticker in page_tickers:
            if ticker not in all_tickers:
                all_tickers.append(ticker)

    if len(all_tickers) != total:
        raise RuntimeError(
            f"Safety check failed: Finviz says {total} results, but parsed "
            f"{len(all_tickers)} unique tickers. ticki.txt will NOT be overwritten."
        )

    return all_tickers


def write_tickers(path: Path, tickers: list[str]) -> None:
    content = "\n".join(tickers)
    if content:
        content += "\n"
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"Saved {len(tickers)} ticker(s) to {path}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Update ticki.txt from the configured Finviz screener")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately without checking whether it is 70 minutes before NASDAQ close",
    )
    parser.add_argument(
        "--output",
        default="ticki.txt",
        help="Output file (default: ticki.txt)",
    )
    args = parser.parse_args()

    if not should_run_now(args.force):
        return 0

    try:
        tickers = fetch_all_tickers()
        write_tickers(Path(args.output), tickers)
        print("Tickers:")
        for ticker in tickers:
            print(ticker)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
