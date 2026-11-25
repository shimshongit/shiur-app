# Shiur Transcriber & Interactive Player

Web app for YUTorah + Torah Anytime shiurim. This copy is set up for
[Streamlit Community Cloud](https://streamlit.io/cloud) (free tier).

## Features

1. **Search YUTorah catalog** directly inside the app (live API, no key)
2. **Batch process** multiple URLs at once (YUTorah or Torah Anytime)
3. **Real click-to-seek** interactive transcript – click any sentence and the audio jumps there
4. Transcription via **ElevenLabs Scribe** or **AssemblyAI** (cloud). Local Whisper is optional and not used on the free cloud tier.
5. Full-text search across your transcripts, plus SRT/VTT export

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repository.
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with GitHub, and deploy that repo. Main file: `app.py`.
3. In **App settings → Secrets**, add the keys from `.streamlit/secrets.toml.example`:

   ```toml
   ELEVENLABS_API_KEY = "your-elevenlabs-api-key"
   ASSEMBLYAI_API_KEY = "your-assemblyai-api-key"
   ```

   You only need one of the two to transcribe. You can also paste a key in the app sidebar.

4. Open the app URL in Chrome on your phone.

### Cloud limits (free tier, ~1 GB RAM)

- **Local Whisper is disabled** (torch + faster-whisper will not fit). Use ElevenLabs or AssemblyAI.
- **Library is not durable.** Audio, transcripts, and saved shiurim live on an ephemeral disk and are wiped when the app restarts or sleeps.
- **Long shiurim can hit memory or request timeouts.** The click-to-seek player embeds the audio file; very large files may be slow or fail. Transcription of a long lecture can also exceed the Cloud request timeout.
- Content is for personal study only. Respect the original sites’ terms.

## Local run (slim, same as cloud)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then edit keys
streamlit run app.py
```

Open the URL Streamlit prints (usually http://localhost:8501).

To enable local Whisper on a machine with enough RAM and disk:

```bash
pip install -r requirements-local.txt
```

The Local (faster-whisper) option then appears in the sidebar.

## API keys

| Secret / env var name     | Required?                         | Where to get it                          |
|---------------------------|-----------------------------------|------------------------------------------|
| `ELEVENLABS_API_KEY`      | One of the two keys is required to transcribe | [elevenlabs.io](https://elevenlabs.io) |
| `ASSEMBLYAI_API_KEY`      | One of the two keys is required to transcribe | [assemblyai.com](https://www.assemblyai.com) |

Keys are read in this order: sidebar text input (override) → `st.secrets` → environment variables. Nothing is hardcoded.

YUTorah search and Torah Anytime / YUTorah URL extraction do not need an API key (`torah-dl` is used for downloads).

## Usage

- **Search YUTorah** → find any shiur → one-click “Add + Transcribe”
- **Torah Anytime** → the site has no public search API, so search on [torahanytime.com](https://www.torahanytime.com), copy lecture links or IDs, and paste them here
- **Batch / Paste URLs** → paste many YUTorah or Torah Anytime links
- **My Library & Reader** → 📖 **Book** view (default): the transcript as clean, book-style paragraphs (serif type, adjustable text size, speaker names only when the speaker changes, Hebrew shown right-to-left), with .txt / .md download. Switch to 🎧 **Interactive player** to tap any sentence and jump the audio; generate SRT or VTT
- **Search My Transcripts** → find phrases across everything you’ve processed

## Custom vocabulary (Hebrew / Torah terms)

In the sidebar, edit the vocabulary list (one term per line). These terms are sent to:

- **AssemblyAI** as `keyterms_prompt` or `word_boost` (high)
- **ElevenLabs Scribe** as `keyterms` (up to 200 terms; Scribe also bills a surcharge above 100)

A starter list of common sefarim, mitzvot, Hebrew words, and speaker names is included.

## SRT + VTT export

In **My Library**, each shiur has:

- **Generate SRT** + Download + Copy to clipboard
- **Generate VTT** + Download + Copy to clipboard

**SRT** (SubRip) works in VLC, YouTube, most video players, and many podcast apps.

**VTT** (WebVTT) is the web-standard subtitle format. It is nearly identical to SRT but uses dots instead of commas in timestamps and starts with a `WEBVTT` header. It works natively with HTML5 `<video>` / `<audio>` and many modern podcast players.
