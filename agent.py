import os
import re
import json
import time
import pandas as pd
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta
from google import genai

# --- KONFIGURASI API ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen_alerts.json"
PORTFOLIO_FILE = "portfolio_state.json"

# --- WATCHLIST TERKURASI ---
# PLTR & SOFI dikomentari agar selaras dengan skrining syariah ketat
US_WATCHLIST = [
    "RKLB", "ASTS", "LUNR", "HIMS", "IONQ",
    "ENVX", "SMCI", "ARM", "CELH", "DUOL", "CRWD", "NET", "PATH"
]

CRYPTO_WATCHLIST = [
    "SUI20947-USD", "TAO22974-USD", "RENDER-USD", "ONDO-USD",
    "NEAR-USD", "AVAX-USD", "LINK-USD", "INJ-USD", "TIA-USD", "AKT-USD"
]


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_portfolio():
    # Ubah "ticker" ke None jika slot sudah kosong / selesai TP / Cut Loss
    default_state = {
        "slot_1_us": {
            "ticker": None,
            "entry_price": None,  # Isi harga belimu (misal 11.50) agar bot hitung P&L otomatis
            "modal_rp": 100000
        },
        "slot_2_crypto": {
            "ticker": None,
            "entry_price": None,
            "modal_rp": 3000000
        }
    }
    if not os.path.exists(PORTFOLIO_FILE):
        save_json(PORTFOLIO_FILE, default_state)
        return default_state
    return load_json(PORTFOLIO_FILE, default_state)


def is_recently_alerted(ticker, seen_data, cooldown_hours=120):
    last_ts = seen_data.get(ticker)
    if not last_ts:
        return False
    return (time.time() - last_ts) < (cooldown_hours * 3600)


def compute_indicators(hist: pd.DataFrame) -> dict:
    """Menghitung SMA20, SMA50, RSI(14), dan ATR(14) murni dengan pandas."""
    close = hist["Close"]
    high = hist["High"]
    low = hist["Low"]

    sma20 = float(close.tail(20).mean())
    sma50 = float(close.tail(50).mean())

    # ATR 14
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = float(tr.rolling(14).mean().iloc[-1])

    # RSI 14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, 1e-9)
    rsi14 = float((100 - (100 / (1 + rs))).iloc[-1])

    return {
        "sma20": sma20,
        "sma50": sma50,
        "atr14": atr14,
        "rsi14": rsi14,
    }


def check_market_regime(benchmark_ticker: str) -> tuple[bool, str]:
    """Rem otomatis: hanya boleh entry jika pasar induk di atas SMA20."""
    try:
        hist = yf.Ticker(benchmark_ticker).history(period="2mo")
        if hist.empty or len(hist) < 20:
            return True, "Data indeks tidak cukup (lanjut hati-hati)"
        last_close = float(hist["Close"].iloc[-1])
        sma20 = float(hist["Close"].tail(20).mean())
        is_bullish = last_close >= sma20
        status = (
            f"BULLISH ({benchmark_ticker} ${last_close:.2f} >= SMA20 ${sma20:.2f})"
            if is_bullish
            else f"RISK-OFF ({benchmark_ticker} ${last_close:.2f} < SMA20 ${sma20:.2f})"
        )
        return is_bullish, status
    except Exception as e:
        return True, f"Gagal cek {benchmark_ticker}: {e}"


def check_active_slot(slot_data, label, max_loss_pct):
    ticker = slot_data.get("ticker")
    if not ticker:
        return None

    try:
        hist = yf.Ticker(ticker).history(period="5d")
        curr_price = round(float(hist["Close"].iloc[-1]), 4)
    except Exception:
        curr_price = None

    entry = slot_data.get("entry_price")
    if entry and curr_price:
        pnl_pct = ((curr_price / entry) - 1) * 100
        if pnl_pct <= -max_loss_pct:
            status = f"🚨 <b>KENA BATAS CUT LOSS ({pnl_pct:.1f}%)</b> — Disarankan keluar & kosongkan slot."
        elif pnl_pct >= 10.0:
            status = f"🛡️ <b>PROFIT +{pnl_pct:.1f}% (MODE ANTI-RUGI)</b> — Geser Stop Loss ke harga modal (${entry})!"
        else:
            status = f"🔒 <b>HOLD ({pnl_pct:+.1f}%)</b> — Slot terkunci, dilarang buka posisi baru."
        return f"<b>{label}: {ticker}</b> | Harga: ${curr_price} (Entry: ${entry})\n{status}"

    price_info = f"${curr_price}" if curr_price else "N/A"
    return (
        f"<b>{label}: {ticker}</b> (Harga: {price_info})\n"
        f"🔒 <b>SLOT TERKUNCI (HOLD / WAIT)</b> — Tuntaskan posisi aktif dulu sebelum masuk koin/saham baru."
    )


def screen_us_stocks():
    candidates = []
    for ticker in US_WATCHLIST:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            hist = stock.history(period="3mo")
            if hist.empty or len(hist) < 50:
                continue

            last_close = float(hist["Close"].iloc[-1])
            last_high = float(hist["High"].iloc[-1])
            last_low = float(hist["Low"].iloc[-1])
            price = float(info.get("currentPrice") or last_close)

            mcap = info.get("marketCap") or 0
            total_debt = info.get("totalDebt") or 0
            rev_growth = info.get("revenueGrowth") or 0
            gross_margin = info.get("grossMargins") or 0

            # 1. Filter Syariah AAOIFI: Debt-to-MarketCap < 30%
            debt_ratio = (total_debt / mcap) if mcap > 0 else 1.0
            if debt_ratio >= 0.30:
                continue

            # 2. Filter Fundamental: Revenue growth >= 20% YoY
            if rev_growth < 0.20:
                continue

            ind = compute_indicators(hist)
            sma20, sma50, atr14, rsi14 = ind["sma20"], ind["sma50"], ind["atr14"], ind["rsi14"]

            # 3. Struktur Tren Naik Sehat: Harga > SMA50 dan SMA20 > SMA50
            if not (last_close > sma50 and sma20 > sma50):
                continue

            # 4. Zona Beli Pullback (Anti-Pucuk):
            # Harga sedang koreksi menempel SMA20 (-1.5% s/d +3.5% dari SMA20) & RSI sehat (42 - 58)
            dist_from_sma20_pct = ((last_close / sma20) - 1) * 100
            if not (-1.5 <= dist_from_sma20_pct <= 3.5) or not (42.0 <= rsi14 <= 58.0):
                continue

            # 5. Konfirmasi Pembeli Menahan Support (Close di separuh atas range harian)
            day_range = last_high - last_low
            close_position = ((last_close - last_low) / day_range) if day_range > 0 else 0.5
            if close_position < 0.45:
                continue

            # Level Eksekusi Limit Order & Stop Loss Dinamis (1.8x ATR, batas -8% s/d -12%)
            limit_entry = round(min(price, max(sma20, price - 0.25 * atr14)), 2)
            raw_sl = limit_entry - (1.8 * atr14)
            sl_price = round(max(limit_entry * 0.88, min(limit_entry * 0.92, raw_sl)), 2)
            sl_pct = round(((sl_price / limit_entry) - 1) * 100, 1)

            candidates.append({
                "asset_type": "US_STOCK",
                "ticker": ticker,
                "name": info.get("shortName", ticker),
                "current_price": round(price, 2),
                "limit_entry": limit_entry,
                "sma20": round(sma20, 2),
                "sma50": round(sma50, 2),
                "rsi14": round(rsi14, 1),
                "atr14": round(atr14, 2),
                "dist_sma20_pct": round(dist_from_sma20_pct, 1),
                "rev_growth_pct": round(rev_growth * 100, 1),
                "gross_margin_pct": round(gross_margin * 100, 1),
                "debt_to_mcap_pct": round(debt_ratio * 100, 1),
                "cl_price": sl_price,
                "cl_pct": sl_pct,
                "be_trigger": round(limit_entry + (1.5 * atr14), 2),
                "tp1_price": round(limit_entry + (2.5 * atr14), 2),
                "tp2_price": round(limit_entry + (4.0 * atr14), 2),
            })
        except Exception as e:
            print(f"[WARN] Gagal scan US {ticker}: {e}")

    # Prioritaskan yang paling dekat dengan SMA20 & pertumbuhan tertinggi
    candidates.sort(key=lambda x: (x["rev_growth_pct"] / (abs(x["dist_sma20_pct"]) + 1.0)), reverse=True)
    return candidates[:2]


def screen_crypto_spot():
    candidates = []
    for ticker in CRYPTO_WATCHLIST:
        try:
            coin = yf.Ticker(ticker)
            hist = coin.history(period="3mo")
            if hist.empty or len(hist) < 50:
                continue

            price = float(hist["Close"].iloc[-1])
            last_high = float(hist["High"].iloc[-1])
            last_low = float(hist["Low"].iloc[-1])

            ind = compute_indicators(hist)
            sma20, sma50, atr14, rsi14 = ind["sma20"], ind["sma50"], ind["atr14"], ind["rsi14"]

            # 1. Struktur Tren Naik Sehat
            if not (price > sma50 and sma20 > sma50):
                continue

            # 2. Zona Beli Pullback di dekat SMA20 (-2% s/d +4%) & RSI 42 - 58
            dist_from_sma20_pct = ((price / sma20) - 1) * 100
            if not (-2.0 <= dist_from_sma20_pct <= 4.0) or not (42.0 <= rsi14 <= 58.0):
                continue

            # 3. Konfirmasi Pantulan Pembeli
            day_range = last_high - last_low
            close_position = ((price - last_low) / day_range) if day_range > 0 else 0.5
            if close_position < 0.45:
                continue

            limit_entry = round(min(price, max(sma20, price - 0.25 * atr14)), 4)
            raw_sl = limit_entry - (1.8 * atr14)
            sl_price = round(max(limit_entry * 0.85, min(limit_entry * 0.90, raw_sl)), 4)
            sl_pct = round(((sl_price / limit_entry) - 1) * 100, 1)

            clean_symbol = "".join([c for c in ticker.split("-")[0] if not c.isdigit()])
            candidates.append({
                "asset_type": "CRYPTO_SPOT",
                "ticker": ticker,
                "symbol": clean_symbol,
                "current_price": round(price, 4),
                "limit_entry": limit_entry,
                "sma20": round(sma20, 4),
                "rsi14": round(rsi14, 1),
                "atr14": round(atr14, 4),
                "dist_sma20_pct": round(dist_from_sma20_pct, 1),
                "cl_price": sl_price,
                "cl_pct": sl_pct,
                "be_trigger": round(limit_entry + (1.5 * atr14), 4),
                "tp1_price": round(limit_entry + (2.5 * atr14), 4),
                "tp2_price": round(limit_entry + (4.0 * atr14), 4),
            })
        except Exception as e:
            print(f"[WARN] Gagal scan Crypto {ticker}: {e}")

    candidates.sort(key=lambda x: abs(x["dist_sma20_pct"]))
    return candidates[:2]


def fetch_catalyst_news(query):
    try:
        res = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": 3,
                "days": 5
            },
            timeout=15
        )
        results = res.json().get("results", [])
        return "\n".join([f"- {r['title']}: {r['content'][:220]}" for r in results])
    except Exception as e:
        return f"Tidak ada berita terbaru ({e})"


def analyze_with_gemini(top_us, top_crypto, market_notes):
    client = genai.Client(api_key=GEMINI_API_KEY)

    context_blocks = [f"KONDISI PASAR INDUK:\n{market_notes}\n\n"]
    for item in top_us:
        news = fetch_catalyst_news(f"{item['ticker']} stock news earnings contract catalyst")
        context_blocks.append(f"DATA SAHAM US (SETUP PULLBACK SMA20):\n{json.dumps(item)}\nBERITA TERBARU:\n{news}\n")

    for item in top_crypto:
        news = fetch_catalyst_news(f"{item['symbol']} crypto token news partnership mainnet")
        context_blocks.append(f"DATA CRYPTO SPOT (SETUP PULLBACK SMA20):\n{json.dumps(item)}\nBERITA TERBARU:\n{news}\n")

    prompt = f"""
Kamu adalah analis swing-trading defensif (fokus utama: win rate tinggi dengan membeli di area pullback support SMA20, BUKAN mengejar pucuk) untuk aplikasi Ajaib.

Evaluasi kandidat berikut yang sudah lolos filter tren naik (`Harga > SMA50`) dan koreksi sehat ke area `SMA20` (`RSI 42-58`). Pilih MAKSIMAL 1 kandidat terbaik HANYA JIKA tidak ada berita negatif/dilusi/hack yang merusak tren. Jika ada risiko negatif, wajib jawab:
"HOLD / WAIT — Belum ada setup pullback aman hari ini, simpan modal di RDPU/Cash."

Data Kandidat:
{''.join(context_blocks)}

Aturan format output Telegram:
- Gunakan murni tag HTML <b>...</b> dan <code>...</code>. DILARANG KERAS memakai tanda bintang ** atau markdown.
- Hindari karakter '<' atau '>' mentah di luar tag HTML.
- Sebutkan Ticker, Harga Sekarang, Posisi RSI & Jarak ke SMA20, serta Ringkasan Katalis (maksimal 2 kalimat).
- Cantumkan Rencana Order (wajib pakai harga dari data):
  - <b>Entry Limit Order:</b> <code>$...</code> (pasang antrean beli, JANGAN market buy di jam 21.30–22.15 WIB saat pasar US baru buka)
  - <b>Stop Loss ATR:</b> <code>$...</code> (sekitar <code>...%</code>, memberi ruang napas volatilitas)
  - <b>Trigger Anti-Rugi:</b> Geser SL ke harga beli saat menyentuh <code>$...</code>
  - <b>TP 1 (Amankan Profit):</b> <code>$...</code> | <b>TP 2 (Moonbag):</b> <code>$...</code>
"""

    models_to_try = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"]
    for model_name in models_to_try:
        for attempt in range(1, 3):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                if response and response.text:
                    return response.text
            except Exception as e:
                print(f"[WARN] {model_name} percobaan {attempt}/2 gagal: {e}")
                if "404" in str(e) or "NOT_FOUND" in str(e):
                    break
                time.sleep(10 * attempt)

    raise Exception("Seluruh kandidat model Gemini gagal merespons.")


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload, timeout=15)
    if r.status_code != 200:
        print(f"[WARN] Gagal kirim HTML ({r.text[:120]}), fallback teks biasa...")
        payload.pop("parse_mode")
        payload["text"] = re.sub(r"<[^>]+>", "", message)
        r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def main():
    seen = load_json(SEEN_FILE, {})
    portfolio = load_portfolio()

    us_bullish, qqq_status = check_market_regime("QQQ")
    crypto_bullish, btc_status = check_market_regime("BTC-USD")
    market_notes = f"• US Market: {qqq_status}\n• Crypto Market: {btc_status}"

    status_messages = [f"📊 <b>KONDISI PASAR:</b>\n{market_notes}"]
    us_hits = []
    crypto_hits = []

    # Cek Slot 1 (Saham US)
    us_lock_msg = check_active_slot(portfolio["slot_1_us"], "SLOT 1 (SAHAM US)", max_loss_pct=10.0)
    if us_lock_msg:
        status_messages.append(us_lock_msg)
    elif us_bullish:
        us_hits = [x for x in screen_us_stocks() if not is_recently_alerted(x["ticker"], seen)]

    # Cek Slot 2 (Crypto Spot)
    crypto_lock_msg = check_active_slot(portfolio["slot_2_crypto"], "SLOT 2 (CRYPTO)", max_loss_pct=12.0)
    if crypto_lock_msg:
        status_messages.append(crypto_lock_msg)
    elif crypto_bullish:
        crypto_hits = [x for x in screen_crypto_spot() if not is_recently_alerted(x["ticker"], seen)]

    if not us_hits and not crypto_hits and len(status_messages) == 1:
        print(f"Tidak ada setup pullback aman hari ini. {qqq_status} | {btc_status}")
        return

    body_parts = ["\n\n".join(status_messages)]

    if us_hits or crypto_hits:
        analysis = analyze_with_gemini(us_hits, crypto_hits, market_notes)
        if analysis:
            body_parts.append(analysis)
            for item in us_hits + crypto_hits:
                seen[item["ticker"]] = time.time()
            save_json(SEEN_FILE, seen)
    else:
        body_parts.append("💤 <b>RADAR PELUANG BARU:</b> HOLD / WAIT — Pasar risk-off atau belum ada harga yang menyentuh area support SMA20.")

    wib = timezone(timedelta(hours=7))
    now_wib = datetime.now(wib).strftime("%d %b %Y %H:%M WIB")
    header = f"🛡️ <b>PULLBACK SNIPER RADAR (AJAIB)</b>\n📅 {now_wib}\n\n"
    send_telegram(header + "\n\n---\n\n".join(body_parts))
    print("Laporan berhasil dikirim ke Telegram.")


if __name__ == "__main__":
    main()
