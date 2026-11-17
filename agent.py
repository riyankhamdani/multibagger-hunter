import os
import json
import time
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
# Catatan: SOFI (fintech bunga) & PLTR (kontrak militer) rawan gagal skrining syariah ketat,
# sesuaikan daftar ini jika ingin 100% compliant.
US_WATCHLIST = [
    "PLTR", "RKLB", "ASTS", "LUNR", "HIMS", "IONQ",
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
            "ticker": "LUNR",
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


def is_recently_alerted(ticker, seen_data, cooldown_hours=72):
    last_ts = seen_data.get(ticker)
    if not last_ts:
        return False
    return (time.time() - last_ts) < (cooldown_hours * 3600)


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
            hist = stock.history(period="2mo")
            if hist.empty or len(hist) < 21:
                continue

            last_close = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2])
            price_5d_ago = float(hist["Close"].iloc[-6])
            sma20 = float(hist["Close"].tail(20).mean())

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

            # 3. Filter Anti-Pisau Jatuh & Anti-Pucuk:
            # - Wajib candle hijau (last_close > prev_close) agar vol_spike bukan panic selling
            # - Wajib di atas SMA20 (uptrend sehat)
            # - Kenaikan 5 hari terakhir maksimal +12% (belum FOMO pucuk)
            pct_5d = ((last_close / price_5d_ago) - 1) * 100
            if last_close <= prev_close or last_close < sma20 or not (-1.0 <= pct_5d <= 12.0):
                continue

            # 4. Deteksi Lonjakan Volume Akumulasi (>= 1.2x rata-rata)
            avg_vol = float(hist["Volume"].iloc[-21:-1].mean())
            last_vol = float(hist["Volume"].iloc[-1])
            vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0
            if vol_ratio < 1.2:
                continue

            candidates.append({
                "asset_type": "US_STOCK",
                "ticker": ticker,
                "name": info.get("shortName", ticker),
                "price": round(price, 2),
                "sma20": round(sma20, 2),
                "pct_5d": round(pct_5d, 1),
                "rev_growth_pct": round(rev_growth * 100, 1),
                "gross_margin_pct": round(gross_margin * 100, 1),
                "debt_to_mcap_pct": round(debt_ratio * 100, 1),
                "vol_spike": round(vol_ratio, 2),
                "cl_price": round(price * 0.93, 2),         # Cut loss ketat -7%
                "be_trigger": round(price * 1.10, 2),       # +10% -> geser SL ke harga modal (0% rugi)
                "tp1_price": round(price * 1.25, 2),        # TP1 +25% (Amankan profit)
                "tp2_price": round(price * 1.60, 2),        # TP2 +60% (Moonbag)
            })
        except Exception as e:
            print(f"[WARN] Gagal scan US {ticker}: {e}")

    candidates.sort(key=lambda x: (x["rev_growth_pct"] * x["vol_spike"]), reverse=True)
    return candidates[:2]


def screen_crypto_spot():
    candidates = []
    for ticker in CRYPTO_WATCHLIST:
        try:
            coin = yf.Ticker(ticker)
            hist = coin.history(period="21d")
            if hist.empty or len(hist) < 15:
                continue

            price = float(hist["Close"].iloc[-1])
            prev_price = float(hist["Close"].iloc[-2])
            price_7d_ago = float(hist["Close"].iloc[-7])
            sma14 = float(hist["Close"].tail(14).mean())

            pct_24h = ((price / prev_price) - 1) * 100
            pct_7d = ((price / price_7d_ago) - 1) * 100

            avg_vol = float(hist["Volume"].iloc[-15:-1].mean())
            last_vol = float(hist["Volume"].iloc[-1])
            vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

            # Wajib hijau 24 jam, di atas SMA14, volume >= 1.5x, dan belum naik > 18% dalam 7 hari
            if price > prev_price and price >= sma14 and vol_ratio >= 1.5 and 0 <= pct_7d <= 18:
                clean_symbol = "".join([c for c in ticker.split("-")[0] if not c.isdigit()])
                candidates.append({
                    "asset_type": "CRYPTO_SPOT",
                    "ticker": ticker,
                    "symbol": clean_symbol,
                    "price": round(price, 4),
                    "pct_24h": round(pct_24h, 1),
                    "pct_7d": round(pct_7d, 1),
                    "vol_spike": round(vol_ratio, 2),
                    "cl_price": round(price * 0.92, 4),     # Cut loss ketat -8%
                    "be_trigger": round(price * 1.12, 4),   # +12% -> geser SL ke harga modal
                    "tp1_price": round(price * 1.30, 4),    # TP1 +30%
                    "tp2_price": round(price * 1.80, 4),    # TP2 +80%
                })
        except Exception as e:
            print(f"[WARN] Gagal scan Crypto {ticker}: {e}")

    candidates.sort(key=lambda x: x["vol_spike"], reverse=True)
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


def analyze_with_gemini(top_us, top_crypto):
    client = genai.Client(api_key=GEMINI_API_KEY)

    context_blocks = []
    for item in top_us:
        news = fetch_catalyst_news(f"{item['ticker']} stock news earnings contract catalyst")
        context_blocks.append(f"DATA SAHAM US:\n{json.dumps(item)}\nBERITA TERBARU:\n{news}\n")

    for item in top_crypto:
        news = fetch_catalyst_news(f"{item['symbol']} crypto token news partnership mainnet")
        context_blocks.append(f"DATA CRYPTO SPOT:\n{json.dumps(item)}\nBERITA TERBARU:\n{news}\n")

    prompt = f"""
Kamu adalah analis investasi defensif (prioritas utama: proteksi modal & anti-FOMO) untuk aplikasi Ajaib.

Evaluasi kandidat berikut yang sudah lolos filter tren naik & volume akumulasi hijau. Pilih MAKSIMAL 1 kandidat terbaik HANYA JIKA ada katalis fundamental/berita yang benar-benar valid dan bukan sekadar hype sesaat. Jika meragukan, wajib jawab "HOLD / WAIT — Belum ada setup probabilitas tinggi hari ini, simpan modal di RDPU/Cash."

Data Kandidat:
{''.join(context_blocks)}

Aturan format output Telegram:
- Gunakan murni tag HTML <b>...</b> dan <code>...</code>. DILARANG KERAS memakai tanda bintang ** atau markdown.
- Sebutkan Ticker, Harga Saat Ini, dan Katalis Utama (maksimal 2 kalimat).
- Cantumkan level eksekusi:
  • Entry Limit Order (jangan kejar harga di atas harga saat ini)
  • Cut Loss (-7% US / -8% Crypto)
  • Trigger Anti-Rugi (saat profit +10%, wajib geser SL ke harga beli)
  • TP 1 (+25% / +30% - amankan profit) & TP 2 (Moonbag)
"""

    for attempt in range(1, 4):
        try:
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt
            )
            return response.text
        except Exception as e:
            print(f"[WARN] Gemini percobaan {attempt}/3 gagal: {e}")
            if attempt < 3:
                time.sleep(15)
            else:
                raise


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()


def main():
    seen = load_json(SEEN_FILE, {})
    portfolio = load_portfolio()

    status_messages = []
    us_hits = []
    crypto_hits = []

    # Cek Slot 1 (Saham US): Jika sedang pegang LUNR, kunci sinyal beli baru
    us_lock_msg = check_active_slot(portfolio["slot_1_us"], "SLOT 1 (SAHAM US)", max_loss_pct=7.0)
    if us_lock_msg:
        status_messages.append(us_lock_msg)
    else:
        us_hits = [x for x in screen_us_stocks() if not is_recently_alerted(x["ticker"], seen)]

    # Cek Slot 2 (Crypto Spot): Jika kosong, baru scan kandidat baru
    crypto_lock_msg = check_active_slot(portfolio["slot_2_crypto"], "SLOT 2 (CRYPTO)", max_loss_pct=8.0)
    if crypto_lock_msg:
        status_messages.append(crypto_lock_msg)
    else:
        crypto_hits = [x for x in screen_crypto_spot() if not is_recently_alerted(x["ticker"], seen)]

    if not us_hits and not crypto_hits and not status_messages:
        print("Tidak ada sinyal baru & semua slot kosong (parkir di RDPU).")
        return

    body_parts = []
    if status_messages:
        body_parts.append("\n\n".join(status_messages))

    if us_hits or crypto_hits:
        analysis = analyze_with_gemini(us_hits, crypto_hits)
        if analysis:
            body_parts.append(analysis)
            for item in us_hits + crypto_hits:
                seen[item["ticker"]] = time.time()
            save_json(SEEN_FILE, seen)
    else:
        body_parts.append("💤 <b>RADAR PELUANG BARU:</b> HOLD / WAIT — Tidak ada kandidat aman atau slot masih terkunci.")

    wib = timezone(timedelta(hours=7))
    now_wib = datetime.now(wib).strftime("%d %b %Y %H:%M WIB")
    header = f"🛡️ <b>DEFENSIVE RADAR (AJAIB)</b>\n📅 {now_wib}\n\n"
    send_telegram(header + "\n\n---\n\n".join(body_parts))
    print("Laporan berhasil dikirim ke Telegram.")


if __name__ == "__main__":
    main()
