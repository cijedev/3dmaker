# 3D Maker

Web app that turns prompts into 3D models (Flask).

## Published from cije.live archive

This repository was published from a cleaned project archive. Secrets, runtime databases, uploads, and dependency folders were removed. Configure via environment variables (see `.env.example` if present, or the docs below).

**Do not commit** `.env`, password files, private keys, or live databases.


# CIJEs 3DGEN

Simple web UI + API for queuing 3D-print prompts and downloading STL files when ready.

Generation is **not** done inside this app by default. An external bot (or admin) receives a webhook, builds the model, and uploads the STL. For local demos, set `LOCAL_GENERATOR=1` to synthesize simple meshes for a few keywords (`yoyo`, `cube`, `cylinder`, `phone stand` / `stand`, `disc`).

## Features

- `POST /api/jobs` — create a job (`{ "prompt": "..." }`, max 500 chars)
- `GET /api/jobs/<id>` — poll status (`queued` | `generating` | `ready` | `failed`)
- `PUT /api/jobs/<id>/model` — upload finished STL (`X-Upload-Token` header)
- `GET /api/jobs/<id>/download` — download STL when ready
- `GET /health` and `GET /ready` — health JSON
- Jobs on disk: `data/jobs/<uuid>/meta.json` + optional `model.stl`
- Designed for reverse-proxy prefix `/3dmaker` via `ROOT_PATH`

## Quick start (local)

```bash
cd /path/to/3dmaker-web
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# For a working demo without a bot:
#   LOCAL_GENERATOR=1
#   ROOT_PATH=          (empty when serving at /)
#   HOST=127.0.0.1
#   PORT=8090
uvicorn app:app --host 127.0.0.1 --port 8090 --reload
```

Open http://127.0.0.1:8090/ and try prompts like `cube` or `yoyo`.

## Environment

See `.env.example`:

| Variable | Purpose |
|----------|---------|
| `ROOT_PATH` | ASGI root path when behind a proxy (e.g. `/3dmaker`) |
| `UPLOAD_TOKEN` | Shared secret for STL uploads (`X-Upload-Token`) |
| `BOT_WEBHOOK_URL` | Optional POST target when a job is created |
| `BOT_WEBHOOK_KEY` | Sent as `Authorization: Bearer …` and `X-Webhook-Key` |
| `LOCAL_GENERATOR` | `1` = local keyword STL fallback; `0` in production |
| `HOST` / `PORT` | Bind address for uvicorn / systemd |

**Never commit a real `.env`.** Rotate `UPLOAD_TOKEN` if it leaks.

## Bot

The user's Generate click is delivered straight to the bot. The bot listens here and receives the prompt in the response. It does not read job files.

```bash
curl -H "X-Upload-Token: $UPLOAD_TOKEN" \
  "https://cije.live/3dmaker/api/bot/next?timeout=25"
```

A prompt looks like:

```json
{
  "job_id": "<uuid>",
  "prompt": "...",
  "upload_url": "https://cije.live/3dmaker/api/jobs/<uuid>/model"
}
```

An empty queue returns `204`. Send the finished STL back with either a raw body or a multipart file:

```bash
curl -X PUT "$upload_url" \
  -H "X-Upload-Token: $UPLOAD_TOKEN" \
  --data-binary @model.stl
```

If `BOT_WEBHOOK_URL` is set, the same prompt is also POSTed there when the user clicks Generate.

## Deploy behind nginx

1. Install under `/opt/3dmaker-web`, create venv, copy `.env` (set `ROOT_PATH=/3dmaker`, `LOCAL_GENERATOR=0`, strong `UPLOAD_TOKEN`).
2. Install systemd unit from `systemd/3dmaker.service` (adjust paths/user).
3. Add `nginx/3dmaker.conf` location block; reload nginx.

The UI uses relative API paths and detects a `/3dmaker` prefix so it works at `/` and `/3dmaker/`.

## Layout

```
3dmaker-web/
  app.py
  requirements.txt
  .env.example
  README.md
  static/          # SPA (index.html, styles.css, app.js)
  data/jobs/       # runtime job storage
  systemd/
  nginx/
```


## Inbound webhook

`POST /api/webhook` (public: `https://cije.live/3dmaker/api/webhook`).

Send `X-Webhook-Key: $WEBHOOK_SECRET` or `Authorization: Bearer $WEBHOOK_SECRET`.

```json
{"action":"ping"}
{"action":"create","prompt":"a phone stand"}
{"action":"status","job_id":"<uuid>","status":"failed","error":"reason"}
```

Finished models are still uploaded with `PUT /api/jobs/<id>/model` and `X-Upload-Token`.

If `LOCAL_GENERATOR=1` and the prompt is not a known simple shape, the app also POSTs the job to `BOT_WEBHOOK_URL` when that URL is set.
