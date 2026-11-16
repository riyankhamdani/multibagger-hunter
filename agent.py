import os
import json
import time
import requests
import yfinance as yf
from datetime import datetime, timezone
from google import genai

# --- KONFIGURASI API ---
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TAVILY_API_KEY = os.environ["TAVILY_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SEEN_FILE = "seen_alerts.json"

# --- WATCHLIST TERKURASI (TERSEDIA DI AJAIB & LOLOS SEKTOR HALAL) ---
US_WATCHLIST = [
    "PLTR", "SOFI", "RKLB", "ASTS", "LUNR", "HIMS", "IONQ",
    "ENVX", "SMCI", "ARM", "CELH", "DUOL", "CRWD", "NET", "PATH"
]

CRYPTO_WATCHLIST = [
    "SUI20947-USD", "TAO22974-USD", "RENDER-USD", "ONDO-USD",
    "NEAR-USD", "AVAX-USD", "LINK-USD", "INJ-USD", "TIA-USD", "AKT-USD"
]


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return {}
    with open(SEEN_FILE, "r") as f:
        return json.load(f)


def save_seen(data):
    with open(SEEN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def is_recently_alerted(ticker, seen_data, cooldown_hours=72):
    last_ts = seen_data.get(ticker)
    if not last_ts:
        return False
    return (time.time() - last_ts) < (cooldown_hours * 3600)


def screen_us_stocks():
    candidates = []
    for ticker in US_WATCHLIST:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            hist = stock.history(period="1mo")
            if hist.empty or len(hist) < 10:
                continue

            price = info.get("currentPrice") or hist["Close"].iloc[-1]
            mcap = info.get("marketCap") or 0
            total_debt = info.get("totalDebt") or 0
            rev_growth = info.get("revenueGrowth") or 0
            gross_margin = info.get("grossMargins") or 0

            # 1. Filter Syariah AAOIFI: Debt-to-MarketCap < 30%
            debt_ratio = (total_debt / mcap) if mcap > 0 else 1.0
            if debt_ratio >= 0.30:
                continue

            # 2. Filter Pertumbuhan Multibagger: Revenue growth >= 20% YoY
            if rev_growth < 0.20:
                continue

            # 3. Deteksi Lonjakan Volume (Akumulasi awal)
            avg_vol = hist["Volume"].iloc[:-1].mean()
            last_vol = hist["Volume"].iloc[-1]
            vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

            candidates.append({
                "asset_type": "US_STOCK",
                "ticker": ticker,
                "name": info.get("shortName", ticker),
                "price": round(float(price), 2),
                "mcap_b": round(mcap / 1e9, 2),
                "rev_growth_pct": round(rev_growth * 100, 1),
                "gross_margin_pct": round(gross_margin * 100, 1),
                "debt_to_mcap_pct": round(debt_ratio * 100, 1),
                "vol_spike": round(float(vol_ratio), 2),
                "cl_price": round(float(price) * 0.88, 2),       # Cut loss -12%
                "tp1_price": round(float(price) * 2.00, 2),      # TP1 +100% (Tarik modal)
                "tp2_price": round(float(price) * 3.00, 2),      # TP2 +200%
            })
        except Exception as e:
            print(f"[WARN] Gagal scan US {ticker}: {e}")

    candidates.sort(key=lambda x: (x["rev_growth_pct"] * x["vol_spike"]), reverse=True)
    return candidates[:3]


def screen_crypto_spot():
    candidates = []
    for ticker in CRYPTO_WATCHLIST:
        try:
            coin = yf.Ticker(ticker)
            hist = coin.history(period="14d")
            if hist.empty or len(hist) < 8:
                continue

            price = hist["Close"].iloc[-1]
            prev_price = hist["Close"].iloc[-2]
            price_7d_ago = hist["Close"].iloc[-7]

            pct_24h = ((price / prev_price) - 1) * 100
            pct_7d = ((price / price_7d_ago) - 1) * 100

            avg_vol = hist["Volume"].iloc[:-1].mean()
            last_vol = hist["Volume"].iloc[-1]
            vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0

            # Filter Momentum Sehat: Volume naik >= 1.5x rata-rata, harga belum FOMO ekstrem
            if vol_ratio >= 1.5 and -5 <= pct_7d <= 35:
                clean_symbol = ticker.split("-")[0]
                clean_symbol = "".join([c for c in clean_symbol if not c.isdigit()])
                candidates.append({
                    "asset_type": "CRYPTO_SPOT",
                    "ticker": ticker,
                    "symbol": clean_symbol,
                    "price": round(float(price), 4),
                    "pct_24h": round(float(pct_24h), 1),
                    "pct_7d": round(float(pct_7d), 1),
                    "vol_spike": round(float(vol_ratio), 2),
                    "cl_price": round(float(price) * 0.85, 4),   # Cut loss -15%
                    "tp1_price": round(float(price) * 2.00, 4),  # TP1 +100% (Tarik modal)
                    "tp2_price": round(float(price) * 3.50, 4),  # TP2 +250%
                })
        except Exception as e:
            print(f"[WARN] Gagal scan Crypto {ticker}: {e}")

    candidates.sort(key=lambda x: x["vol_spike"], reverse=True)
    return candidates[:3]


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

    if not context_blocks:
        return None

    prompt = f"""
Kamu adalah analis investasi syariah & pemburu multibagger untuk portofolio modal Rp8,9 juta di aplikasi Ajaib (Slot 1: Saham US Rp4jt, Slot 2: Crypto Spot Rp3jt, Slot 3: Cash/RDPU Rp1,9jt).

Berdasarkan data kuantitatif dan berita terbaru berikut, pilih MAKSIMAL 1 Saham US Halal terbaik dan MAKSIMAL 1 Crypto Spot terbaik yang memiliki katalis paling kuat hari ini (bukan sekadar noise). Jika tidak ada yang layak masuk, katakan "HOLD / WAIT".

Data Kandidat:
{''.join(context_blocks)}

Format output wajib singkat, padat, berbahasa Indonesia untuk Telegram (gunakan HTML bold <b>...</b> dan code <code>...</code>, JANGAN pakai markdown **):
1. Sebutkan Ticker, Harga Saat Ini, dan Alasan Katalis Utama (maksimal 2 kalimat).
2. Tampilkan angka eksekusi Ajaib:
   - Entry Ideal (Limit Order)
   - Cut Loss / Invalidation
   - TP 1 (+100% - Tarik Modal Awal)
   - TP 2 (Moonbag)
3. Ingatkan 1 kalimat manajemen risiko agar tetap disiplin.
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
    seen = load_seen()
    us_hits = [x for x in screen_us_stocks() if not is_recently_alerted(x["ticker"], seen)]
    crypto_hits = [x for x in screen_crypto_spot() if not is_recently_alerted(x["ticker"], seen)]

    if not us_hits and not crypto_hits:
        print("Tidak ada sinyal baru hari ini (aman / cooldown aktif).")
        return

    analysis = analyze_with_gemini(us_hits[:2], crypto_hits[:2])
    if not analysis:
        return

    now_wib = datetime.now(timezone.utc).strftime("%d %b %Y")
    header = f"🎯 <b>MULTIBAGGER RADAR (AJAIB SYARIAH)</b>\n📅 {now_wib}\n\n"
    send_telegram(header + analysis)

    for item in us_hits[:2] + crypto_hits[:2]:
        seen[item["ticker"]] = time.time()
    save_seen(seen)
    print("Alert berhasil dikirim ke Telegram.")


if __name__ == "__main__":
    main()
