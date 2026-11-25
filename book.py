"""
Book-style formatting for shiur transcripts.

Turns a list of timed words ({"text", "start", "end", optional "speaker", optional "p"})
into flowing, readable paragraphs. Pure Python, no Streamlit import, so it is easy to test.

- If words carry a provider paragraph index ("p", e.g. from AssemblyAI's /paragraphs
  endpoint) those paragraph breaks are used.
- Otherwise paragraphs are built from sentence boundaries, pauses and speaker changes.
"""

import re
import html as html_lib

SENT_END = ".!?…"
FILLERS = {"um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah", "hmm", "mm", "mhm", "eh"}

# Paragraph heuristics
PAUSE_SOFT = 1.2      # pause (s) that ends a paragraph if it already has a few sentences
PAUSE_HARD = 2.5      # pause (s) that always ends a paragraph
MIN_SENTS_SOFT = 2
MAX_SENTS = 6
MAX_WORDS = 140


def _clean_token(text: str) -> str:
    return (text or "").strip()


def _is_filler(text: str) -> bool:
    core = re.sub(r"[^\w']", "", text.lower())
    return core in FILLERS


def _merge_punct(words: list) -> list:
    """Attach punctuation-only tokens (ElevenLabs emits e.g. a lone '.') to the previous word."""
    out = []
    for w in words:
        t = w["text"]
        if any(c.isalnum() for c in t):
            out.append(w)
            continue
        if not out:
            continue
        prev = out[-1]["text"]
        mark = t.strip()[:1]
        if prev[-1:] in SENT_END:
            continue
        if prev[-1:] in ",;:":
            if mark in SENT_END:
                out[-1] = dict(out[-1], text=prev[:-1] + mark)
            continue
        out[-1] = dict(out[-1], text=prev + mark)
    return out


def _drop_fillers(words: list) -> list:
    """Remove standalone 'um'/'uh' tokens, moving any trailing punctuation to the previous word."""
    out = []
    for w in words:
        t = _clean_token(w.get("text", ""))
        if not t:
            continue
        if _is_filler(t):
            trail = re.sub(r"^[\w']+", "", re.sub(r"^\W+", "", t))
            if trail and out and out[-1]["text"][-1:] not in SENT_END + ",;:":
                out[-1] = dict(out[-1], text=out[-1]["text"] + trail[:1])
            continue
        out.append(dict(w, text=t))
    return out


def _join(tokens: list) -> str:
    s = " ".join(tokens)
    s = re.sub(r"\s+([,.;:!?…%)\]])", r"\1", s)
    s = re.sub(r"([(\[])\s+", r"\1", s)
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def _cap_first(s: str) -> str:
    for i, ch in enumerate(s):
        if ch.isalpha():
            return s[:i] + ch.upper() + s[i + 1:] if ch.islower() else s
        if ch.isdigit():
            return s
    return s


def _finish(s: str) -> str:
    s = s.strip().rstrip(",;:—-").strip()
    if s and s[-1] not in SENT_END + "\"'”’)":
        s += "."
    return s


def _sentences(words: list) -> list:
    """Group words into sentences, also breaking on speaker change and very long pauses."""
    sents, cur = [], []

    def flush():
        if cur:
            sents.append({
                "text": _cap_first(_join([x["text"] for x in cur])),
                "start": cur[0]["start"],
                "end": cur[-1]["end"],
                "speaker": cur[0].get("speaker"),
                "p": cur[0].get("p"),
                "n_words": len(cur),
            })
            cur.clear()

    prev = None
    for w in words:
        if prev is not None:
            gap = (w.get("start") or 0) - (prev.get("end") or 0)
            if (w.get("speaker") != prev.get("speaker")
                    or (w.get("p") is not None and w.get("p") != prev.get("p"))
                    or (gap >= PAUSE_HARD and len(cur) >= 3)
                    or len(cur) >= 60):
                if cur:
                    cur[-1] = dict(cur[-1], text=_finish(cur[-1]["text"]))
                flush()
        cur.append(w)
        t = w["text"]
        if t[-1:] in SENT_END or t.endswith(('."', ".”", '?"', "?”", '!"', "!”")):
            flush()
        prev = w
    if cur:
        cur[-1] = dict(cur[-1], text=_finish(cur[-1]["text"]))
    flush()
    return sents


def build_paragraphs(words: list, drop_fillers: bool = True) -> list:
    """Return [{"text", "start", "end", "speaker"}] paragraphs."""
    words = [dict(w, text=_clean_token(w.get("text", ""))) for w in (words or [])]
    words = _merge_punct([w for w in words if w["text"]])
    if drop_fillers:
        words = _drop_fillers(words)
    if not words:
        return []
    use_provider = any(w.get("p") is not None for w in words)
    sents = _sentences(words)

    paras, cur = [], []

    def flush():
        if cur:
            paras.append({
                "text": " ".join(s["text"] for s in cur),
                "start": cur[0]["start"],
                "end": cur[-1]["end"],
                "speaker": cur[0].get("speaker"),
            })
            cur.clear()

    for s in sents:
        if cur:
            last = cur[-1]
            gap = s["start"] - last["end"]
            n_words = sum(x["n_words"] for x in cur)
            if use_provider:
                brk = s.get("p") != last.get("p") or s.get("speaker") != last.get("speaker")
                brk = brk or n_words >= MAX_WORDS * 2
            else:
                brk = (
                    s.get("speaker") != last.get("speaker")
                    or gap >= PAUSE_HARD
                    or (gap >= PAUSE_SOFT and len(cur) >= MIN_SENTS_SOFT)
                    or len(cur) >= MAX_SENTS
                    or n_words >= MAX_WORDS
                )
            if brk:
                flush()
        cur.append(s)
    flush()
    return paras


def mostly_rtl(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    rtl = sum(1 for c in letters if "\u0590" <= c <= "\u06ff")
    return rtl / len(letters) > 0.5


def _speaker_name(sp) -> str:
    if sp is None:
        return ""
    s = str(sp)
    m = re.match(r"speaker_(\d+)$", s)
    if m:
        return f"Speaker {int(m.group(1)) + 1}"
    return f"Speaker {s}"


def _multi_speaker(paras: list) -> bool:
    return len({p.get("speaker") for p in paras if p.get("speaker") is not None}) > 1


def fmt_clock(t: float) -> str:
    t = int(t or 0)
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def paragraphs_to_text(paras: list, title: str = "", markdown: bool = False) -> str:
    multi = _multi_speaker(paras)
    out = []
    if title:
        out.append(f"# {title}" if markdown else title)
        out.append("")
    prev = object()
    for p in paras:
        sp = p.get("speaker")
        if multi and sp != prev:
            label = _speaker_name(sp)
            out.append(f"**{label}:**" if markdown else f"{label}:")
        prev = sp
        out.append(p["text"])
        out.append("")
    return "\n".join(out).strip() + "\n"


def paragraphs_to_html(paras: list, title: str = "", subtitle: str = "",
                       font_px: int = 19, show_times: bool = False) -> str:
    """Self-contained HTML block (for st.markdown(unsafe_allow_html=True))."""
    all_text = " ".join(p["text"] for p in paras)
    rtl = mostly_rtl(all_text)
    direction = "rtl" if rtl else "ltr"
    multi = _multi_speaker(paras)
    parts = []
    prev = object()
    for i, p in enumerate(paras):
        sp = p.get("speaker")
        if multi and sp != prev:
            parts.append(f'<div class="bk-speaker">{html_lib.escape(_speaker_name(sp))}</div>')
        new_speaker = multi and sp != prev
        prev = sp
        ts = f'<span class="bk-ts">{fmt_clock(p["start"])}</span> ' if show_times else ""
        cls = "bk-p bk-first" if (i == 0 or new_speaker) else "bk-p"
        parts.append(f'<p class="{cls}" dir="auto">{ts}{html_lib.escape(p["text"])}</p>')
    body = "\n".join(parts) or '<p class="bk-p">(No transcript text.)</p>'
    head = ""
    if title:
        head += f'<h1 class="bk-title" dir="auto">{html_lib.escape(title)}</h1>'
    if subtitle:
        head += f'<div class="bk-sub" dir="auto">{html_lib.escape(subtitle)}</div>'
    if head:
        head += '<div class="bk-rule">❧</div>'
    lh = 1.75
    return f"""
<style>
.bk-page {{
  background: #fbf7ee; color: #2b2620;
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia,
               "Noto Serif", "Noto Serif Hebrew", "Frank Ruehl CLM", "Times New Roman", serif;
  font-size: {font_px}px; line-height: {lh};
  max-width: 40em; margin: 0 auto; padding: 2.2em 1.6em 2.6em;
  border-radius: 6px; box-shadow: 0 1px 6px rgba(0,0,0,.25);
  hyphens: auto; -webkit-hyphens: auto; overflow-wrap: break-word;
}}
.bk-title {{ font-family: inherit; font-size: 1.45em; line-height: 1.3; text-align: center;
  margin: 0 0 .3em; color: #2b2620; font-weight: 600; }}
.bk-sub {{ text-align: center; font-size: .82em; color: #7a6e5d; font-style: italic; }}
.bk-rule {{ text-align: center; color: #a8977c; margin: .9em 0 1.2em; font-size: 1.1em; }}
.bk-p {{ margin: 0 0 .15em; text-align: justify; text-indent: 1.5em; color: #2b2620; font-size: 1em; line-height: {lh}; }}
.bk-p.bk-first {{ text-indent: 0; }}
.bk-p + .bk-p {{ margin-top: .55em; }}
.bk-speaker {{ font-variant: small-caps; letter-spacing: .04em; color: #7a5c2e;
  margin: 1.2em 0 .25em; font-size: .9em; }}
.bk-ts {{ font-family: system-ui, sans-serif; font-size: .62em; color: #a8977c; vertical-align: .15em; }}
@media (max-width: 640px) {{ .bk-page {{ padding: 1.4em 1.05em 1.8em; border-radius: 0; }} .bk-p {{ text-align: start; }} }}
</style>
<div class="bk-page" dir="{direction}" lang="{'he' if rtl else 'en'}">
{head}
{body}
</div>
"""
