"""
Shiur Transcriber & Interactive Player
- Search YUTorah catalog
- Batch process multiple URLs
- Click-to-seek interactive transcript (real HTML5 audio control)
- ElevenLabs / AssemblyAI transcription (cloud)
- Optional local Whisper when faster-whisper is installed
"""

import streamlit as st
import streamlit.components.v1 as components
import os
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import html as html_lib
import base64
import re

from book import build_paragraphs, paragraphs_to_html, paragraphs_to_text

# Optional imports
try:
    from torah_dl import extract as torah_extract
except ImportError:
    torah_extract = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
LIBRARY_DIR = BASE_DIR / "library"
AUDIO_DIR = BASE_DIR / "audio"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"

for d in (LIBRARY_DIR, AUDIO_DIR, TRANSCRIPTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

LOCAL_WHISPER_AVAILABLE = WhisperModel is not None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_secret(name: str) -> str:
    """Read a secret from st.secrets, then environment variables. Never raise if missing."""
    try:
        val = st.secrets.get(name, "")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return (os.environ.get(name) or "").strip()


def resolve_api_key(secret_name: str, label: str) -> str:
    """Use st.secrets / env if set; allow a sidebar text input to override."""
    stored = get_secret(secret_name)
    if stored:
        st.caption(f"{label}: a key is configured in secrets or the environment. Enter one below only to override it.")
    typed = st.text_input(
        label,
        type="password",
        placeholder="Leave blank to use the saved key" if stored else "Paste your API key",
        key=f"key_input_{secret_name}",
    )
    return typed.strip() or stored


def sanitize_elevenlabs_keyterms(terms: list, limit: int = 200) -> list:
    """Scribe keyterms: max 1000 terms, 50 chars, 5 words; no <>{{}}[]\\."""
    cleaned, seen = [], set()
    for raw in terms or []:
        t = re.sub(r"[<>{}[\]\\]", "", str(raw)).strip()
        if not t:
            continue
        words = t.split()
        if len(words) > 5:
            t = " ".join(words[:5])
        if len(t) > 50:
            t = t[:50].rstrip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            cleaned.append(t)
        if len(cleaned) >= limit:
            break
    return cleaned


def sanitize_assembly_terms(terms: list, limit: int = 1000) -> list:
    """word_boost / keyterms_prompt: max 1000 terms, 6 words; punctuation except apostrophes removed."""
    cleaned, seen = [], set()
    for raw in terms or []:
        t = re.sub(r"[^\w\s'’\-]", " ", str(raw), flags=re.UNICODE)
        t = " ".join(t.split())
        if not t:
            continue
        words = t.split()
        if len(words) > 6:
            t = " ".join(words[:6])
        key = t.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(t)
        if len(cleaned) >= limit:
            break
    return cleaned


def format_duration(doc: dict) -> str:
    formatted = doc.get("durationformatted")
    if formatted:
        return str(formatted)
    dur = doc.get("duration")
    if dur:
        return f"{dur} min"
    return "?"


def format_series(doc: dict) -> str:
    series = doc.get("seriesname") or ""
    if isinstance(series, (list, tuple)):
        return ", ".join(str(s) for s in series if s)
    return str(series)


def is_transcribable(doc: dict) -> bool:
    """YUTorah search includes PDFs and other text; only audio/video can be transcribed."""
    cat = (doc.get("mediatypecategory") or "").lower()
    name = (doc.get("mediatypename") or "").lower()
    path = (doc.get("shiurdownloadurl") or "").split("?")[0].lower()
    text_types = {"pdf", "doc", "docx", "txt", "html", "epub"}
    if cat in {"text", "document"} or name in text_types:
        return False
    if any(path.endswith(f".{ext}") for ext in text_types):
        return False
    if cat in {"audio", "video"}:
        return True
    audio_exts = {".mp3", ".m4a", ".wav", ".mp4", ".wma", ".aac", ".ogg", ".flac", ".m4v"}
    return any(path.endswith(ext) for ext in audio_exts)


def safe_filename(title: str, max_len: int = 80) -> str:
    keep = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ "
    cleaned = "".join(c if c in keep else "_" for c in title).strip()
    cleaned = "_".join(cleaned.split())
    return cleaned[:max_len] or "shiur"


def md5_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def load_library() -> list:
    items = []
    for f in LIBRARY_DIR.glob("*.json"):
        try:
            with open(f, encoding="utf-8") as fh:
                items.append(json.load(fh))
        except Exception:
            continue
    items.sort(key=lambda x: x.get("added_at", ""), reverse=True)
    return items


def save_library_item(item: dict):
    path = LIBRARY_DIR / f"{item['id']}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(item, fh, ensure_ascii=False, indent=2)


def group_words_into_sentences(words: list) -> list:
    if not words:
        return []
    sentences = []
    current = []
    for w in words:
        text = w["text"].strip()
        if not text:
            continue
        current.append(w)
        ends = text[-1] in ".!?…" or text.endswith(('."', '."'))
        if ends and len(current) > 2:
            sentences.append({
                "text": " ".join(x["text"].strip() for x in current).strip(),
                "start": current[0]["start"],
                "end": current[-1]["end"],
            })
            current = []
    if current:
        sentences.append({
            "text": " ".join(x["text"].strip() for x in current).strip(),
            "start": current[0]["start"],
            "end": current[-1]["end"],
        })
    if len(sentences) < 3 and len(words) > 20:
        sentences = []
        chunk = []
        for i, w in enumerate(words):
            chunk.append(w)
            if len(chunk) >= 14 or i == len(words) - 1:
                sentences.append({
                    "text": " ".join(x["text"].strip() for x in chunk).strip(),
                    "start": chunk[0]["start"],
                    "end": chunk[-1]["end"],
                })
                chunk = []
    return sentences


# ---------------------------------------------------------------------------
# YUTorah Search API
# ---------------------------------------------------------------------------
def search_yutorah(query: str, page: int = 1) -> dict:
    url = "https://yutorah.org/api/search/get"
    params = {
        "searchTerm": query,
        "getFacets": "false",
        "start": page,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        resp = data.get("response", {})
        return {
            "numFound": resp.get("numFound", 0),
            "docs": resp.get("docs", []),
        }
    except Exception as e:
        st.error(f"YUTorah search failed: {e}")
        return {"numFound": 0, "docs": []}


def teacher_name(doc: dict) -> str:
    return (
        doc.get("teacherfullname")
        or doc.get("teachername")
        or doc.get("TeacherName")
        or "Unknown"
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
GENERIC_TITLES = {"", "rabbi", "rav", "r", "mr", "mrs", "dr", "rebbetzin", "untitled", "untitled shiur"}


def normalize_source_url(url: str) -> str:
    """Canonicalize Torah Anytime IDs / hash links to /lectures/{id} so extract hits the right page."""
    url = (url or "").strip()
    if not url:
        return url
    m = re.search(
        r"(?:torahanytime\.com/(?:#/)?lectures/|(?:torahanytime\.com/?)?#/?lectures\?v=|[?&#]v=)(\d+)",
        url,
        re.I,
    )
    if m and ("torahanytime" in url.lower() or "lectures" in url.lower()):
        return f"https://www.torahanytime.com/lectures/{m.group(1)}"
    m2 = re.search(r"mytat\.me/[av](\d+)", url, re.I)
    if m2:
        return f"https://www.torahanytime.com/lectures/{m2.group(1)}"
    return url


_LECTURE_PATTERNS = [
    # Torah Anytime short links from the app's Share button: MyTAT.me/a123 (audio) / v123 (video)
    re.compile(r"(?:https?://)?(?:www\.)?mytat\.me/[av](\d+)", re.I),
    # torahanytime.com/lectures/123, /#/lectures?v=123, ...?v=123
    re.compile(r"(?:https?://)?(?:www\.)?torahanytime\.com/\S*?(?:lectures/|[?&#]v=)(\d+)", re.I),
]
_YUTORAH_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?yutorah\.org/\S+", re.I)
_ANY_URL_PATTERN = re.compile(r"https?://\S+", re.I)
_TRAILING_JUNK = ".,;:!?)]}>'\"“”’"


def extract_shiur_url(text: str) -> str | None:
    """Pull the shiur link out of anything: a bare URL, a lecture ID, or a whole share
    message (e.g. Torah Anytime's "🎥 Video MyTAT.me/v123 🎙 Audio MyTAT.me/a123 ...").
    Speaker / series links (MyTAT.me/s665) are ignored. Returns a canonical URL or None."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return f"https://www.torahanytime.com/lectures/{text}"
    best = None  # (position, url)
    for pat in _LECTURE_PATTERNS:
        m = pat.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), f"https://www.torahanytime.com/lectures/{m.group(1)}")
    m = _YUTORAH_PATTERN.search(text)
    if m and (best is None or m.start() < best[0]):
        url = m.group(0).rstrip(_TRAILING_JUNK)
        if not url.lower().startswith("http"):
            url = "https://" + url
        best = (m.start(), url)
    if best:
        return best[1]
    for m in _ANY_URL_PATTERN.finditer(text):
        url = m.group(0).rstrip(_TRAILING_JUNK)
        if re.search(r"mytat\.me/|chat\.whatsapp\.com|wa\.me/", url, re.I):
            continue  # speaker/series short links and WhatsApp invites aren't shiurim
        return normalize_source_url(url)
    return None


def fix_download_url(download_url: str) -> str:
    """torah-dl currently strips backslashes from \\u0026, leaving a literal 'u0026' that 403s."""
    if not download_url:
        return download_url
    return download_url.replace("\\u0026", "&").replace("u0026", "&")


def fallback_page_title(url: str, current: str) -> str:
    cur = (current or "").strip()
    # torah-dl sometimes returns just the honorific ("Rabbi", "Mr.") as the title
    if cur.lower().rstrip(".'") not in GENERIC_TITLES and len(cur.split()) > 1:
        return current
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        m = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            r.text,
            re.I,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
                r.text,
                re.I,
            )
        if not m:
            m = re.search(r"<title>(.*?)</title>", r.text, re.I | re.S)
        if m:
            title = html_lib.unescape(re.sub(r"<.*?>", "", m.group(1), flags=re.S)).strip()
            if title:
                return title
    except Exception:
        pass
    return current or "Untitled Shiur"


def download_from_url(url: str) -> dict | None:
    if torah_extract is None:
        st.error("torah-dl not installed")
        return None
    url = normalize_source_url(url)
    try:
        extraction = torah_extract(url)
    except Exception as e:
        st.error(f"Extract failed: {e}")
        return None
    if not extraction or not getattr(extraction, "download_url", None):
        st.error("No audio found for this URL")
        return None

    title = getattr(extraction, "title", None) or "Untitled Shiur"
    if "torahanytime.com" in url.lower():
        title = fallback_page_title(url, title)
    download_url = fix_download_url(extraction.download_url)
    raw_name = getattr(extraction, "file_name", None) or ""
    raw_name = raw_name.split("?")[0].split("#")[0]
    file_name = raw_name if raw_name and "." in raw_name else f"{safe_filename(title)}.mp3"
    shiur_id = md5_id(url)
    audio_path = AUDIO_DIR / f"{shiur_id}_{file_name}"

    if not audio_path.exists():
        try:
            r = requests.get(download_url, stream=True, timeout=180)
            r.raise_for_status()
            with open(audio_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=16384):
                    f.write(chunk)
        except Exception as e:
            st.error(f"Download failed: {e}")
            return None

    return {
        "id": shiur_id,
        "title": title,
        "source_url": url,
        "download_url": download_url,
        "audio_path": str(audio_path),
        "file_name": file_name,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }


def download_from_yutorah_doc(doc: dict) -> dict | None:
    title = doc.get("shiurtitle") or "Untitled"
    download_url = doc.get("shiurdownloadurl")
    shiur_id = str(doc.get("shiurid") or md5_id(title))
    if not download_url:
        details = f"https://www.yutorah.org/lectures/details?shiurid={shiur_id}"
        return download_from_url(details)

    file_name = f"{safe_filename(title)}.mp3"
    audio_path = AUDIO_DIR / f"{shiur_id}_{file_name}"

    if not audio_path.exists():
        try:
            r = requests.get(
                download_url, stream=True, timeout=180,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            r.raise_for_status()
            with open(audio_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=16384):
                    f.write(chunk)
        except Exception as e:
            st.warning(f"Direct download failed, trying torah-dl: {e}")
            details = f"https://www.yutorah.org/lectures/details?shiurid={shiur_id}"
            return download_from_url(details)

    return {
        "id": shiur_id,
        "title": title,
        "source_url": f"https://www.yutorah.org/lectures/details?shiurid={shiur_id}",
        "download_url": download_url,
        "audio_path": str(audio_path),
        "file_name": file_name,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "teacher": teacher_name(doc),
        "duration_min": doc.get("duration"),
    }


# ---------------------------------------------------------------------------
# Transcription backends
# ---------------------------------------------------------------------------
def transcribe_elevenlabs(audio_path: str, api_key: str, keyterms: list | None = None) -> list | None:
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        try:
            from elevenlabs import ElevenLabs
        except ImportError:
            st.error("pip install elevenlabs")
            return None
    client = ElevenLabs(api_key=api_key)
    kwargs = dict(
        model_id="scribe_v2",
        diarize=True,          # speaker labels in the book view when speakers change
        tag_audio_events=False,
        no_verbatim=True,      # drop "um", "uh", false starts
    )
    # keyterms / keyterm prompting (up to 1000 terms on Scribe; we send up to 200)
    if keyterms:
        kwargs["keyterms"] = sanitize_elevenlabs_keyterms(keyterms, limit=200)
    try:
        with open(audio_path, "rb") as f:
            result = client.speech_to_text.convert(file=f, **kwargs)
    except TypeError:
        # older SDK without some params – retry without them
        kwargs.pop("keyterms", None)
        kwargs.pop("no_verbatim", None)
        try:
            with open(audio_path, "rb") as f:
                result = client.speech_to_text.convert(file=f, **kwargs)
        except Exception as e:
            st.error(f"ElevenLabs: {e}")
            return None
    except Exception as e:
        st.error(f"ElevenLabs: {e}")
        return None

    words = []
    for w in getattr(result, "words", []) or []:
        if getattr(w, "type", "word") != "word":
            continue
        text = (getattr(w, "text", "") or "").strip()
        start, end = getattr(w, "start", None), getattr(w, "end", None)
        if text and start is not None and end is not None:
            wd = {"text": text, "start": float(start), "end": float(end)}
            sp = getattr(w, "speaker_id", None)
            if sp is not None:
                wd["speaker"] = str(sp)
            words.append(wd)
    if not words and getattr(result, "text", None):
        words = [{"text": result.text, "start": 0.0, "end": 0.0}]
    return words


def transcribe_assemblyai(audio_path: str, api_key: str, keyterms: list | None = None) -> list | None:
    try:
        import assemblyai as aai
    except ImportError:
        st.error("pip install assemblyai")
        return None
    aai.settings.api_key = api_key
    cfg_kwargs = dict(
        language_detection=True,
        punctuate=True,
        format_text=True,
        disfluencies=False,   # leave out "um", "uh"
        speaker_labels=True,
    )
    boost = sanitize_assembly_terms(keyterms, limit=1000) if keyterms else []
    if boost:
        # Newer models prefer keyterms_prompt; word_boost still works (Universal-2).
        try:
            import inspect
            params = inspect.signature(aai.TranscriptionConfig.__init__).parameters
        except (TypeError, ValueError):
            params = {}
        if "keyterms_prompt" in params:
            cfg_kwargs["keyterms_prompt"] = boost
        else:
            cfg_kwargs["word_boost"] = boost
            cfg_kwargs["boost_param"] = "high"
    try:
        transcript = aai.Transcriber().transcribe(audio_path, aai.TranscriptionConfig(**cfg_kwargs))
        if transcript.status == aai.TranscriptStatus.error and "speaker" in (transcript.error or "").lower():
            # some languages don't support speaker labels – retry without them
            cfg_kwargs.pop("speaker_labels", None)
            transcript = aai.Transcriber().transcribe(audio_path, aai.TranscriptionConfig(**cfg_kwargs))
    except Exception as e:
        st.error(f"AssemblyAI: {e}")
        return None
    if transcript.status == aai.TranscriptStatus.error:
        st.error(transcript.error)
        return None
    # AssemblyAI's own paragraph breaks (GET /v2/transcript/{id}/paragraphs)
    para_bounds = []
    try:
        para_bounds = [((p.start or 0) / 1000.0, (p.end or 0) / 1000.0) for p in transcript.get_paragraphs()]
    except Exception:
        para_bounds = []
    words = []
    pi = 0
    for w in transcript.words or []:
        text_w = (w.text or "").strip()
        if text_w:
            wd = {
                "text": text_w,
                "start": (w.start or 0) / 1000.0,
                "end": (w.end or 0) / 1000.0,
            }
            if getattr(w, "speaker", None):
                wd["speaker"] = str(w.speaker)
            if para_bounds:
                while pi < len(para_bounds) - 1 and wd["start"] >= para_bounds[pi + 1][0] - 1e-3:
                    pi += 1
                wd["p"] = pi
            words.append(wd)
    return words


def transcribe_local(audio_path: str, model_size: str = "base") -> list | None:
    if WhisperModel is None:
        st.error(
            "Local Whisper is not available here (faster-whisper is not installed). "
            "Choose ElevenLabs or AssemblyAI, or install requirements-local.txt."
        )
        return None
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(audio_path, word_timestamps=True)
        words = []
        for seg in segments:
            for w in seg.words or []:
                words.append({
                    "text": w.word.strip(),
                    "start": float(w.start),
                    "end": float(w.end),
                })
        return words
    except Exception as e:
        st.error(f"Local Whisper: {e}")
        return None


def run_transcription(audio_path: str, provider: str, eleven_key: str, assembly_key: str, local_model: str, keyterms: list | None = None):
    if provider == "ElevenLabs Scribe":
        if not eleven_key:
            st.warning("Enter ElevenLabs API key in sidebar")
            return None
        return transcribe_elevenlabs(audio_path, eleven_key, keyterms=keyterms)
    elif provider == "AssemblyAI":
        if not assembly_key:
            st.warning("Enter AssemblyAI API key in sidebar")
            return None
        return transcribe_assemblyai(audio_path, assembly_key, keyterms=keyterms)
    else:
        return transcribe_local(audio_path, local_model)


# ---------------------------------------------------------------------------
# Interactive player HTML (real click-to-seek)
# ---------------------------------------------------------------------------
def render_interactive_player(audio_path: str, sentences: list, height: int = 680):
    try:
        size_mb = Path(audio_path).stat().st_size / (1024 * 1024)
        if size_mb > 20:
            st.warning(
                f"This audio file is {size_mb:.0f} MB. Embedding it in the click-to-seek "
                "player can be slow or run out of memory on Streamlit Community Cloud (~1 GB RAM)."
            )
    except OSError:
        pass
    with open(audio_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    rows = []
    for i, s in enumerate(sentences):
        start = s["start"]
        text = html_lib.escape(s["text"])
        rows.append(
            f'<div class="sent" data-start="{start:.3f}" id="s{i}">'
            f'<span class="ts">{start:.1f}s</span>'
            f'<span class="tx">{text}</span></div>'
        )
    sentences_html = "\n".join(rows)

    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 8px; background: #0e1117; color: #fafafa; }}
  audio {{ width: 100%; margin-bottom: 12px; }}
  #controls {{ display: flex; gap: 8px; margin-bottom: 10px; align-items: center; flex-wrap: wrap; }}
  button {{ background: #262730; color: #fafafa; border: 1px solid #444; border-radius: 6px; padding: 6px 12px; cursor: pointer; }}
  button:hover {{ background: #3a3b45; }}
  #transcript {{ max-height: 460px; overflow-y: auto; border: 1px solid #333; border-radius: 8px; padding: 6px; }}
  .sent {{ padding: 8px 10px; border-radius: 6px; cursor: pointer; display: flex; gap: 12px; margin-bottom: 2px; }}
  .sent:hover {{ background: #1e1f26; }}
  .sent.active {{ background: #1a3a5c; border-left: 3px solid #4da3ff; }}
  .ts {{ color: #8ab4f8; font-size: 0.8rem; min-width: 48px; flex-shrink: 0; }}
  .tx {{ line-height: 1.45; }}
  #status {{ font-size: 0.85rem; color: #aaa; }}
</style>
</head>
<body>
  <audio id="player" controls preload="auto" src="data:audio/mpeg;base64,{b64}"></audio>
  <div id="controls">
    <button onclick="skip(-15)">-15s</button>
    <button onclick="skip(-5)">-5s</button>
    <button onclick="togglePlay()">Play / Pause</button>
    <button onclick="skip(5)">+5s</button>
    <button onclick="skip(15)">+15s</button>
    <span id="status">Ready – click any sentence to jump</span>
  </div>
  <div id="transcript">
    {sentences_html}
  </div>
<script>
  const player = document.getElementById('player');
  const status = document.getElementById('status');
  const sents = document.querySelectorAll('.sent');

  function togglePlay() {{
    if (player.paused) player.play(); else player.pause();
  }}
  function skip(sec) {{
    player.currentTime = Math.max(0, player.currentTime + sec);
  }}
  function seekTo(t) {{
    player.currentTime = t;
    player.play();
    status.textContent = 'Playing from ' + t.toFixed(1) + 's';
  }}

  sents.forEach(el => {{
    el.addEventListener('click', () => {{
      const t = parseFloat(el.dataset.start);
      seekTo(t);
      sents.forEach(s => s.classList.remove('active'));
      el.classList.add('active');
      el.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
    }});
  }});

  player.addEventListener('timeupdate', () => {{
    const t = player.currentTime;
    let current = null;
    sents.forEach(el => {{
      const start = parseFloat(el.dataset.start);
      const next = el.nextElementSibling ? parseFloat(el.nextElementSibling.dataset.start) : 1e9;
      if (t >= start && t < next) current = el;
    }});
    sents.forEach(s => s.classList.remove('active'));
    if (current) current.classList.add('active');
  }});
</script>
</body>
</html>
"""
    components.html(html, height=height, scrolling=True)



def sentences_to_srt(sentences: list) -> str:
    """Convert sentence list to SRT subtitle format."""
    lines = []
    for i, s in enumerate(sentences, 1):
        start = s.get("start", 0)
        end = s.get("end", start + 3)
        # SRT time format: HH:MM:SS,mmm
        def fmt(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            sec = int(t % 60)
            ms = int((t % 1) * 1000)
            return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
        lines.append(str(i))
        lines.append(f"{fmt(start)} --> {fmt(end)}")
        lines.append(s.get("text", "").replace("\n", " "))
        lines.append("")
    return "\n".join(lines)


def sentences_to_vtt(sentences: list) -> str:
    """Convert sentence list to WebVTT subtitle format."""
    lines = ["WEBVTT", ""]
    for s in sentences:
        start = s.get("start", 0)
        end = s.get("end", start + 3)
        # VTT time format: HH:MM:SS.mmm
        def fmt(t):
            h = int(t // 3600)
            m = int((t % 3600) // 60)
            sec = int(t % 60)
            ms = int((t % 1) * 1000)
            return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"
        lines.append(f"{fmt(start)} --> {fmt(end)}")
        lines.append(s.get("text", "").replace("\n", " "))
        lines.append("")
    return "\n".join(lines)


def render_book_view(item: dict, audio_path: str, data: dict, kp: str = ""):
    """Book-style reader: clean paragraphs, text size, downloads."""
    paras = build_paragraphs(data.get("words") or data.get("sentences", []))
    subtitle_bits = [b for b in (item.get("teacher"), item.get("provider")) if b and b != "Unknown"]
    c_size, c_times = st.columns([3, 2])
    with c_size:
        size = st.select_slider(
            "Text size", options=["Small", "Medium", "Large", "Extra large"],
            value="Medium", key=f"{kp}book_size",
        )
    with c_times:
        show_times = st.toggle("Show times", value=False, key=f"{kp}book_times")
    font_px = {"Small": 16, "Medium": 19, "Large": 22, "Extra large": 26}[size]
    with st.expander("🎧 Listen while reading", expanded=False):
        st.audio(audio_path)
    st.markdown(
        paragraphs_to_html(paras, title=item.get("title", ""),
                           subtitle=" · ".join(subtitle_bits),
                           font_px=font_px, show_times=show_times),
        unsafe_allow_html=True,
    )
    st.caption(f"{len(paras)} paragraphs")
    base_name = safe_filename(item.get("title", "shiur"), 60)
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "⬇️ Download as text (.txt)",
            data=paragraphs_to_text(paras, item.get("title", "")),
            file_name=f"{base_name}.txt", mime="text/plain",
            key=f"{kp}dl_book_txt_{item['id']}",
        )
    with d2:
        st.download_button(
            "⬇️ Download as Markdown (.md)",
            data=paragraphs_to_text(paras, item.get("title", ""), markdown=True),
            file_name=f"{base_name}.md", mime="text/markdown",
            key=f"{kp}dl_book_md_{item['id']}",
        )

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Shiur Transcriber",
    page_icon="📖",
    layout="wide",
    initial_sidebar_state="auto",  # collapsed on phones so the reader gets the full screen
)

st.title("📖 Shiur Transcriber & Interactive Player")
st.caption(
    "YUTorah search • Batch URLs • Click-to-seek transcript • ElevenLabs / AssemblyAI"
    + (" / Local Whisper" if LOCAL_WHISPER_AVAILABLE else "")
)
st.caption(
    "On Streamlit Community Cloud, audio and transcripts are stored on an ephemeral disk "
    "and are wiped when the app restarts."
)

with st.sidebar:
    st.header("Transcription Settings")
    providers = ["ElevenLabs Scribe", "AssemblyAI"]
    if LOCAL_WHISPER_AVAILABLE:
        providers.append("Local (faster-whisper)")
    provider = st.radio("Provider", providers, index=0)
    if not LOCAL_WHISPER_AVAILABLE:
        st.caption(
            "Local (faster-whisper) is not installed on this host — it is too heavy for "
            "the Streamlit Community Cloud free tier. Use ElevenLabs or AssemblyAI, or "
            "install `requirements-local.txt` to enable it locally."
        )
    eleven_key = resolve_api_key("ELEVENLABS_API_KEY", "ElevenLabs API Key")
    assembly_key = resolve_api_key("ASSEMBLYAI_API_KEY", "AssemblyAI API Key")
    local_model = "base"
    if provider == "Local (faster-whisper)":
        local_model = st.selectbox("Model size", ["tiny", "base", "small", "medium"], index=1)

    st.divider()
    st.subheader("Custom vocabulary")
    st.caption("One term per line. Boosts accuracy for sefarim, Hebrew words, and speaker names.")

    with st.expander("Torah / Halacha terms", expanded=False):
        vocab_text = st.text_area(
            "Sefarim & terms",
            value="""Rashi
Tosafot
Gemara
Halacha
Mitzvah
Shabbos
Shabbat
Hashem
Baruch Hashem
Chazal
Rambam
Ramban
Mishnah
Berachot
Berachos
Kiddushin
Ketubot
Bava Kamma
Bava Metzia
Bava Batra
Sanhedrin
Yevamot
Niddah
Chullin
Pesachim
Sukkah
Rosh Hashanah
Yom Kippur
Tefillah
Shemoneh Esrei
Amidah
Shema
Mezuzah
Tefillin
Tzitzit
Kashrut
Treif
Pareve
Cholent
Shiur
Daf Yomi
Parsha
Parashah
Chumash
Tanakh
Neviim
Ketuvim
Midrash
Aggadah
Mussar
Hashkafa
Emunah
Bitachon
Teshuva
Teshuvah
Aveilut
Avelut
Minhag
Piskei
Poskim
Shulchan Aruch
Rema
Mishnah Berurah
Aruch HaShulchan
Igrot Moshe
Beit Yosef
Tur
Rif
Rosh
Shach
Taz
Kitzur
Mishneh Torah
Moreh Nevuchim
Kuzari
Sefer HaChinuch
Sefer HaMitzvot
Or HaChaim
Sforno
Ibn Ezra
Targum
Onkelos
Yerushalmi
Bavli
Masechet
Daf
Amud
Sugya
Chiddush
Kushya
Terutz
Sevara
Machlokes
Machloket
Tanna
Amora
Geonim
Rishonim
Acharonim
Shulchan Aruch HaRav
Alter Rebbe
Baal HaTanya
Tanya
Likutei Torah
Torah Or
Maamar
Sichos
Sichot
Farbrengen
Chassidus
Chassidut
Lubavitch
Breslov
Chabad
Rebbe
Rebbetzin
Rav
Rabbi
Rosh Yeshiva
Maggid Shiur
Talmid
Talmidim
Beit Midrash
Yeshiva
Kollel
Semicha
Smicha
Psak
Heter
Issur
Mutar
Assur
Karet
Malkot
Ones
Shogeg
Mezid
Pesach
Matzah
Maror
Seder
Haggadah
Sukkot
Lulav
Etrog
Chanukah
Menorah
Purim
Megillah
Esther
Tisha B'Av
Kinnot
Selichot
Viduy
Neilah
Musaf
Mincha
Maariv
Shacharit
Aleinu
Kaddish
Kedusha
Birkat Hamazon
Bentsching
Netilat Yadayim
Negel Vasser
Tallit
Eruv
Muktzeh
Melacha
Yom Tov
Chol HaMoed
Rosh Chodesh
Havdalah
HaMotzi
Mezonot
Shehakol
Ashrei
Hallel
Hoshanot
Hakafot
Simchat Torah
Shemini Atzeret
Shavuot
Akdamut
Megillat Ruth
Kohelet
Eichah
Shir HaShirim
Tehillim
Mishlei
Iyov
Daniel
Ezra
Nechemia
Divrei HaYamim
Yehoshua
Shoftim
Shmuel
Melachim
Yeshayahu
Yirmiyahu
Yechezkel
Trei Asar""",
            height=200,
            help="Sefarim, tractates, mitzvot, and common Hebrew terms.",
            key="vocab_terms",
        )

    with st.expander("Speaker / Rabbi names (boosting)", expanded=False):
        speaker_text = st.text_area(
            "Common speakers",
            value="""Rabbi Soloveitchik
Rav Soloveitchik
the Rav
Rabbi Schachter
Rav Schachter
Rabbi Hershel Schachter
Rabbi Willig
Rav Willig
Rabbi Moshe Willig
Rabbi Twersky
Rav Twersky
Rabbi Mayer Twersky
Rabbi Rosensweig
Rav Rosensweig
Rabbi Michael Rosensweig
Rabbi Bleich
Rav Bleich
Rabbi J. David Bleich
Rabbi Tendler
Rav Tendler
Rabbi Moshe Dovid Tendler
Rabbi Miller
Rav Miller
Rabbi Avigdor Miller
Rabbi Frand
Rav Frand
Rabbi Yissocher Frand
Rabbi Kamenetsky
Rav Kamenetsky
Rabbi Yaakov Kamenetsky
Rabbi Feinstein
Rav Feinstein
Rabbi Moshe Feinstein
Rabbi Kotler
Rav Kotler
Rabbi Aharon Kotler
Rabbi Ruderman
Rav Ruderman
Rabbi Yaakov Yitzchok Ruderman
Rabbi Hutner
Rav Hutner
Rabbi Yitzchok Hutner
Rabbi Dessler
Rav Dessler
Rabbi Eliyahu Dessler
Rabbi Wolbe
Rav Wolbe
Rabbi Shlomo Wolbe
Rabbi Aviner
Rav Aviner
Rabbi Shlomo Aviner
Rabbi Riskin
Rav Riskin
Rabbi Shlomo Riskin
Rabbi Lamm
Rav Lamm
Rabbi Norman Lamm
Rabbi Lookstein
Rav Lookstein
Rabbi Haskel Lookstein
Rabbi Berman
Rav Berman
Rabbi Saul Berman
Rabbi Blau
Rav Blau
Rabbi Yosef Blau
Rabbi Charlop
Rav Charlop
Rabbi Zevulun Charlop
Rabbi Goldwicht
Rav Goldwicht
Rabbi Meir Goldwicht
Rabbi Kahn
Rav Kahn
Rabbi Aharon Kahn
Rabbi Reichman
Rav Reichman
Rabbi Hershel Reichman""",
            height=150,
            help="Rabbi names commonly heard in shiurim.",
            key="vocab_speakers",
        )

    terms = [t.strip() for t in vocab_text.splitlines() if t.strip()]
    speakers = [t.strip() for t in speaker_text.splitlines() if t.strip()]
    keyterms = terms + speakers
    st.caption(f"Boosting {len(keyterms)} terms total")

    st.divider()
    st.markdown("**Get free keys**")
    st.markdown("- [ElevenLabs](https://elevenlabs.io) → API Key")
    st.markdown("- [AssemblyAI](https://www.assemblyai.com) → API Key")

# ---------------------------------------------------------------------------
# Quick add: link from ?url=..., clipboard button, or paste box (auto-starts)
# ---------------------------------------------------------------------------
_LINK_CATCHER_HTML = """
<div class="lc">
  <button type="button" class="lc-btn">📋 Paste link &amp; transcribe</button>
  <input class="lc-in" type="text" inputmode="url" autocomplete="off"
         placeholder="…or paste a Torah Anytime / YUTorah link here" />
  <div class="lc-msg"></div>
</div>
"""
_LINK_CATCHER_CSS = """
.lc { display:flex; flex-direction:column; gap:10px; font-family: var(--st-font, sans-serif); }
.lc-btn { font-size:1.15rem; font-weight:600; padding:16px 12px; border-radius:12px; border:none;
          background: var(--st-primary-color, #ff4b4b); color:#fff; cursor:pointer; width:100%; }
.lc-btn:active { opacity:.8; }
.lc-in { font-size:1rem; padding:12px; border-radius:10px; width:100%; box-sizing:border-box;
         border:1px solid rgba(128,128,128,.5); background: transparent; color: inherit; }
.lc-msg { font-size:.9rem; min-height:1.1em; opacity:.85; }
"""
_LINK_CATCHER_JS = r"""
const LECTURE_RE = /(mytat\.me\/[av]\d+|torahanytime\.com\/\S*(lectures\/|[?&#]v=)\d+|yutorah\.org\/\S*\d{3,})/i;
export default function(component) {
  const { setTriggerValue, parentElement } = component;
  const btn = parentElement.querySelector('.lc-btn');
  const inp = parentElement.querySelector('.lc-in');
  const msg = parentElement.querySelector('.lc-msg');
  let last = '';
  const send = (text, how) => {
    text = (text || '').trim();
    if (!text) { msg.textContent = 'Clipboard is empty — copy the shiur link first.'; return; }
    if (!LECTURE_RE.test(text) && !/^\d+$/.test(text) && !/https?:\/\//i.test(text)) {
      msg.textContent = "That doesn't look like a shiur link.";
      return;
    }
    if (text === last) return;
    last = text;
    msg.textContent = 'Got it — starting…';
    setTriggerValue('link', text + '\u0000' + how + '\u0000' + Date.now());
  };
  btn.onclick = async () => {
    try {
      const t = await navigator.clipboard.readText();
      inp.value = t || '';
      send(t, 'clipboard');
    } catch (e) {
      msg.textContent = "Couldn't read the clipboard. Long-press the box below and tap Paste.";
      inp.focus();
    }
  };
  const onTyped = () => {
    const t = inp.value;
    if (LECTURE_RE.test(t) || /^\s*\d{4,}\s*$/.test(t)) send(t, 'paste');
  };
  inp.oninput = onTyped;
  inp.onpaste = () => setTimeout(onTyped, 0);
  inp.onkeydown = (e) => { if (e.key === 'Enter') send(inp.value, 'enter'); };
}
"""

_link_catcher = None
try:
    import streamlit.components.v2 as _cv2
    _link_catcher = _cv2.component(
        "shiur_link_catcher", html=_LINK_CATCHER_HTML, css=_LINK_CATCHER_CSS, js=_LINK_CATCHER_JS,
    )
except Exception:
    _link_catcher = None


def find_library_item(item_id: str) -> dict | None:
    p = LIBRARY_DIR / f"{item_id}.json"
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            it = json.load(fh)
    except Exception:
        return None
    tp = it.get("transcript_path")
    return it if tp and Path(tp).exists() else None


def quick_transcribe(url: str) -> dict | None:
    """Download + transcribe one link, save to the library. Reuses an existing transcript."""
    existing = find_library_item(md5_id(normalize_source_url(url)))
    if existing:
        return existing
    with st.status("Working on your shiur…", expanded=True) as status:
        st.write(f"Downloading `{url}`")
        meta = download_from_url(url)
        if not meta:
            status.update(label="Download failed", state="error")
            return None
        st.write(f"**{meta['title']}** — transcribing with {provider}. Long shiurim can take a few minutes; keep this page open.")
        words = run_transcription(meta["audio_path"], provider, eleven_key, assembly_key, local_model, keyterms=keyterms)
        if not words:
            status.update(label="Transcription failed", state="error")
            return None
        sentences = group_words_into_sentences(words)
        tp = TRANSCRIPTS_DIR / f"{meta['id']}.json"
        with open(tp, "w", encoding="utf-8") as f:
            json.dump({"words": words, "sentences": sentences}, f, ensure_ascii=False, indent=2)
        meta["transcript_path"] = str(tp)
        meta["num_sentences"] = len(sentences)
        meta["provider"] = provider
        if "torahanytime.com" in url.lower():
            meta["source"] = "TorahAnytime"
        save_library_item(meta)
        status.update(label=f"Done — {meta['title']}", state="complete", expanded=False)
    return meta


st.subheader("⚡ Quick add")
st.caption("In Torah Anytime tap **Share → Copy**, then come back here and tap the button.")

pending = None
# 1) Link handed over in the address: ?url=<link or whole share text>
qp_raw = st.query_params.get("url") or st.query_params.get("text") or st.query_params.get("link")
if qp_raw:
    qp_url = extract_shiur_url(qp_raw)
    if qp_url and st.session_state.get("qp_done") != qp_raw:
        st.session_state["qp_done"] = qp_raw
        pending = qp_url
    elif not qp_url:
        st.warning("The link that was passed in doesn't look like a shiur link.")

# 2) Clipboard button / auto-start paste box
if _link_catcher is not None:
    lc = _link_catcher(key="link_catcher", on_link_change=lambda: None)
    raw = getattr(lc, "link", None) if lc is not None else None
    if raw:
        text = str(raw).split("\u0000")[0]
        got = extract_shiur_url(text)
        if got:
            pending = got
        else:
            st.warning("Couldn't find a shiur link in what was pasted.")
else:
    typed = st.text_input("Paste a Torah Anytime / YUTorah link", key="quick_link")
    if typed and st.session_state.get("quick_link_done") != typed:
        got = extract_shiur_url(typed)
        if got:
            st.session_state["quick_link_done"] = typed
            pending = got

if pending:
    done = quick_transcribe(pending)
    if done:
        st.session_state["quick_item_id"] = done["id"]
    if "url" in st.query_params or "text" in st.query_params or "link" in st.query_params:
        st.query_params.clear()  # so a refresh doesn't start it again

qid = st.session_state.get("quick_item_id")
if qid:
    qitem = find_library_item(qid)
    if qitem:
        with open(qitem["transcript_path"], encoding="utf-8") as f:
            qdata = json.load(f)
        top_l, top_r = st.columns([4, 1])
        with top_l:
            st.success(f"Ready: **{qitem['title']}** (also saved in My Library)")
        with top_r:
            if st.button("✖ Close", key="quick_close"):
                st.session_state.pop("quick_item_id", None)
                st.rerun()
        render_book_view(qitem, qitem["audio_path"], qdata, kp="quick_")

st.divider()

tab_search, tab_tat, tab_batch, tab_library, tab_fullsearch = st.tabs([
    "🔎 Search YUTorah",
    "🟠 Torah Anytime",
    "📦 Batch / Paste URLs",
    "📚 My Library & Reader",
    "🔍 Search My Transcripts",
])

# ---------------------------------------------------------------------------
# Tab: Search YUTorah
# ---------------------------------------------------------------------------
with tab_search:
    st.subheader("Search the YUTorah catalog")
    q = st.text_input("Search term", placeholder="e.g. Rashi Vayishlach, hilchos shabbos, Rabbi Schachter…")
    col_a, col_b = st.columns([1, 4])
    with col_a:
        page = st.number_input("Page", min_value=1, value=1, step=1)

    if st.button("Search", type="primary"):
        with st.spinner("Searching YUTorah…"):
            result = search_yutorah(q, page=page)
        st.session_state["search_result"] = result
        st.session_state["search_q"] = q

    result = st.session_state.get("search_result")
    if result:
        st.write(f"**{result['numFound']} results** for “{st.session_state.get('search_q','')}” (page {page})")
        for doc in result["docs"]:
            title = doc.get("shiurtitle", "—")
            teacher = teacher_name(doc)
            dur = format_duration(doc)
            series = format_series(doc)
            sid = doc.get("shiurid")
            media = doc.get("mediatypename") or doc.get("mediatypecategory") or ""

            c1, c2 = st.columns([5, 1])
            with c1:
                st.markdown(f"**{title}**")
                st.caption(f"{teacher} • {series} • {dur} • {media} • ID {sid}")
            with c2:
                can_transcribe = is_transcribable(doc)
                if not can_transcribe:
                    st.caption("Not audio")
                if st.button("Add + Transcribe", key=f"add_{sid}", disabled=not can_transcribe):
                    with st.status("Processing…", expanded=True) as status:
                        st.write("Downloading…")
                        meta = download_from_yutorah_doc(doc)
                        if meta:
                            st.write("Transcribing…")
                            words = run_transcription(meta["audio_path"], provider, eleven_key, assembly_key, local_model, keyterms=keyterms)
                            if words:
                                sentences = group_words_into_sentences(words)
                                tp = TRANSCRIPTS_DIR / f"{meta['id']}.json"
                                with open(tp, "w", encoding="utf-8") as f:
                                    json.dump({"words": words, "sentences": sentences}, f, ensure_ascii=False, indent=2)
                                meta["transcript_path"] = str(tp)
                                meta["num_sentences"] = len(sentences)
                                meta["provider"] = provider
                                save_library_item(meta)
                                status.update(label=f"Done – {len(sentences)} sentences", state="complete")
                                st.success(f"Added **{meta['title']}**")
                            else:
                                status.update(label="Transcription failed", state="error")
                        else:
                            status.update(label="Download failed", state="error")
            st.divider()

# ---------------------------------------------------------------------------
# Tab: Batch / Paste URLs
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tab: Torah Anytime
# ---------------------------------------------------------------------------
with tab_tat:
    st.subheader("Torah Anytime")
    st.caption(
        "Torah Anytime does not expose a public search API. "
        "Paste lecture URLs (or IDs) below, or open the site, search there, and copy links here."
    )
    st.markdown(
        "Open [torahanytime.com](https://www.torahanytime.com) → search → copy the lecture URL "
        "(looks like `https://www.torahanytime.com/lectures/123456` or a share link)."
    )

    tat_urls = st.text_area(
        "Torah Anytime URLs or lecture IDs (one per line)",
        height=140,
        placeholder="https://www.torahanytime.com/lectures/344820\n486699\nhttps://www.torahanytime.com/#/lectures?v=67173",
        key="tat_urls",
    )

    if st.button("Download & Transcribe TAT", type="primary", key="tat_go"):
        lines = [ln.strip() for ln in tat_urls.splitlines() if ln.strip()]
        urls = []
        for ln in lines:
            got = extract_shiur_url(ln)
            if not got and ("v=" in ln or "lectures" in ln.lower()):
                got = normalize_source_url(f"https://www.torahanytime.com/{ln.lstrip('#/')}")
            if got:
                if got not in urls:
                    urls.append(got)
            else:
                st.warning(f"Skipped unrecognized line: {ln}")
        if not urls:
            st.warning("No valid URLs / IDs")
        else:
            progress = st.progress(0)
            status_box = st.empty()
            for i, url in enumerate(urls):
                status_box.info(f"({i+1}/{len(urls)}) {url[:90]}")
                meta = download_from_url(url)
                if meta:
                    words = run_transcription(
                        meta["audio_path"], provider, eleven_key, assembly_key, local_model, keyterms=keyterms
                    )
                    if words:
                        sentences = group_words_into_sentences(words)
                        tp = TRANSCRIPTS_DIR / f"{meta['id']}.json"
                        with open(tp, "w", encoding="utf-8") as f:
                            json.dump({"words": words, "sentences": sentences}, f, ensure_ascii=False, indent=2)
                        meta["transcript_path"] = str(tp)
                        meta["num_sentences"] = len(sentences)
                        meta["provider"] = provider
                        meta["source"] = "TorahAnytime"
                        save_library_item(meta)
                        st.success(f"✅ {meta['title']} ({len(sentences)} sentences)")
                    else:
                        st.error(f"Transcription failed: {url}")
                else:
                    st.error(f"Download failed: {url}")
                progress.progress((i + 1) / len(urls))
            status_box.success(f"Finished {len(urls)} item(s)")

with tab_batch:
    st.subheader("Paste one or more shiur URLs")
    st.caption("YUTorah or Torah Anytime – one URL per line")
    urls_text = st.text_area("URLs", height=150, placeholder="https://www.yutorah.org/lectures/details?shiurid=...\nhttps://www.torahanytime.com/...")

    if st.button("Process all", type="primary"):
        urls = []
        for u in urls_text.splitlines():
            got = extract_shiur_url(u)
            if got and got not in urls:
                urls.append(got)
        if not urls:
            st.warning("No valid URLs found")
        else:
            progress = st.progress(0)
            status_box = st.empty()
            for i, url in enumerate(urls):
                status_box.info(f"({i+1}/{len(urls)}) {url[:80]}…")
                meta = download_from_url(url)
                if meta:
                    words = run_transcription(meta["audio_path"], provider, eleven_key, assembly_key, local_model, keyterms=keyterms)
                    if words:
                        sentences = group_words_into_sentences(words)
                        tp = TRANSCRIPTS_DIR / f"{meta['id']}.json"
                        with open(tp, "w", encoding="utf-8") as f:
                            json.dump({"words": words, "sentences": sentences}, f, ensure_ascii=False, indent=2)
                        meta["transcript_path"] = str(tp)
                        meta["num_sentences"] = len(sentences)
                        meta["provider"] = provider
                        save_library_item(meta)
                        st.success(f"✅ {meta['title']} ({len(sentences)} sentences)")
                    else:
                        st.error(f"Transcription failed for {url}")
                else:
                    st.error(f"Download failed for {url}")
                progress.progress((i + 1) / len(urls))
            status_box.success(f"Finished processing {len(urls)} URL(s)")

# ---------------------------------------------------------------------------
# Tab: Library + real click-to-seek player
# ---------------------------------------------------------------------------
with tab_library:
    library = load_library()
    if not library:
        st.info("No shiurim yet. Search YUTorah or paste URLs.")
    else:
        labels = [f"{it['title']}  ({it.get('num_sentences', '?')} sent.)" for it in library]
        idx = st.selectbox("Choose a shiur", range(len(library)), format_func=lambda i: labels[i])
        item = library[idx]

        st.markdown(f"### {item['title']}")
        st.caption(item.get("source_url", ""))

        audio_path = item.get("audio_path")
        transcript_path = item.get("transcript_path")

        if audio_path and Path(audio_path).exists() and transcript_path and Path(transcript_path).exists():
            with open(transcript_path, encoding="utf-8") as f:
                data = json.load(f)
            sentences = data.get("sentences", [])

            view = st.radio(
                "View",
                ["📖 Book", "🎧 Interactive player"],
                horizontal=True,
                key="lib_view",
                help="Book: clean paragraphs to read. Interactive player: tap a sentence to jump the audio there.",
            )
            if view == "📖 Book":
                render_book_view(item, audio_path, data)
            else:
                filt = st.text_input("Filter sentences in this shiur", key="lib_filter")
                if filt:
                    sentences = [s for s in sentences if filt.lower() in s["text"].lower()]

                st.markdown("**Export timed captions**")
                col_srt, col_vtt = st.columns(2)
                with col_srt:
                    if st.button("Generate SRT", key=f"srt_btn_{item['id']}"):
                        srt_content = sentences_to_srt(sentences)
                        st.session_state[f"srt_{item['id']}"] = srt_content
                    if f"srt_{item['id']}" in st.session_state:
                        srt_content = st.session_state[f"srt_{item['id']}"]
                        st.download_button(
                            "Download .srt",
                            data=srt_content,
                            file_name=f"{item.get('title','shiur')[:40]}.srt",
                            mime="text/plain",
                            key=f"dl_srt_{item['id']}",
                        )
                        if st.button("Copy SRT to clipboard", key=f"copy_srt_{item['id']}"):
                            st.code(srt_content, language="text")
                            st.success("SRT shown above — select all and copy (Ctrl/Cmd+C)")
                with col_vtt:
                    if st.button("Generate VTT", key=f"vtt_btn_{item['id']}"):
                        vtt_content = sentences_to_vtt(sentences)
                        st.session_state[f"vtt_{item['id']}"] = vtt_content
                    if f"vtt_{item['id']}" in st.session_state:
                        vtt_content = st.session_state[f"vtt_{item['id']}"]
                        st.download_button(
                            "Download .vtt",
                            data=vtt_content,
                            file_name=f"{item.get('title','shiur')[:40]}.vtt",
                            mime="text/vtt",
                            key=f"dl_vtt_{item['id']}",
                        )
                        if st.button("Copy VTT to clipboard", key=f"copy_vtt_{item['id']}"):
                            st.code(vtt_content, language="text")
                            st.success("VTT shown above — select all and copy (Ctrl/Cmd+C)")
                st.caption("SRT works in VLC, YouTube, most players. VTT (WebVTT) is the web standard used by HTML5 video and many podcast apps.")

                st.markdown("**Click any sentence below → audio jumps to that exact moment.**")
                render_interactive_player(audio_path, sentences, height=700)
        else:
            st.warning("Audio or transcript missing for this entry.")

# ---------------------------------------------------------------------------
# Tab: Search across personal transcripts
# ---------------------------------------------------------------------------
with tab_fullsearch:
    q2 = st.text_input("Search inside your transcribed shiurim", placeholder="bitul, Rashi, mitzvah…")
    if q2 and len(q2) >= 2:
        hits = []
        for item in load_library():
            tp = item.get("transcript_path")
            if not tp or not Path(tp).exists():
                continue
            with open(tp, encoding="utf-8") as f:
                data = json.load(f)
            for s in data.get("sentences", []):
                if q2.lower() in s["text"].lower():
                    hits.append({
                        "title": item["title"],
                        "start": s["start"],
                        "text": s["text"],
                    })
        if hits:
            st.success(f"{len(hits)} matches")
            for h in hits[:60]:
                st.markdown(f"**{h['title']}** — `{h['start']:.1f}s`")
                st.markdown(f"> {h['text']}")
                st.divider()
        else:
            st.info("No matches found.")
