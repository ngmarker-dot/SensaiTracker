import json
import os
import re
from datetime import datetime
from pathlib import Path

import httpx

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PRICES_FILE = Path("prices.json")

# Пробуем разные User-Agent чтобы обойти блокировку
HEADERS_LIST = [
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "et-EE,et;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    },
]


def fetch_products():
    products = {}

    for headers in HEADERS_LIST:
        try:
            with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
                # Сначала заходим на главную чтобы получить cookies
                client.get("https://bigbox.ee/", timeout=15)

                resp = client.get(
                    "https://bigbox.ee/search",
                    params={"text": "sensai", "resultsPerPage": "100"},
                )
                print(f"Статус: {resp.status_code}, размер: {len(resp.text)} байт")

                if resp.status_code == 403:
                    print("403 — пробуем следующий User-Agent")
                    continue

                resp.raise_for_status()
                html = resp.text

                # JSON-LD structured data
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

                # Regex fallback — ищем цены рядом с Sensai
                if not products:
                    patterns = [
                        r'"name"\s*:\s*"([^"]*[Ss]ensai[^"]*)"[^}]{0,400}?"price"\s*:\s*"?([\d.]+)',
                        r'data-product-name="([^"]*[Ss]ensai[^"]*)"[^>]*data-product-price="([\d.]+)"',
                        r'itemprop="name"[^>]*>\s*([^<]*[Ss]ensai[^<]*)<[^"]{0,300}itemprop="price"[^>]*content="([\d.]+)"',
                    ]
                    for pattern in patterns:
                        matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)
                        for name, price in matches:
                            name = re.sub(r'\s+', ' ', name).strip()[:100]
                            if name and name not in products:
                                products[name] = {"price": float(price), "link": "https://bigbox.ee/search?text=sensai"}
                                print(f"  Regex: {name} — {price}€")

                # Если нашли хоть что-то — выходим из цикла
                if products:
                    break

                # Если ничего не нашли но страница загрузилась — сохраняем отладочный фрагмент
                if "sensai" in html.lower():
                    print("Sensai упоминается на странице, но товары не распознаны")
                    # Ищем любые цены рядом с sensai
                    blocks = re.findall(r'.{0,100}[Ss]ensai.{0,200}', html)
                    for b in blocks[:3]:
                        print(f"  Контекст: {b[:150]}")
                else:
                    print("Sensai не найден на странице")

                break

        except Exception as e:
            print(f"Ошибка: {e}")
            continue

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
    print(f"Отправка в Telegram, chat_id={TELEGRAM_CHAT_ID}")
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json={
            "chat_id": int(TELEGRAM_CHAT_ID),
            "text": text,
            "parse_mode": "HTML",
        })
        print(f"Telegram ответ: {resp.status_code} — {resp.text[:200]}")
        resp.raise_for_status()


def main():
    print(f"[{datetime.now()}] Запуск трекера Sensai...")
    print(f"TOKEN задан: {'да' if TELEGRAM_TOKEN else 'НЕТ'}")
    print(f"CHAT_ID: {TELEGRAM_CHAT_ID}")

    current = fetch_products()
    print(f"Итого найдено товаров: {len(current)}")

    if not current:
        print("Товары не найдены — отправляем предупреждение")
        send_telegram("⚠️ <b>Sensai Tracker</b>: не удалось загрузить товары с bigbox.ee. Проверю завтра.")
        return

    previous = load_previous_prices()
    discounts = find_discounts(current, previous)
    save_prices(current)

    if not previous:
        msg = (
            f"📦 <b>Sensai Tracker запущен!</b>\n"
            f"Нашёл {len(current)} товаров Sensai на bigbox.ee.\n"
            f"Завтра начну отслеживать изменения цен. 🔍"
        )
        send_telegram(msg)
        print("Первый запуск — сохранил базовые цены.")
        return

    if discounts:
        lines = [f"🔥 <b>Sensai подешевел на bigbox.ee!</b> ({datetime.now().strftime('%d.%m.%Y')})\n"]
        for d in discounts:
            lines.append(
                f"📉 <b>{d['name']}</b>\n"
                f"   Было: {d['old_price']:.2f}€ → Стало: {d['new_price']:.2f}€ (-{d['pct']}%)\n"
                f"   🔗 <a href='{d['link']}'>Посмотреть</a>\n"
            )
        send_telegram("\n".join(lines))
    else:
        msg = f"✅ <b>Sensai на bigbox.ee</b> ({datetime.now().strftime('%d.%m.%Y')})\nЦены не изменились. Слежу дальше 👀"
        send_telegram(msg)


if __name__ == "__main__":
    main()
