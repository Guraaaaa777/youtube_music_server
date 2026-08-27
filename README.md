# youtube_music_server

指定した YouTube 動画を **サーバー機のスピーカーで音声だけ再生** する Web サーバー。
ブラウザ側は操作パネルで、音は常にサーバー側から鳴ります（家の PC を音楽プレイヤー化して
スマホから操作する、といった使い方向け）。

- 音声抽出: [yt-dlp](https://github.com/yt-dlp/yt-dlp)（ダウンロードせずストリーム再生）
- 再生: `ffplay -nodisp`（FFmpeg 同梱）
- API/画面: FastAPI + 素の HTML/JS

## セットアップ

FFmpeg（`ffplay`）を PATH に通したうえで、

```bash
pip install -r requirements.txt
```

## 起動

```bash
python server.py
```

`http://127.0.0.1:8000` を開く。環境変数で変更できます。

| 変数 | 既定値 | 説明 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | LAN の他端末から操作するなら `0.0.0.0` |
| `PORT` | `8000` | 待ち受けポート |
| `FFPLAY` | PATH から自動検出 | `ffplay` の実行パス |

例（LAN 公開）:

```bash
HOST=0.0.0.0 PORT=8080 python server.py
```

認証は無いので、`0.0.0.0` で待ち受ける場合は信頼できるネットワークだけにしてください
（誰でも再生・音量操作ができます）。

## 使い方

入力欄に次のいずれかを入れて「再生」または「キューに追加」:

- 動画 URL — `https://www.youtube.com/watch?v=...` / `https://youtu.be/...`
- 動画 ID — `jNQXAC9IVRw`
- プレイリスト URL — 全曲がキューに展開されます
- 検索語 — 先頭の 1 件を追加します

キューの曲をクリックでその曲へジャンプ、`×` で削除。スペースキーで再生/一時停止。
曲が終わると自動で次へ進み、リピートは オフ → 全曲 → 1曲 で切り替わります。

## HTTP API

| メソッド | パス | ボディ | 説明 |
| --- | --- | --- | --- |
| GET | `/api/status` | – | 再生状態・再生位置・キュー全体 |
| POST | `/api/play` | `{"url": "..."}` | 追加して即再生 |
| POST | `/api/add` | `{"url": "...", "play_now": false}` | キューに追加（停止中なら再生開始） |
| POST | `/api/toggle` | – | 再生 / 一時停止 |
| POST | `/api/pause` \| `/api/resume` \| `/api/stop` | – | 一時停止 / 再開 / 停止 |
| POST | `/api/next` \| `/api/prev` | – | 曲送り / 曲戻し |
| POST | `/api/seek` | `{"position": 90}` | 秒指定でシーク |
| POST | `/api/volume` | `{"volume": 60}` | 音量 0–100 |
| POST | `/api/repeat` | `{"mode": "off\|all\|one"}` | リピート設定 |
| POST | `/api/queue/{i}/play` | – | キューの i 番目を再生 |
| DELETE | `/api/queue/{i}` | – | キューから 1 曲削除 |
| DELETE | `/api/queue` | – | キューを全消去 |

すべての更新系レスポンスは `/api/status` と同じ形を返すので、そのまま画面反映に使えます。
`curl` からの例:

```bash
curl -X POST localhost:8000/api/play -H 'Content-Type: application/json' -d '{"url":"https://youtu.be/jNQXAC9IVRw"}'
```

OpenAPI ドキュメントは `/api/docs`。

## 仕組みと制約

`ffplay` には実行中の制御チャンネルが無いため、**一時停止・シーク・音量変更はプロセスを
その位置で起動し直す**ことで実現しています。動作としては自然ですが、切り替え時に一瞬の
無音（ストリーム再取得ぶん）が入ります。

- ストリーム URL は署名付きで期限があるため、30 分キャッシュして再生直前に取り直します。
- プレイリストはメタデータのみ先に取得（183 曲で約 3 秒）し、実 URL は再生直前に解決します。
- 再生状態はメモリ上のみ。サーバーを再起動するとキューは消えます。
- 年齢制限付き動画などは yt-dlp に cookie が必要になる場合があります。
