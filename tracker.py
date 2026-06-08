import json
import os
import re
from datetime import datetime
from pathlib import Path

import httpx

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")
PRICES_FILE = Path("prices.json")
TARGET_URL = "https://bigbox.ee/search?text=sensai&resultsPerPage=100"


def fetch_html():
    """Загружает HTML через ScraperAPI (обходит Cloudflare) или напрямую."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.8",
    }

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        # Метод 1: ScraperAPI (если ключ задан)
        if SCRAPER_API_KEY:
            try:
                resp = client.get(
                    "http://api.scraperapi.com",
                    params={"api_key": SCRAPER_API_KEY, "url": TARGET_URL, "render": "true"},
                    timeout=60,
                )
                if resp.status_code == 200:
                    print(f"ScraperAPI: OK, {len(resp.text)} байт")
                    return resp.text
                print(f"ScraperAPI статус: {resp.status_code}")
            except Exception as e:
                print(f"ScraperAPI ошибка: {e}")

        # Метод 2: Прямой запрос
        try:
            client.get("https://bigbox.ee/", timeout=10)  # cookies
            resp = client.get(TARGET_URL, headers=headers)
            print(f"Прямой запрос: {resp.status_code}, {len(resp.text)} байт")
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            print(f"Прямой запрос ошибка: {e}")

    return None


def parse_products(html):
    products = {}
    if not html:
        return products

    sensai_found = "sensai" in html.lower()
    print(f"Sensai упоминается на странице: {sensai_found}")

    # JSON-LD
    jsonld_matches = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    for raw in jsonld_matches:
        try:
            data = json.loads(raw.strip())
            items = []
            if isinstance(data, dict):
                if data.get("@type") == "ItemList":
                    items = data.get("itemListElement", [])
                elif data.get("@type") == "Product":
                    items = [data]
            elif isinstance(data, list):
                items = data
            for item in items:
                if item.get("@type") == "ListItem":
                    item = item.get("item", {})
                if item.get("@type") != "Product":
                    continue
                name = item.get("name", "")
                if "sensai" not in name.lower():
                    continue
                offers = item.get("offers", {})
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price")
                url = item.get("url", "") or offers.get("url", "")
                if price and name:
                    products[name] = {"price": float(price), "link": url}
                    print(f"  JSON-LD: {name} — {price}€")
        except Exception:
            continue

    # Regex fallback
    if not products and sensai_found:
        patterns = [
            r'"name"\s*:\s*"([^"]*[Ss]ensai[^"]*)"[^}]{0,400}?"price"\s*:\s*"?([\d.]+)',
            r'data-product-name="([^"]*[Ss]ensai[^"]*)"[^>]*data-product-price="([\d.]+)"',
            r'itemprop="name"[^>]*>\s*([^<]*[Ss]ensai[^<]*)<[^"]{0,300}itemprop="price"[^>]*content="([\d.]+)"',
            r'"productName"\s*:\s*"([^"]*[Ss]ensai[^"]*)"[^}]{0,200}"finalPrice"\s*:\s*([\d.]+)',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)
            for name, price in matches:
                name = re.sub(r'\s+', ' ', name).strip()[:100]
                if name and name not in products:
                    products[name] = {"price": float(price), "link": TARGET_URL}
                    print(f"  Regex: {name} — {price}€")

        if not products:
            # Показываем контекст вокруг Sensai для отладки
            blocks = re.findall(r'.{0,50}[Ss]ensai.{0,100}', html)
            for b in blocks[:5]:
                print(f"  Контекст: {b[:150]}")

    return products


def load_previous_prices():
    if PRICES_FILE.exists():
        with open(PRICES_FILE) as f:
            return json.load(f)
    return {}


def save_prices(products):
    data = {name: info["price"] for name, info in products.items()}
    with open(PRICES_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find_discounts(current, previous):
    discounts = []
    for name, info in current.items():
        new_price = info["price"]
        old_price = previous.get(name)
        if old_price and new_price < old_price:
            pct = round((old_price - new_price) / old_price * 100, 1)
            discounts.append({
                "name": name,
                "old_price": old_price,
                "new_price": new_price,
                "pct": pct,
                "link": info.get("link", ""),
            })
    return sorted(discounts, key=lambda x: x["pct"], reverse=True)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json={
            "chat_id": int(TELEGRAM_CHAT_ID),
            "text": text,
            "parse_mode": "HTML",
        })
        resp.raise_for_status()


def main():
    print(f"[{datetime.now()}] Запуск трекера Sensai...")
    print(f"ScraperAPI: {'задан' if SCRAPER_API_KEY else 'не задан'}")

    html = fetch_html()
    current = parse_products(html)
    print(f"Итого найдено товаров: {len(current)}")

    if not current:
        send_telegram("⚠️ <b>Sensai Tracker</b>: не удалось загрузить товары с bigbox.ee.\n\nСайт блокирует автоматические запросы. Нужен ScraperAPI ключ.")
        return

    previous = load_previous_prices()
    discounts = find_discounts(current, previous)
    save_prices(current)

    if not previous:
        msg = (f"📦 <b>Sensai Tracker запущен!</b>\n"
               f"Нашёл {len(current)} товаров Sensai на bigbox.ee.\n"
               f"Завтра начну отслеживать изменения цен. 🔍")
        send_telegram(msg)
        return

    if discounts:
        lines = [f"🔥 <b>Sensai подешевел!</b> ({datetime.now().strftime('%d.%m.%Y')})\n"]
        for d in discounts:
            lines.append(
                f"📉 <b>{d['name']}</b>\n"
                f"   {d['old_price']:.2f}€ → {d['new_price']:.2f}€ (-{d['pct']}%)\n"
                f"   🔗 <a href='{d['link']}'>Посмотреть</a>\n"
            )
        send_telegram("\n".join(lines))
    else:
        send_telegram(f"✅ <b>Sensai</b> ({datetime.now().strftime('%d.%m.%Y')}): цены не изменились 👀")


if __name__ == "__main__":
    main()
