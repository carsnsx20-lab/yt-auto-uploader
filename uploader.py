"""
YouTube Auto Uploader — Google Drive Edition
Flow: Drive folder → download temp → Gemini watches it → metadata → YouTube upload → move to done/ in Drive
"""

import io
import json
import logging
import os
import re
import socket
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Give Google API calls 60 s to connect — default Windows timeout is too short for Indian ISPs
socket.setdefaulttimeout(60)

from google import genai
from apscheduler.schedulers.blocking import BlockingScheduler
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR     = Path(__file__).parent
LOG_DIR      = BASE_DIR / "logs"
TOKEN_FILE   = BASE_DIR / "token.json"
SECRETS_FILE = BASE_DIR / "client_secrets.json"
CONFIG_FILE  = BASE_DIR / "config.json"

# Drive read-only is enough — we never move or delete files, just download them
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/drive.readonly",
]

GEMINI_MODEL     = "gemini-2.0-flash-lite"
DRIVE_VIDEO_MIME = "video/"  # matches all video/* MIME types

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_DIR / f"uploader_{datetime.now().strftime('%Y%m')}.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_AUTOMOTIVE_METADATA = {
    # Edit this section or override "automotive_metadata" in config.json.
    "channel_name": "The Auto Pit",
    "default_tags": [
        "cars",
        "automotive",
        "car culture",
        "car enthusiast",
        "car facts",
        "car shorts",
        "vehicles",
        "auto",
        "The Auto Pit",
    ],
    "description_template": (
        "{VIDEO_TITLE}\n\n"
        "Automotive content featuring cars, performance, technology, and car culture.\n\n"
        "Subscribe to {CHANNEL_NAME} for automotive Shorts.\n\n"
        "#Shorts #Cars #Automotive"
    ),
    "max_tags": 25,
    "keyword_tags": {
        # Manufacturers
        "bmw": ["BMW", "BMW cars", "BMW performance"],
        "audi": ["Audi", "Audi cars", "Audi performance"],
        "mercedes": ["Mercedes-Benz", "Mercedes AMG"],
        "porsche": ["Porsche", "Porsche cars"],
        "toyota": ["Toyota", "Toyota cars"],
        "ford": ["Ford", "Ford cars"],
        "chevrolet": ["Chevrolet", "Chevrolet cars"],
        "honda": ["Honda", "Honda cars"],
        "nissan": ["Nissan", "Nissan cars"],
        "lexus": ["Lexus", "Lexus cars"],
        "tesla": ["Tesla", "Tesla cars", "electric vehicles"],
        "volkswagen": ["Volkswagen", "Volkswagen cars"],
        "subaru": ["Subaru", "Subaru cars"],
        "mazda": ["Mazda", "Mazda cars"],
        "ferrari": ["Ferrari", "Ferrari cars"],
        "lamborghini": ["Lamborghini", "Lamborghini cars"],
        "mclaren": ["McLaren", "McLaren cars"],
        "dodge": ["Dodge", "Dodge cars"],
        "jeep": ["Jeep", "Jeep vehicles"],
        "hyundai": ["Hyundai", "Hyundai cars"],
        "kia": ["Kia", "Kia cars"],
        "acura": ["Acura", "Acura cars"],
        "infiniti": ["Infiniti", "Infiniti cars"],
        "volvo": ["Volvo", "Volvo cars"],
        "rivian": ["Rivian", "electric vehicles"],
        # Models, systems, and automotive topics
        "m340i": ["BMW M340i", "sports sedan"],
        "quattro": ["Audi Quattro", "all-wheel drive", "AWD"],
        "amg": ["AMG performance"],
        "supra": ["Toyota Supra", "sports cars"],
        "corvette": ["Chevrolet Corvette", "sports cars"],
        "gtr": ["Nissan GT-R", "performance cars"],
        "gt-r": ["Nissan GT-R", "performance cars"],
        "supercharged": ["supercharged cars", "supercharger"],
        "awd": ["all-wheel drive", "AWD"],
        "4wd": ["four-wheel drive", "4WD"],
        "drifting": ["drifting", "motorsport"],
        "drag race": ["drag racing", "car performance"],
    },
}

DEFAULT_CONFIG = {
    "upload_time":          "09:00",   # HH:MM local time
    "privacy_status":       "public",  # public | unlisted | private
    "default_category":     "2",       # Autos & Vehicles
    "ai_metadata_enabled":  True,      # optional AI descriptions
    "automotive_metadata":  DEFAULT_AUTOMOTIVE_METADATA,
    "gemini_api_key":       "",        # aistudio.google.com/app/apikey
    "gemini_extra_prompt":  "",        # extra instructions for Gemini
    "drive_queue_folder_id": "",       # Drive folder ID where your videos live
    "drive_done_folder_id":  "",       # Drive folder ID to move uploaded videos to
}

# YouTube category IDs Gemini can choose from:
# 1 Film&Animation | 2 Autos | 10 Music | 15 Pets | 17 Sports | 19 Travel
# 20 Gaming | 22 People&Blogs | 23 Comedy | 24 Entertainment | 25 News
# 26 Howto&Style | 27 Education | 28 Science&Tech


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    # Environment variables override config.json for secrets
    if os.getenv("GEMINI_API_KEY"):
        cfg["gemini_api_key"] = os.getenv("GEMINI_API_KEY")
    if os.getenv("GROQ_API_KEY"):
        cfg["groq_api_key"] = os.getenv("GROQ_API_KEY")
    return cfg


YOUTUBE_TAG_CHARACTER_LIMIT = 500


def _automotive_metadata_config(config: dict) -> dict:
    """Merge user-editable automotive metadata overrides with local defaults."""
    custom = config.get("automotive_metadata", {})
    if not isinstance(custom, dict):
        log.warning("automotive_metadata must be an object; using built-in defaults.")
        custom = {}

    settings = {**DEFAULT_AUTOMOTIVE_METADATA, **custom}
    custom_keywords = custom.get("keyword_tags", {})
    if not isinstance(custom_keywords, dict):
        log.warning(
            "automotive_metadata.keyword_tags must be an object; "
            "using built-in keyword tags."
        )
        custom_keywords = {}
    settings["keyword_tags"] = {
        **DEFAULT_AUTOMOTIVE_METADATA["keyword_tags"],
        **custom_keywords,
    }
    return settings


def _youtube_tag_character_count(tags: list[str]) -> int:
    """Calculate YouTube's 500-character tag-list cost."""
    if not tags:
        return 0
    tag_cost = sum(len(tag) + (2 if " " in tag else 0) for tag in tags)
    return tag_cost + len(tags) - 1  # Commas between tags count.


def _title_contains_keyword(video_title: str, keyword: str) -> bool:
    """Match a whole keyword or phrase without partial-word brand matches."""
    pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])"
    return re.search(pattern, video_title, flags=re.IGNORECASE) is not None


def build_video_tags(video_title: str, config: dict) -> list[str]:
    """Build deterministic, deduplicated tags from local automotive settings."""
    settings = _automotive_metadata_config(config)
    default_tags = settings.get("default_tags", [])
    if not isinstance(default_tags, list):
        log.warning(
            "automotive_metadata.default_tags must be a list; "
            "using built-in default tags."
        )
        default_tags = DEFAULT_AUTOMOTIVE_METADATA["default_tags"]

    max_tags = settings.get("max_tags", DEFAULT_AUTOMOTIVE_METADATA["max_tags"])
    if not isinstance(max_tags, int) or isinstance(max_tags, bool) or max_tags < 1:
        log.warning("automotive_metadata.max_tags must be a positive integer; using 25.")
        max_tags = DEFAULT_AUTOMOTIVE_METADATA["max_tags"]

    candidates = list(default_tags)
    for keyword, keyword_tags in settings["keyword_tags"].items():
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        if not _title_contains_keyword(video_title, keyword.strip()):
            continue
        if isinstance(keyword_tags, str):
            candidates.append(keyword_tags)
        elif isinstance(keyword_tags, list):
            candidates.extend(keyword_tags)

    tags = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        tag = candidate.strip()
        dedupe_key = tag.casefold()
        if not tag or dedupe_key in seen:
            continue
        if len(tags) >= max_tags:
            break
        if _youtube_tag_character_count([*tags, tag]) > YOUTUBE_TAG_CHARACTER_LIMIT:
            continue
        tags.append(tag)
        seen.add(dedupe_key)
    return tags


def build_default_description(video_title: str, config: dict) -> str:
    """Render the local automotive description without any AI dependency."""
    settings = _automotive_metadata_config(config)
    template = settings.get(
        "description_template",
        DEFAULT_AUTOMOTIVE_METADATA["description_template"],
    )
    if not isinstance(template, str):
        log.warning(
            "automotive_metadata.description_template must be a string; "
            "using the built-in template."
        )
        template = DEFAULT_AUTOMOTIVE_METADATA["description_template"]
    return (
        template.replace("{VIDEO_TITLE}", video_title)
        .replace("{CHANNEL_NAME}", str(settings["channel_name"]))
    )


# ---------------------------------------------------------------------------
# Google OAuth — single sign-in for both Drive + YouTube
# ---------------------------------------------------------------------------
def get_google_clients(token_file: Path = None, force_reauth: bool = False):
    """Returns (youtube, drive) API clients. token_file overrides default TOKEN_FILE."""
    _token = Path(token_file) if token_file else TOKEN_FILE

    if not SECRETS_FILE.exists():
        raise FileNotFoundError(
            "client_secrets.json not found.\n"
            "  1. console.cloud.google.com → create project\n"
            "  2. Enable: YouTube Data API v3  +  Google Drive API\n"
            "  3. OAuth consent screen → External → add your Gmail as test user\n"
            "  4. Credentials → Create OAuth 2.0 Client ID (Desktop app)\n"
            "  5. Download JSON → save as client_secrets.json next to uploader.py\n"
            "  OR paste it in the Settings panel at http://localhost:8000"
        )

    creds = None
    if not force_reauth and _token.exists():
        creds = Credentials.from_authorized_user_file(str(_token), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing Google token…")
            creds.refresh(Request())
        else:
            log.info("Opening browser for Google sign-in (YouTube + Drive)…")
            flow = InstalledAppFlow.from_client_secrets_file(SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        _token.parent.mkdir(parents=True, exist_ok=True)
        with open(_token, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        log.info(f"Token saved → {_token}")

    youtube = build("youtube", "v3", credentials=creds)
    drive   = build("drive",   "v3", credentials=creds)
    return youtube, drive


# ---------------------------------------------------------------------------
# Uploaded-list helpers  (local text file, one Drive file ID per line)
# ---------------------------------------------------------------------------
def load_uploaded_ids(list_path: Path) -> set:
    """Return set of Drive file IDs already uploaded, read from list_path."""
    if not list_path.exists():
        return set()
    ids = set()
    with open(list_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                ids.add(line.split("|")[0].strip())
    return ids


def mark_as_uploaded(list_path: Path, file_id: str, file_name: str, yt_url: str) -> None:
    """Append one record to the uploaded list."""
    list_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(list_path, "a", encoding="utf-8") as f:
        if list_path.stat().st_size == 0:
            f.write("# YT Auto Uploader — uploaded video log\n")
            f.write("# Format: drive_file_id | filename | youtube_url | uploaded_at\n")
            f.write("# Delete a line to allow that video to be re-uploaded.\n\n")
        f.write(f"{file_id} | {file_name} | {yt_url} | {ts}\n")


# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------
def get_next_drive_video(drive, folder_id: str, skip_ids: set = None) -> dict | None:
    """Return the oldest non-uploaded video in the Drive folder."""
    skip_ids   = skip_ids or set()
    page_token = None
    while True:
        for attempt in range(3):
            try:
                result = drive.files().list(
                    q=(f"'{folder_id}' in parents "
                       f"and mimeType contains '{DRIVE_VIDEO_MIME}' "
                       f"and trashed = false"),
                    orderBy="name",
                    pageSize=100,
                    fields="nextPageToken, files(id, name, mimeType, size)",
                    **( {"pageToken": page_token} if page_token else {} ),
                ).execute()
                break
            except (TimeoutError, OSError) as e:
                if attempt < 2:
                    log.warning(f"Drive API timeout (attempt {attempt+1}/3), retrying in 30s… ({e})")
                    time.sleep(30)
                else:
                    raise
        for f in result.get("files", []):
            if f["id"] not in skip_ids:
                return f
        page_token = result.get("nextPageToken")
        if not page_token:
            return None


def download_drive_file(drive, file_id: str, dest_path: Path) -> None:
    """Download a Drive file to dest_path, logging progress."""
    request    = drive.files().get_media(fileId=file_id)
    buf        = io.FileIO(str(dest_path), "wb")
    downloader = MediaIoBaseDownload(buf, request, chunksize=8 * 1024 * 1024)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            log.info(f"  Drive download: {int(status.progress() * 100)}%")
    buf.close()


def _find_ff(name: str) -> str:
    """Find ffmpeg or ffprobe — checks PATH, then WinGet install folder."""
    import shutil, glob as _glob
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    pattern = os.path.join(local, "Microsoft", "WinGet", "Packages",
                           "Gyan.FFmpeg*", "**", f"{name}.exe")
    matches = _glob.glob(pattern, recursive=True)
    if matches:
        log.info(f"Found {name} at: {matches[0]}")
        return matches[0]
    return name  # will fail with FileNotFoundError — caught below


def _extract_video_frames(video_path: Path, n: int = 3) -> list[Path]:
    """Extract n evenly-spaced frames from the video using ffmpeg."""
    import subprocess, tempfile
    ffmpeg  = _find_ff("ffmpeg")
    ffprobe = _find_ff("ffprobe")
    frames  = []
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        duration = float(r.stdout.strip())
        for i in range(n):
            t = duration * (i + 1) / (n + 1)
            with tempfile.NamedTemporaryFile(suffix=f"_f{i}.jpg", delete=False) as _tf:
                frame_path = Path(_tf.name)
            subprocess.run(
                [ffmpeg, "-ss", f"{t:.2f}", "-i", str(video_path),
                 "-vframes", "1", "-vf", "scale=720:-1", "-q:v", "3",
                 "-y", str(frame_path)],
                capture_output=True, timeout=20,
            )
            if frame_path.exists() and frame_path.stat().st_size > 1000:
                frames.append(frame_path)
    except FileNotFoundError:
        log.warning("ffmpeg/ffprobe not found — install ffmpeg and add to PATH.")
    except Exception as e:
        log.warning(f"Frame extraction failed: {e}")
        for f in frames:
            try: f.unlink()
            except Exception: pass
        return []
    return frames


_VISION_PROMPT = """You are an automotive YouTube Shorts metadata expert. You are looking at {n} screenshots from a car video.

The exact video title is already fixed from its Google Drive filename: {video_title}
Do not generate, rewrite, or return a title.

Analyze the visible vehicle, driving scene, components, and automotive topic. Return ONLY a valid JSON object — no markdown or explanation:
{{
  "description": "A concise, accurate automotive description followed by relevant hashtags including #Shorts #Cars #Automotive"
}}

Rules:
- Discuss only automotive content supported by the screenshots or fixed title.
- Keep the description concise and useful.
- Never invent specifications, vehicle models, horsepower figures, or technical claims.
- Do not return title, tags, or categoryId fields.
{extra}"""

_TEXT_ONLY_PROMPT = """You are an automotive YouTube Shorts metadata expert.

The exact video title is already fixed from its Google Drive filename: {video_title}
Do not generate, rewrite, or return a title.

Use that title as context and return ONLY a valid JSON object:
{{
  "description": "A concise automotive description followed by relevant hashtags including #Shorts #Cars #Automotive"
}}

Rules:
- Keep the description accurate to the fixed title; do not invent unsupported specifications.
- Never invent specifications, vehicle models, horsepower figures, or technical claims.
- Do not return title, tags, or categoryId fields.
{extra}"""


def generate_metadata_with_groq(
    config: dict, video_path: Path = None, video_title: str = ""
) -> dict | None:
    """Generate an optional automotive description with Groq."""
    import base64
    api_key = config.get("groq_api_key", "").strip()
    if not api_key:
        return None
    try:
        from groq import Groq
    except ImportError:
        log.error("groq not installed — run: pip install groq")
        return None

    client = Groq(api_key=api_key)
    extra  = config.get("gemini_extra_prompt", "").strip()
    extra_line = f"\nExtra instructions: {extra}" if extra else ""

    # Vision mode: extract frames so Groq can inspect the actual vehicle content.
    if video_path and video_path.exists():
        frames = _extract_video_frames(video_path)
        if frames:
            log.info(f"Sending {len(frames)} video frames to Groq Vision…")
            image_parts = []
            for fp in frames:
                try:
                    b64 = base64.b64encode(fp.read_bytes()).decode()
                    image_parts.append({
                        "type":      "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    })
                finally:
                    try: fp.unlink()
                    except Exception: pass

            prompt = _VISION_PROMPT.format(
                n=len(image_parts), video_title=video_title, extra=extra_line
            )
            messages = [{"role": "user", "content": image_parts + [{"type": "text", "text": prompt}]}]
            model    = "meta-llama/llama-4-scout-17b-16e-instruct"

            try:
                response = client.chat.completions.create(
                    model=model, messages=messages, temperature=0.9, max_tokens=1024,
                )
                return _parse_groq_response(response.choices[0].message.content)
            except Exception as e:
                log.warning(f"Groq Vision failed ({e}) — falling back to text mode…")

    # ── Text-only fallback ──
    log.info("Generating metadata with Groq AI (text mode)…")
    prompt = _TEXT_ONLY_PROMPT.format(video_title=video_title, extra=extra_line)
    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.95,
        )
        return _parse_groq_response(response.choices[0].message.content)
    except Exception as e:
        log.error(f"Groq error: {e}")
        return None


def _parse_groq_response(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1][4:].strip() if parts[1].startswith("json") else parts[1].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Groq returned invalid JSON: {e}\n{raw[:300]}")
        return None


def generate_metadata_with_gemini(
    video_path: Path, config: dict, video_title: str = ""
) -> dict | None:
    api_key = config.get("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        log.warning("No Gemini API key — skipping AI metadata.")
        return None

    client = genai.Client(api_key=api_key)
    extra  = config.get("gemini_extra_prompt", "").strip()

    # Text-only generation uses the filename title as automotive context.
    text_prompt = (
        "You are an automotive YouTube Shorts metadata expert.\n\n"
        f"The exact video title is already fixed from its Google Drive filename: {video_title}\n"
        "Do not generate, rewrite, or return a title.\n\n"
        "Return ONLY a valid JSON object — no markdown, no explanation:\n"
        "{\n"
        '  "description": "A concise automotive description followed by relevant hashtags including #Shorts #Cars #Automotive"\n'
        "}\n\n"
        "Rules:\n"
        "- Keep the description accurate to the fixed title; do not invent unsupported specifications.\n"
        "- Never invent specifications, vehicle models, horsepower figures, or technical claims.\n"
        "- Do not return title, tags, or categoryId fields.\n"
        f"{('- Additional: ' + extra) if extra else ''}"
    )

    log.info("Generating metadata with Gemini (text mode)…")
    raw = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=text_prompt,
            )
            raw = response.text.strip()
            break
        except Exception as gen_err:
            err_str = str(gen_err)
            if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str or "quota" in err_str.lower():
                wait = 90 * (attempt + 1)
                log.warning(f"Gemini quota hit — waiting {wait}s before retry ({attempt + 1}/3)…")
                time.sleep(wait)
            else:
                log.error(f"Gemini error: {gen_err}")
                return None
    if raw is None:
        log.error("Gemini failed after 3 retries.")
        return None

    # Strip markdown code fences if Gemini wrapped the JSON
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Gemini returned invalid JSON: {e}\nOutput: {raw[:500]}")
        return None

    return metadata


# ---------------------------------------------------------------------------
# YouTube upload
# ---------------------------------------------------------------------------
def _get_optional_bool(meta: dict, key: str) -> bool:
    """Return an optional per-video boolean setting, defaulting to False."""
    value = meta.get(key, False)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a bool, got {type(value).__name__}")
    return value


def upload_to_youtube(youtube, video_path: Path, meta: dict) -> dict:
    # YouTube requires privacyStatus="private" when publishAt is set
    privacy = "private" if meta.get("publishAt") else meta["privacyStatus"]
    body = {
        "snippet": {
            "title":       meta["title"],
            "description": meta["description"],
            "tags":        meta.get("tags", []),
            "categoryId":  meta["categoryId"],
        },
        "status": {
            "privacyStatus":             privacy,
            "selfDeclaredMadeForKids":   _get_optional_bool(meta, "selfDeclaredMadeForKids"),
            "containsSyntheticMedia":    _get_optional_bool(meta, "containsSyntheticMedia"),
        },
    }
    if meta.get("publishAt"):
        body["status"]["publishAt"] = meta["publishAt"]

    media = MediaFileUpload(
        str(video_path),
        chunksize=4 * 1024 * 1024,
        resumable=True,
    )

    req      = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = req.next_chunk()
        if status:
            log.info(f"  YouTube upload: {int(status.progress() * 100)}%")
    return response


# ---------------------------------------------------------------------------
# Daily job
# ---------------------------------------------------------------------------
def upload_job(channel_config: dict = None, publish_at=None) -> "dict | None":
    """Upload one video. publish_at is an aware datetime for scheduled publishing."""
    log.info("─" * 60)
    log.info("Upload job triggered")

    config       = channel_config if channel_config is not None else load_config()
    token_file   = Path(config["token_file"]) if config.get("token_file") else None
    queue_folder = config.get("drive_queue_folder_id", "").strip()
    list_path    = Path(config.get("uploaded_list", str(BASE_DIR / "uploaded.txt")))

    if not queue_folder:
        log.error(
            "drive_queue_folder_id not set.\n"
            "  Open your Drive folder → copy the ID from the URL:\n"
            "  https://drive.google.com/drive/folders/THIS_IS_THE_ID"
        )
        return None

    youtube, drive = get_google_clients(token_file=token_file)

    # Load already-uploaded IDs and skip them
    skip_ids = load_uploaded_ids(list_path)
    log.info(f"Uploaded so far: {len(skip_ids)} videos")

    video_file = get_next_drive_video(drive, queue_folder, skip_ids=skip_ids)
    if not video_file:
        log.info("No new videos in Drive folder — all have been uploaded.")
        return None

    file_id   = video_file["id"]
    file_name = video_file["name"]
    file_size = int(video_file.get("size", 0))
    log.info(f"Next video: {file_name} ({file_size / 1_048_576:.1f} MB)")

    video_title = Path(file_name).stem.strip()
    if not video_title:
        message = f"Cannot upload Drive file {file_name!r}: filename produces an empty YouTube title."
        log.error(message)
        raise ValueError(message)
    if len(video_title) > 100:
        message = (
            f"Cannot upload Drive file {file_name!r}: filename-derived YouTube title "
            f"is {len(video_title)} characters; maximum is 100."
        )
        log.error(message)
        raise ValueError(message)

    suffix = Path(file_name).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        log.info("Downloading from Google Drive…")
        download_drive_file(drive, file_id, tmp_path)
        log.info("Download complete.")

        # AI metadata is optional and can enrich only the description.
        ai_meta = None
        if config.get("ai_metadata_enabled", True):
            try:
                ai_meta = generate_metadata_with_groq(
                    config, video_path=tmp_path, video_title=video_title
                )
            except Exception as e:
                log.warning(f"Groq metadata failed ({e}) — trying Gemini.")
            if not ai_meta:
                try:
                    ai_meta = generate_metadata_with_gemini(
                        tmp_path, config, video_title=video_title
                    )
                except Exception as e:
                    log.warning(f"Gemini metadata failed ({e}) — using fallback metadata.")
        else:
            log.info("AI metadata disabled — using filename-based automotive metadata.")

        if ai_meta is not None and not isinstance(ai_meta, dict):
            log.warning("AI metadata was not an object — using local automotive metadata.")
            ai_meta = None

        fallback_description = build_default_description(video_title, config)
        ai_description = (ai_meta or {}).get("description")
        description = (
            ai_description.strip()
            if isinstance(ai_description, str) and ai_description.strip()
            else fallback_description
        )
        meta = {
            "title":         video_title,
            "description":   description,
            "tags":          build_video_tags(video_title, config),
            "categoryId":    "2",
            "privacyStatus": config["privacy_status"],
        }

        log.info(f"  Title   : {meta['title']}")
        log.info(f"  Privacy : {meta['privacyStatus']}")

        if publish_at is not None:
            aware = publish_at if publish_at.tzinfo else publish_at.astimezone()
            meta["publishAt"] = aware.isoformat(timespec='seconds')
            log.info(f"  Scheduled for: {aware.strftime('%Y-%m-%d %H:%M %Z')}")

        response = upload_to_youtube(youtube, tmp_path, meta)
        vid_id   = response["id"]
        yt_url   = f"https://youtu.be/{vid_id}"
        log.info(f"YouTube upload complete! {yt_url}")

        # Record in uploaded list — video stays on Drive untouched
        mark_as_uploaded(list_path, file_id, file_name, yt_url)
        log.info(f"Recorded in uploaded list: {list_path.name}")

        return {"video_id": vid_id, "title": meta["title"], "url": yt_url,
                "drive_file_id": file_id, "drive_file_name": file_name}

    except HttpError as e:
        log.error(f"Google API error: {e}")
        raise
    except Exception as e:
        log.error(f"Unexpected error: {e}", exc_info=True)
        raise
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
            log.info("Temp file deleted.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    config       = load_config()
    upload_time  = config.get("upload_time", "09:00")
    hour, minute = map(int, upload_time.split(":"))

    log.info("=" * 60)
    log.info("YouTube Auto Uploader  (Google Drive + AI metadata)")
    log.info(f"  Drive queue folder : {config.get('drive_queue_folder_id') or 'NOT SET'}")
    log.info(f"  Upload time        : {upload_time} (local)")
    log.info(f"  Privacy            : {config['privacy_status']}")
    log.info(f"  AI metadata        : {'enabled' if config.get('gemini_api_key') else 'DISABLED — set gemini_api_key'}")
    log.info("=" * 60)

    log.info("Verifying Google credentials (YouTube + Drive)…")
    get_google_clients()
    log.info("Credentials OK.")

    scheduler = BlockingScheduler()
    scheduler.add_job(upload_job, "cron", hour=hour, minute=minute)
    log.info(f"Scheduler running — uploads daily at {upload_time}. Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    if "--now" in sys.argv:
        upload_job()
    else:
        main()
