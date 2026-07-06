#!/usr/bin/env python3
"""Komorebi Stationery - Pinterest catalog feed builder.

Shopify Admin GraphQL から商品データを取得し、国別(通貨別)の
Pinterest カタログ CSV を feeds/ に生成する。

旧構成(Google Sheets + Matrixify IMPORTDATA + GOOGLEFINANCE)の置き換え。
ロジックは旧シート「Pinterestカタログ」の数式を移植:
  - price = Variant Price(JPY) * 1.02 / 為替(XXXJPY)
  - availability = qty>=1 -> in stock / qty<=0 & policy=CONTINUE -> preorder / else out of stock
  - variant_names/values = "Title"/"Default Title" は空欄
  - brand = Komorebi Stationery, condition = new
  - google_product_category = metafield mc-facebook.google_product_category
    (無ければ mm-google-shopping.google_product_category)

必要な環境変数:
  KOMOREBI_SHOPIFY_DOMAIN        例: b27b56.myshopify.com
  KOMOREBI_SHOPIFY_ACCESS_TOKEN  Admin API アクセストークン(read_products, read_inventory)
"""
import csv
import html
import json
import os
import re
import sys
import time
import urllib.request

API_VERSION = "2026-01"
PRICE_MARKUP = 1.02  # 旧シート: 商品リスト_成形!I = Variant Price * 1.02
BRAND = "Komorebi Stationery"

# 非公開商品の除外タグ。
# TAKETORI等の「抽選販売(Lottery Sales)」限定品は status=ACTIVE かつ Admin API の
# onlineStoreUrl/publishedAt が値を返す(=Shopify的には公開扱い)のに、実店頭では
# テーマ側で隠され購入不可。標準の公開フラグでは判別できないため、Komorebi自身が
# 付けているこのタグで除外する。将来 read_product_listings スコープを付与できれば
# publishedOnCurrentPublication での厳密判定に置き換え可能。
EXCLUDE_TAGS = {"Lottery Sales"}

# 国別フィード: (出力ファイル名, 通貨コード) 旧シートの import用_XX タブと同一
COUNTRIES = {
    "canada": "CAD",
    "australia": "AUD",
    "new-zealand": "NZD",
    "mexico": "MXN",
    "germany": "EUR",
    "france": "EUR",
    "italy": "EUR",
    "spain": "EUR",
    "netherlands": "EUR",
    "united-kingdom": "GBP",
    "hong-kong": "HKD",
    "taiwan": "TWD",
    "indonesia": "IDR",
    "poland": "PLN",
    "india": "INR",
    "belgium": "EUR",
    "portugal": "EUR",
    "romania": "RON",
    "sweden": "SEK",
    "greece": "EUR",
    "austria": "EUR",
    "switzerland": "CHF",
    "hungary": "HUF",
    # ブラジル: Pinterestに削除できないブラジルロックのデータソースが残っており、
    # 空フィードだとエラーになるため正規のBRLフィードを供給して無害化(2026-07-07 Taka決定)。
    "brazil": "BRL",
}

HEADERS = [
    "id", "item_group_id", "variant_names", "variant_values", "title",
    "link", "image_link", "price", "product_type", "google_product_category",
    "availability", "brand", "description", "condition",
]

ROOT = os.path.dirname(os.path.abspath(__file__))
FEEDS_DIR = os.path.join(ROOT, "feeds")
RATES_CACHE = os.path.join(ROOT, "fx_rates.json")
TAXONOMY_PATH = os.path.join(ROOT, "google_taxonomy.json")  # 数値コード -> カテゴリ文字列


def http_json(url, data=None, headers=None, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt+1} after error: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))


def fetch_fx_rates():
    """JPY基準の為替レートを取得。失敗時は前回成功分(fx_rates.json)にフォールバック。"""
    needed = sorted(set(COUNTRIES.values()))
    try:
        data = http_json("https://open.er-api.com/v6/latest/JPY")
        if data.get("result") != "success":
            raise RuntimeError(f"er-api result={data.get('result')}")
        rates = {c: data["rates"][c] for c in needed}
        with open(RATES_CACHE, "w") as f:
            json.dump({"date": data.get("time_last_update_utc"), "rates": rates}, f, indent=1)
        print(f"FX rates fetched ({data.get('time_last_update_utc')})")
        return rates
    except Exception as e:
        print(f"WARN: FX fetch failed ({e}); falling back to cached fx_rates.json", file=sys.stderr)
        with open(RATES_CACHE) as f:
            cached = json.load(f)
        print(f"FX rates from cache ({cached.get('date')})")
        return cached["rates"]


def shopify_graphql(query, variables=None):
    domain = os.environ["KOMOREBI_SHOPIFY_DOMAIN"]
    token = os.environ["KOMOREBI_SHOPIFY_ACCESS_TOKEN"]
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    data = http_json(
        f"https://{domain}/admin/api/{API_VERSION}/graphql.json",
        data=body,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
    )
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 100, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    nodes {
      legacyResourceId
      isGiftCard
      title
      tags
      onlineStoreUrl
      productType
      descriptionHtml
      featuredMedia { preview { image { url } } }
      catFb: metafield(namespace: "mc-facebook", key: "google_product_category") { value }
      catGs: metafield(namespace: "mm-google-shopping", key: "google_product_category") { value }
      variants(first: 100) {
        nodes {
          legacyResourceId
          sku
          price
          inventoryQuantity
          inventoryPolicy
          selectedOptions { name value }
          image { url }
        }
      }
    }
  }
}
"""

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def html_to_text(s):
    if not s:
        return ""
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|br)>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", " ", s)
    s = TAG_RE.sub("", s)
    s = html.unescape(s)
    return WS_RE.sub(" ", s).strip()


def resolve_category(value, taxonomy):
    """metafield値をPinterest用カテゴリ文字列に正規化する。

    - 数値コード(例: "961") -> Googleタクソノミー文字列に変換(旧シートのvlookup相当)
    - タブ結合の汚れ値(例: "Desk Organizers\\tOffice Supplies > ...") -> フルパス側を採用
    """
    value = (value or "").strip()
    if "\t" in value:
        parts = [p.strip() for p in value.split("\t") if ">" in p]
        value = parts[-1] if parts else value.split("\t")[-1].strip()
    if value.isdigit():
        return taxonomy.get(value, "")
    return value


def build_base_rows():
    """通貨換算前(JPY)の行を組み立てる。price列だけ後で国別に換算する。"""
    with open(TAXONOMY_PATH) as f:
        taxonomy = json.load(f)
    rows = []
    cursor = None
    n_products = 0
    while True:
        data = shopify_graphql(PRODUCTS_QUERY, {"cursor": cursor})
        page = data["products"]
        for p in page["nodes"]:
            if not p.get("onlineStoreUrl"):
                continue  # オンラインストア未公開は除外
            if p.get("isGiftCard"):
                continue  # ギフトカードはフィード対象外(旧フィードにも無し)
            if EXCLUDE_TAGS & set(p.get("tags") or []):
                continue  # 抽選販売等の非公開商品を除外
            n_products += 1
            category = (resolve_category((p["catFb"] or {}).get("value"), taxonomy)
                        or resolve_category((p["catGs"] or {}).get("value"), taxonomy))
            desc = html_to_text(p.get("descriptionHtml"))
            fm = p.get("featuredMedia") or {}
            product_image = ((fm.get("preview") or {}).get("image") or {}).get("url", "")
            for v in p["variants"]["nodes"]:
                opts = v.get("selectedOptions") or []
                names = ", ".join(o["name"] for o in opts if o["name"] != "Title")
                values = ", ".join(o["value"] for o in opts if o["value"] != "Default Title")
                qty = v.get("inventoryQuantity") or 0
                if qty >= 1:
                    availability = "in stock"
                elif v.get("inventoryPolicy") == "CONTINUE":
                    availability = "preorder"
                else:
                    availability = "out of stock"
                image = (v.get("image") or {}).get("url") or product_image
                rows.append({
                    "id": v["legacyResourceId"],
                    "item_group_id": p["legacyResourceId"],
                    "variant_names": names,
                    "variant_values": values,
                    "title": p["title"],
                    "link": p["onlineStoreUrl"],
                    "image_link": image,
                    "price_jpy": float(v["price"]) * PRICE_MARKUP,
                    "product_type": p.get("productType") or "",
                    "google_product_category": category,
                    "availability": availability,
                    "brand": BRAND,
                    "description": desc,
                    "condition": "new",
                })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    print(f"Shopify: {n_products} published products / {len(rows)} variants")
    return rows


def write_feeds(rows, rates):
    os.makedirs(FEEDS_DIR, exist_ok=True)
    for slug, currency in COUNTRIES.items():
        rate = rates[currency]  # 1 JPY = rate XXX
        path = os.path.join(FEEDS_DIR, f"{slug}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(HEADERS)
            for r in rows:
                # price は「金額 通貨コード」形式にする(例: "12345.00 IDR")。
                # 通貨コードが無いとPinterestが通貨を判別できず、データソースの
                # 国/地域と不一致になり全商品に通貨ミスマッチ警告が付く(2026-07-07判明)。
                price = f'{round(r["price_jpy"] * rate, 2)} {currency}'
                w.writerow([
                    r["id"], r["item_group_id"], r["variant_names"], r["variant_values"],
                    r["title"], r["link"], r["image_link"], price,
                    r["product_type"], r["google_product_category"],
                    r["availability"], r["brand"], r["description"], r["condition"],
                ])
    print(f"Wrote {len(COUNTRIES)} feeds x {len(rows)} rows to feeds/")


def main():
    rates = fetch_fx_rates()
    rows = build_base_rows()
    if len(rows) < 500:
        # 旧シート実績は約900バリアント。極端に少なければ異常とみなし
        # 古いフィードを壊さないよう失敗させる。
        raise RuntimeError(f"Sanity check failed: only {len(rows)} variants fetched")
    write_feeds(rows, rates)
    with open(os.path.join(ROOT, "meta.json"), "w") as f:
        json.dump({
            "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "variants": len(rows),
            "countries": len(COUNTRIES),
        }, f, indent=1)


if __name__ == "__main__":
    main()
