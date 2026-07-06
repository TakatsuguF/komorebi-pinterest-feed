# Komorebi Stationery - Pinterest Catalog Feeds

Pinterest カタログ用の国別商品フィード(CSV)を毎日自動生成するリポジトリ。

- データ源: Shopify Admin API(商品・価格・在庫・カテゴリmetafield)
- 為替: open.er-api.com(JPY基準、失敗時は前回レートにフォールバック)
- 生成: GitHub Actions が毎日 10:00 JST に `build_feeds.py` を実行し `feeds/*.csv` を更新
- 配信: GitHub Pages。Pinterest の各国データソースはこの URL を参照する

旧構成(Google Sheets「Pinterestカタログ」+ Matrixify IMPORTDATA + GOOGLEFINANCE)の
置き換え。IMPORTDATA のデータ量制限と GOOGLEFINANCE の間欠エラーによる
Pinterest 取得失敗を解消するために移行(2026-07)。

## フィードURL

`https://takatsuguf.github.io/komorebi-pinterest-feed/feeds/<country>.csv`

23カ国: canada, australia, new-zealand, mexico, germany, france, italy, spain,
netherlands, united-kingdom, hong-kong, taiwan, indonesia, poland, india,
belgium, portugal, romania, sweden, greece, austria, switzerland, hungary

## 手動実行

Actions タブ -> build-feeds -> Run workflow
