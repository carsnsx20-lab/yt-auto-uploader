# YT Auto Uploader

A self-hosted YouTube automation tool with a web dashboard. It pulls videos from Google Drive, uses each filename as the exact title, builds automotive tags locally, optionally generates a description with Groq or Gemini, and schedules uploads automatically.

Configured for automotive YouTube Shorts channels, including The Auto Pit.

---

## Features

- **Multi-channel support** — manage as many YouTube channels as you want from one dashboard
- **Google Drive queue** — drop videos in a Drive folder; the uploader picks the oldest unuploaded one each run
- **Automotive metadata** — local branded tags with optional AI-written automotive descriptions
- **Scheduled uploads** — set a daily trigger time and uploads-per-day count per channel
- **YouTube Shorts scheduling** — multiple videos per day are spaced out and set to `publishAt` so they release throughout the day
- **Web dashboard** — live status, 7-day upload chart, system logs, and one-click controls at `http://localhost:8000`
- **REST API** — trigger uploads, check status, and read logs from any external system

---

## Prerequisites

- **Python 3.11+**
- **ffmpeg** (optional — needed for vision-based AI metadata; text-only metadata works without it)
  - Windows: `winget install Gyan.FFmpeg`
  - Mac: `brew install ffmpeg`
- A **Google Cloud project** with YouTube Data API v3 and Google Drive API enabled
- A **Groq API key** (free) — get one at [console.groq.com](https://console.groq.com/keys)

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/yt-auto-uploader.git
cd yt-auto-uploader
pip install -r requirements.txt
```

### 2. Set up your API keys

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Edit `.env`:

```
GROQ_API_KEY=gsk_your_key_here
GEMINI_API_KEY=AIza_your_key_here   # optional fallback
```

### 3. Set up Google OAuth

You need a `client_secrets.json` file from Google Cloud Console. Do this **once**:

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or select an existing one)
3. Enable these two APIs:
   - **YouTube Data API v3**
   - **Google Drive API**
4. Go to **OAuth consent screen** → External → fill in App name → add your Gmail as a **Test user**
5. Go to **Credentials** → **Create Credentials** → **OAuth 2.0 Client ID** → Desktop app → Download JSON
6. Open the dashboard at `http://localhost:8000`, click **Settings**, and paste the JSON content into the "Paste client_secrets.json" field → Save

### 4. Run

```bash
python api.py
```

Open your browser at **http://localhost:8000**

---

## Adding Your First Channel

1. Click **+ Add Channel** in the dashboard
2. Fill in:
   - **Channel Name** — any label you want
   - **First Upload Time** — when the daily batch job fires (local time)
   - **Uploads per Day** — how many videos to schedule per day (they are spaced evenly across 24h)
   - **Drive Queue Folder ID** — open your Drive folder → copy the ID from the URL:
     `drive.google.com/drive/folders/`**`THIS_PART_IS_THE_ID`**
   - **Privacy** — `Public`, `Unlisted`, or `Private`
3. Click **Save Channel**
4. Click **🔑 Auth** on the channel card — a browser window opens for Google sign-in (YouTube + Drive access)
5. Drop videos into your Drive folder and click **Schedule N** to do an immediate test run

---

## Configuration

### `.env` (secrets — gitignored)

| Variable | Description |
|---|---|
| `GROQ_API_KEY` | Groq API key. Enables AI metadata via Llama 4 vision. Recommended. |
| `GEMINI_API_KEY` | Gemini API key. Used as fallback if Groq is not set. |

### `config.json` (non-sensitive defaults)

| Field | Default | Description |
|---|---|---|
| `upload_time` | `"09:00"` | Default daily trigger time (HH:MM, local) |
| `privacy_status` | `"public"` | Default privacy for new videos |
| `default_category` | `"2"` | Autos & Vehicles category used for uploads |
| `ai_metadata_enabled` | `true` | Generate an optional automotive description with Groq/Gemini |
| `automotive_metadata` | See `config.example.json` | Editable channel name, description template, default tags, tag cap, and filename keyword mapping |
| `gemini_extra_prompt` | `""` | Extra instructions appended to the AI prompt |
| `drive_queue_folder_id` | `""` | Fallback Drive folder (overridden per-channel) |

Per-channel settings (upload time, uploads per day, privacy, etc.) are set via the dashboard and stored in `channels.json`.

---

## How It Works

```
Daily trigger fires
       │
       ▼
Get next video from Drive folder (oldest unuploaded)
       │
       ▼
Download to temp file
       │
       ▼
Extract video frames (ffmpeg) → Groq Vision reads the frames
       │  (falls back to text-only if ffmpeg not installed)
       ▼
Filename supplies exact title · category is Autos & Vehicles (2)
AI optionally enriches the description; tags are generated locally
       │
       ▼
Upload to YouTube with publishAt timestamp
       │
       ▼
Record Drive file ID in uploaded list (skipped on next run)
       │
       ▼
Delete temp file · Repeat for remaining slots
```

---

## REST API

The dashboard consumes these endpoints — you can also call them from any script or tool.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/channels` | List all channels with live status |
| `POST` | `/api/channels` | Add a new channel |
| `PUT` | `/api/channels/{id}` | Update a channel |
| `DELETE` | `/api/channels/{id}` | Delete a channel |
| `POST` | `/api/channels/{id}/trigger` | Trigger an immediate upload batch |
| `POST` | `/api/channels/{id}/pause` | Toggle pause on/off |
| `POST` | `/api/channels/{id}/authenticate` | Re-run OAuth for a channel |
| `GET` | `/api/analytics` | Global stats + 7-day upload time series |
| `GET` | `/api/history` | Recent upload history |
| `GET` | `/api/logs` | Live log stream |
| `GET` | `/api/settings` | Read global config |
| `POST` | `/api/settings` | Update global config |
| `POST` | `/api/secrets` | Save `client_secrets.json` content |

Full interactive docs: **http://localhost:8000/docs**

---

## Troubleshooting

**"client_secrets.json not found"**
→ Use the Settings panel in the dashboard to paste your OAuth JSON, or place the file manually next to `api.py`.

**OAuth browser window doesn't open**
→ Run `python api.py` in a terminal (not as a background service) — the OAuth flow needs an interactive browser.

**"quota exceeded" from Gemini**
→ Groq is the primary AI provider and has a more generous free tier. Set `GROQ_API_KEY` in `.env` and it will be used automatically.

**Videos are skipped / not uploading**
→ Check the System Log in the dashboard. The most common cause is the Drive folder ID being wrong, or all videos in the folder have already been uploaded (shown in the uploaded list).

**To re-upload a video that was already processed**
→ Click **Reset** on the channel card to clear the uploaded list. This does not delete anything from Drive or YouTube.

---

## Project Structure

```
api.py                  # Main entry point — run this
uploader.py             # Core upload logic (Drive → AI → YouTube)
frontend/
  index.html            # Dashboard UI
  css/style.css
  js/app.js
config.json             # Non-sensitive defaults (committed as empty template)
config.example.json     # Annotated example
.env                    # Your API keys (gitignored — never committed)
.env.example            # Template showing available env vars
channels.json           # Per-channel config (gitignored — created by the dashboard)
channels.example.json   # Template
credentials/            # OAuth tokens per channel (gitignored)
data/                   # Upload history (gitignored)
logs/                   # Log files (gitignored)
```

---

## License

MIT
