# finviz-tickers

Automatyczna aktualizacja `ticki.txt` na podstawie publicznego screenera Finviz:

`https://finviz.com/screener?v=111&f=exch_nasd,sh_price_u10,ta_change_u,ta_highlow20d_nh,ta_highlow50d_nh,ta_highlow52w_nh,ta_perf_dup&ft=4`

## Jak to działa

- GitHub Actions uruchamia kontrolę w dni robocze w możliwych godzinach odpowiadających 70 minutom przed normalnym lub skróconym zamknięciem NASDAQ.
- `pandas_market_calendars` sprawdza rzeczywisty kalendarz sesji NASDAQ, w tym dni wolne i skrócone sesje.
- Skrypt pobiera pierwszą stronę Finviz i odczytuje liczbę `Total`.
- Jeśli wyników jest więcej niż 20, pobiera następne strony przez `r=21`, `r=41`, `r=61` itd.
- Usuwa duplikaty, zachowując kolejność Finviz.
- Przed zapisem sprawdza, czy liczba unikalnych tickerów jest dokładnie równa wartości `Total` z Finviz.
- Jeśli pobieranie jest niepełne albo Finviz zwróci stronę blokady, `ticki.txt` nie jest nadpisywany.
- Gdy `ticki.txt` się zmieni, GitHub Actions automatycznie wykonuje commit i push do `main`.

## Pliki

- `finviz_tickers.py` – pobieranie, paginacja, walidacja i kontrola czasu sesji.
- `requirements.txt` – zależności Pythona.
- `.github/workflows/update-tickers.yml` – harmonogram GitHub Actions.
- `ticki.txt` – aktualna lista tickerów, jeden symbol na linię.

## Uruchomienie ręczne

W GitHub przejdź do **Actions → Update Finviz tickers → Run workflow**. Uruchomienie ręczne pomija kontrolę godziny i od razu próbuje odświeżyć `ticki.txt`.
