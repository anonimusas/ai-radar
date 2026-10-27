#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI News Dashboard builder.
Тянет RSS/Atom из N источников, нормализует, дедуплицирует,
вшивает данные прямо в index.html (файл работает офлайн, без сервера и без API-ключей).

Запуск:  python3 build.py
Опционально (сократka-дайджест через бесплатную LLM):
         GROQ_API_KEY=... python3 build.py --llm
"""
import argparse, html, json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

SOURCES = [
    # key        название                  url                                                   цвет      язык  cap
    dict(key="tc",     name="TechCrunch AI",   url="https://techcrunch.com/category/artificial-intelligence/feed/",       color="#0aa06e", lang="en", cap=20),
    dict(key="hf",     name="Hugging Face",    url="https://huggingface.co/blog/feed.xml",                                color="#f5a623", lang="en", cap=15),
    dict(key="gh",     name="GitHub Trending", url="https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml",         color="#8957e5", lang="en", cap=15),
    dict(key="habr",   name="Habr AI",         url="https://habr.com/ru/rss/hubs/artificial_intelligence/",               color="#5aa2e0", lang="ru", cap=20),
    dict(key="openai", name="OpenAI",          url="https://openai.com/news/rss.xml",                                     color="#10a37f", lang="en", cap=15),
    dict(key="gai",    name="Google AI",       url="https://blog.google/technology/ai/rss/",                              color="#4285f4", lang="en", cap=15),
    dict(key="dm",     name="DeepMind",        url="https://deepmind.google/blog/rss.xml",                                color="#00a67e", lang="en", cap=15),
    dict(key="verge",  name="The Verge AI",    url="https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",   color="#ed5c9b", lang="en", cap=15),
    dict(key="mt",     name="MIT Tech Review", url="https://www.technologyreview.com/topic/artificial-intelligence/feed", color="#d93f2a", lang="en", cap=12),
    dict(key="simon",  name="Simon Willison",  url="https://simonwillison.net/atom/everything/",                          color="#9aa0a6", lang="en", cap=15),
    # ── добавлено ──
    dict(key="mistral",name="Mistral AI",      url="https://mistral.ai/rss.xml",                                          color="#ff7000", lang="en", cap=12),
    dict(key="arxiv",  name="arXiv cs.AI",     url="https://export.arxiv.org/rss/cs.AI",                                  color="#b31b1b", lang="en", cap=12, days=7),
    dict(key="llama",  name="r/LocalLLaMA",    url="https://www.reddit.com/r/LocalLLaMA/.rss",                            color="#ff4500", lang="en", cap=15),
    dict(key="hn",     name="Hacker News AI",  url="https://hnrss.org/newest?q=AI+OR+LLM+OR+GPT&count=25",                 color="#ff6600", lang="en", cap=15),
    dict(key="nvidia", name="NVIDIA",          url="https://blogs.nvidia.com/feed/",                                      color="#76b900", lang="en", cap=12),
    dict(key="ars",    name="Ars Technica AI", url="https://arstechnica.com/ai/feed/",                                    color="#ff4e00", lang="en", cap=12),
    dict(key="mtp",    name="MarkTechPost",    url="https://www.marktechpost.com/feed/",                                  color="#00b4d8", lang="en", cap=12),
    dict(key="bair",   name="BAIR Berkeley",   url="https://bair.berkeley.edu/blog/feed.xml",                             color="#003262", lang="en", cap=4, days=400),
    # ── Telegram-каналы (читаются через публичную веб-версию t.me/s/…) ──
    dict(key="tg_provod",  name="Провод",          url="https://t.me/s/provod",        color="#2aabee", lang="ru", cap=15, tg=True, ai_only=True),
    dict(key="tg_maltsev", name="Мальцев просто",  url="https://t.me/s/maltsevprosto", color="#3fb6e8", lang="ru", cap=15, tg=True),
    dict(key="tg_neyro",   name="Нейросети",       url="https://t.me/s/neyroseti_dr",  color="#5ac8fa", lang="ru", cap=15, tg=True),
    dict(key="tg_gptnews", name="GPT News",        url="https://t.me/s/gpt_news",      color="#0088cc", lang="ru", cap=15, tg=True, ai_only=True),
]

UA = "Mozilla/5.0 (compatible; AINewsDashboard/1.0; +https://example.com)"
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def strip_html(s: str, limit: int = 300) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>", " ", s)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = WS_RE.sub(" ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return s


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def find_all(node, name):
    return [c for c in node.iter() if localname(c.tag) == name]


def first_text(node, names):
    for c in node:
        if localname(c.tag) in names:
            # <link href="..."> (atom) или текстовый <link>
            if localname(c.tag) == "link" and c.get("href"):
                rel = c.get("rel", "alternate")
                if rel in ("alternate", None):
                    return c.get("href")
            if (c.text or "").strip():
                return c.text.strip()
    return ""


def parse_date(raw: str):
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw.replace("+00:00", "Z"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            continue
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def fetch(url: str, timeout: int = 25, retries: int = 3) -> bytes:
    import gzip, zlib
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                "Accept-Encoding": "gzip, deflate",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def parse_feed(xml_bytes: bytes, src: dict, per_feed: int = 25):
    key, lang = src["key"], src["lang"]
    # отрезаем BOM, пробелы и мусор перед прологом
    xml_bytes = xml_bytes.lstrip(b"\xef\xbb\xbf \r\n\t")
    m = re.search(rb"<(?:(?:\?xml)|rss|feed|rdf:RDF)", xml_bytes)
    if m and m.start() > 0:
        xml_bytes = xml_bytes[m.start():]
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        root = ET.fromstring(xml_bytes.decode("utf-8", "replace").encode("utf-8"))
    out = []
    # элементы <item> (RSS) или <entry> (Atom)
    nodes = [n for n in root.iter() if localname(n.tag) in ("item", "entry")]
    for n in nodes[:per_feed]:
        title = strip_html(first_text(n, {"title"}), 220)
        link = first_text(n, {"link", "guid", "id"})
        if not link or not title:
            continue
        if not link.startswith("http"):
            link = "https://" + link.lstrip("/")
        # дата
        ts = parse_date(first_text(n, {"pubdate", "published", "updated", "date", "created", "modified"}))
        # описание
        desc = ""
        for child in n:
            ln = localname(child.tag)
            if ln in ("description", "summary", "content", "encoded", "subtitle", "abstract"):
                txt = "".join(child.itertext()) if len(list(child)) else (child.text or "")
                if txt.strip():
                    desc = strip_html(txt, 320)
                    break
        # GitHub Trending: вытащить язык/звёзды из описания не нужно — чистим картинки
        if key in ("hn", "llama"):
            desc = re.sub(r"(?i)\b(article|comments)\s+url:\s*\S+", " ", desc)
            desc = re.sub(r"(?i)\bpoints?:\s*\d+", " ", desc)
            desc = re.sub(r"(?i)#\s*comments?:\s*\d+", " ", desc)
            desc = re.sub(r"(?i)submitted by\s*/u/\S+.*$", " ", desc)
            desc = WS_RE.sub(" ", desc).strip(" .·-")
        if key == "gh":
            desc = re.sub(r"https?://\S+", "", desc)
            desc = strip_html(desc, 220)
            if not ts:
                now = datetime.now(timezone.utc)
                ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        cats = [c.text.strip() for c in n if localname(c.tag) == "category" and c.text]
        out.append({
            "id": link,
            "title": title,
            "link": link,
            "src": key,
            "ts": ts or 0,
            "desc": desc,
            "lang": lang,
            "cats": cats[:3],
        })
    return out


# ---------------------------------------------------------------- Telegram
TG_MSG_RE = re.compile(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|\Z)', re.S)
TG_POST_RE = re.compile(r'data-post="([^"]+)"')
TG_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
TG_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
AI_RE = re.compile(r"(?i)(\bии\b|\bai\b|нейро|нейрон|\bgpt|chatgpt|claude|gemini|openai|anthropic|llm|"
                   r"промпт|prompt|deepseek|grok|copilot|midjourney|codex|\bагент|скилл|искусствен|машинн\w* обуч|qwen|mistral|sora|veo)")


def parse_tg(html_text: str, src: dict, per_feed: int = 15):
    out = []
    for block in TG_MSG_RE.findall(html_text):
        pm, tm = TG_POST_RE.search(block), TG_TEXT_RE.search(block)
        if not pm or not tm:
            continue  # пост без текста (только фото/видео)
        raw = re.sub(r"(?i)<br\s*/?>", "\n", tm.group(1))
        raw = re.sub(r"<[^>]+>", "", raw)
        lines = [WS_RE.sub(" ", l).strip() for l in html.unescape(raw).split("\n")]
        lines = [l for l in lines if l]
        if not lines:
            continue
        lines[0] = re.sub(r"^(?:[^\w«]*)(?:привет|всем привет|доброе утро)[^!.]*[!.]\s*", "", lines[0], flags=re.I) or lines[0]
        if not lines[0]:
            lines = lines[1:] or [""]
        full = " ".join(lines)
        if src.get("ai_only") and not AI_RE.search(full):
            continue
        # заголовок — первая строка (или первое предложение), остальное — описание
        title = lines[0]
        rest = " ".join(lines[1:])
        if len(title) > 160:
            m = re.match(r"(.{30,160}?[.!?…])\s", title + " ")
            cut = m.group(1) if m else title[:160].rsplit(" ", 1)[0] + "…"
            rest = (title[len(cut):].strip() + " " + rest).strip()
            title = cut
        title = title.strip(" —-:")
        dm = TG_TIME_RE.search(block)
        link = "https://t.me/" + pm.group(1)
        out.append({
            "id": link, "title": strip_html(title, 200), "link": link, "src": src["key"],
            "ts": parse_date(dm.group(1)) if dm else 0, "desc": strip_html(rest, 320),
            "lang": "ru", "cats": [],
        })
    out.sort(key=lambda x: x["ts"], reverse=True)  # в веб-версии новые посты внизу
    return out[:per_feed]


def dedupe(items):
    seen, res = {}, []
    for it in items:
        k = it["id"].split("?")[0].rstrip("/").lower()
        if k in seen:
            continue
        seen[k] = 1
        res.append(it)
    return res


def build_items(per_feed=25, max_age_days=14, errors=None):
    items = []
    for src in SOURCES:
        try:
            raw = fetch(src["url"])
            cap = min(per_feed, src.get("cap", per_feed))
            if src.get("tg"):
                got = parse_tg(raw.decode("utf-8", "replace"), src, cap)
            else:
                got = parse_feed(raw, src, cap)
            print(f"  ok  {src['name']:<18} {len(got):>3} items", file=sys.stderr)
            items += got
        except Exception as e:
            print(f"  ERR {src['name']:<18} {type(e).__name__}: {e}", file=sys.stderr)
            if errors is not None:
                errors.append(src["name"])
    items = dedupe(items)
    windows = {x["key"]: x.get("days", max_age_days) for x in SOURCES}
    now = int(time.time())
    # у каждого источника своё окно свежести: у «медленных» блогов оно шире
    dated = [i for i in items if i["ts"] and i["ts"] >= now - windows.get(i["src"], max_age_days) * 86400]
    undated = [i for i in items if not i["ts"]]
    for i in undated:
        i["ts"] = 0
    dated.sort(key=lambda x: x["ts"], reverse=True)
    return dated, undated


# ---------------------------------------------------------------- перевод на русский
TR_CACHE = "translations.json"


def _tr_key(text: str) -> str:
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _google_tr(text: str) -> str:
    """Бесплатный эндпоинт Google Translate (без ключа). Пустая строка = не получилось."""
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ru&dt=t&q="
           + urllib.parse.quote(text))
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            res = "".join(part[0] for part in data[0] if part and part[0])
            return res.strip()
        except Exception:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    return ""


def translate_items(items, out_dir):
    """Переводит заголовки и описания нерусских источников. Готовые переводы
    хранятся в translations.json, поэтому каждый запуск переводит только новое."""
    from concurrent.futures import ThreadPoolExecutor
    path = os.path.join(out_dir, TR_CACHE)
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:  # noqa: BLE001
        cache = {}

    todo = {}
    for it in items:
        if it["lang"] == "ru":
            continue
        fields = ["desc"] if it["src"] == "gh" else ["title", "desc"]  # у GitHub заголовок = имя репо
        for fld in fields:
            txt = it.get(fld) or ""
            if txt and ru_ratio(txt) < 0.3:
                k = _tr_key(txt)
                if k not in cache:
                    todo[k] = txt

    if todo:
        print(f"  перевод: {len(todo)} новых строк", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=6) as ex:
            for k, ru in zip(todo, ex.map(_google_tr, todo.values())):
                if ru and ru_ratio(ru) >= 0.3:
                    cache[k] = ru
        print(f"  перевод: готово {sum(1 for k in todo if k in cache)}/{len(todo)}", file=sys.stderr)

    used = {}
    for it in items:
        for fld in ("title", "desc"):
            txt = it.get(fld) or ""
            k = _tr_key(txt) if txt else None
            if k and k in cache:
                it[fld + "_orig"] = txt
                it[fld] = cache[k]
                used[k] = cache[k]

    # храним только переводы текущих материалов, чтобы файл не рос бесконечно
    with open(path, "w", encoding="utf-8") as f:
        json.dump(used, f, ensure_ascii=False, indent=0)


# ---------------------------------------------------------------- LLM digest
DIGEST_PROMPT = (
    "ОТВЕЧАЙ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. Перевод заголовков на английский недопустим.\n\n"
    "Ты — редактор ежедневного дайджеста новостей об искусственном интеллекте для "
    "русскоязычных читателей. По списку заголовков составь сводку ГЛАВНОГО.\n\n"
    "Правила:\n"
    "1. Ровно 5 пунктов — ни больше, ни меньше. Самое значимое первым.\n"
    "2. Формат пункта: **Короткий заголовок (до 7 слов)** — одно короткое предложение "
    "до 100 символов: что произошло и почему это важно.\n"
    "3. Только текст и ** для жирного. Никаких # и списков с цифрами.\n"
    "4. Никаких вступлений вроде «вот дайджест» и выводов.\n"
    "5. Названия компаний и моделей оставляй как есть.\n"
    "6. Не выдумывай факты, которых нет в заголовках.\n"
    "7. Весь текст — по-русски.\n\n"
    "Заголовки:\n"
)

RU_TAIL = "\n\nНапоминание: ответ строго на русском языке."


def ru_ratio(txt: str) -> float:
    letters = [c for c in txt if c.isalpha()]
    if not letters:
        return 0.0
    cyr = sum(1 for c in letters if "\u0400" <= c <= "\u04FF")
    return cyr / len(letters)


def _digest_prompt(items, n=16):
    listing = "\n".join(f"- {i.get('title_orig', i['title'])}" for i in items[:n])
    return DIGEST_PROMPT + listing


def _groq(items):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return "", ""
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    payload = json.dumps({
        "model": model, "temperature": 0.3, "max_tokens": 900,
        "messages": [
            {"role": "system", "content": "Ты пишешь краткие новостные дайджесты на русском."},
            {"role": "user", "content": _digest_prompt(items) + RU_TAIL},
        ],
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"].strip(), f"Groq · {model}"


def _pollinations(items):
    """Бесплатная LLM без ключа и регистрации (лимит ~1 запрос / 30 сек)."""
    q = urllib.parse.quote(_digest_prompt(items) + RU_TAIL)
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                "https://text.pollinations.ai/" + q,
                headers={"User-Agent": UA, "Accept": "text/plain"})
            with urllib.request.urlopen(req, timeout=120) as r:
                txt = r.read().decode("utf-8", "replace").strip()
            bullets = txt.count("**") // 2
            ok_ru = ru_ratio(txt) >= 0.25
            ok_len = 4 <= bullets <= 7 and txt.strip().endswith((".", "!", "?", "*", "»"))
            if len(txt) > 80 and ok_ru and ok_len:
                return txt, "бесплатная LLM · Pollinations"
            reason = []
            if len(txt) <= 80: reason.append("слишком коротко")
            if not ok_ru: reason.append(f"не русский (ru={ru_ratio(txt):.2f})")
            if not ok_len: reason.append(f"пунктов={bullets}, обрезано")
            print(f"  pollinations: {'; '.join(reason)}, повтор", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"  pollinations: {type(e).__name__}: {e}", file=sys.stderr)
        if attempt < 2:
            time.sleep(33)
    return "", ""


def _fallback(items):
    """LLM недоступна — показываем 5 главных заголовков как есть, чтобы блок не пустовал."""
    lines = []
    for it in items[:5]:
        name = next((x["name"] for x in SOURCES if x["key"] == it["src"]), it["src"])
        lines.append(f"**{name}** — {it['title']}")
    return "\n".join(lines), "без LLM (только заголовки)"


def make_digest(items):
    """Пробуем провайдеров по цепочке: Groq → Pollinations → заголовки без перевода."""
    for fn in (_groq, _pollinations):
        try:
            md, label = fn(items)
            if md and ru_ratio(md) >= 0.25:
                print(f"  digest ok: {label} (ru={ru_ratio(md):.2f})", file=sys.stderr)
                return md, label
            if md:
                print(f"  digest {fn.__name__}: отклонено, не русский текст", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"  digest {fn.__name__}: {type(e).__name__}: {e}", file=sys.stderr)
    print("  digest: fallback", file=sys.stderr)
    return _fallback(items)


def md_to_html(md: str) -> str:
    out = []
    for line in md.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", line)
        out.append(f"<li>{line}</li>")
    return "<ul>" + "".join(out) + "</ul>" if out else ""


# ---------------------------------------------------------------- render
def render(items, digest_md, generated_iso, digest_label=""):
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    src_meta = json.dumps(
        {x["key"]: {"name": x["name"], "color": x["color"], "lang": x["lang"]} for x in SOURCES},
        ensure_ascii=False)
    digest_html = md_to_html(digest_md)
    tpl = TEMPLATE
    tpl = tpl.replace("__DATA__", payload.replace("</", "<\\/"))
    tpl = tpl.replace("__SOURCES__", src_meta)
    tpl = tpl.replace("__DIGEST__", json.dumps(digest_html, ensure_ascii=False))
    tpl = tpl.replace("__DIGESTLABEL__", json.dumps(digest_label or "сводка недоступна", ensure_ascii=False))
    tpl = tpl.replace("__GENERATED__", generated_iso)
    return tpl


TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d12">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>AI Radar — новости ИИ</title>
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="icon-192.png">
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#0b0d12; --bg2:#11141c; --card:#151925; --card2:#1b2030;
  --line:#242a3a; --tx:#e8ecf5; --tx2:#9aa4bb; --tx3:#6b7488;
  --acc:#7c5cff; --acc2:#22d3ee; --new:#34d399; --warn:#fbbf24;
  --r:16px;
}
@media (prefers-color-scheme:light){
  :root{--bg:#f6f7fb;--bg2:#ffffff;--card:#ffffff;--card2:#f0f2f8;--line:#e3e6ef;
        --tx:#111725;--tx2:#5a6379;--tx3:#8b93a7;}
}
html,body{margin:0;padding:0;background:var(--bg);color:var(--tx);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased;overscroll-behavior-y:contain}
body{padding-bottom:calc(82px + env(safe-area-inset-bottom))}
a{color:inherit;text-decoration:none}
.wrap{max-width:760px;margin:0 auto;padding:0 14px}

/* ── header ── */
header{position:sticky;top:0;z-index:30;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:saturate(180%) blur(14px);-webkit-backdrop-filter:saturate(180%) blur(14px);
  border-bottom:1px solid var(--line);padding-top:env(safe-area-inset-top)}
.hd{display:flex;align-items:center;gap:10px;padding:12px 14px 10px;max-width:760px;margin:0 auto}
.logo{width:32px;height:32px;border-radius:10px;flex:none;
  background:linear-gradient(140deg,var(--acc),var(--acc2));display:grid;place-items:center;
  font-size:17px;box-shadow:0 4px 16px rgba(124,92,255,.35)}
h1{font-size:17px;margin:0;font-weight:700;letter-spacing:-.2px;line-height:1.15}
.sub{font-size:11px;color:var(--tx3);margin-top:2px}
.spacer{flex:1}
.iconbtn{width:36px;height:36px;border-radius:11px;border:1px solid var(--line);background:var(--card);
  color:var(--tx2);display:grid;place-items:center;font-size:15px;cursor:pointer;flex:none}
.iconbtn:active{transform:scale(.94)}

/* ── search ── */
.searchbox{padding:0 14px 10px;max-width:760px;margin:0 auto}
.search{display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--line);
  border-radius:13px;padding:0 12px;height:42px}
.search input{flex:1;border:0;background:transparent;color:var(--tx);font-size:15px;outline:none;
  font-family:inherit;min-width:0}
.search input::placeholder{color:var(--tx3)}
.search .clr{color:var(--tx3);cursor:pointer;font-size:16px;display:none}

/* ── chips ── */
.chips{display:flex;gap:7px;overflow-x:auto;padding:0 14px 12px;max-width:760px;margin:0 auto;
  scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;border:1px solid var(--line);background:var(--card);color:var(--tx2);
  border-radius:999px;padding:7px 13px;font-size:13px;font-weight:600;cursor:pointer;
  display:flex;align-items:center;gap:6px;white-space:nowrap;transition:.15s}
.chip .dot{width:7px;height:7px;border-radius:50%;background:var(--tx3)}
.chip.on{background:var(--tx);color:var(--bg);border-color:var(--tx)}
.chip.on .dot{box-shadow:0 0 0 2px color-mix(in srgb,var(--bg) 60%,transparent)}
.chip .n{font-size:11px;opacity:.6;font-weight:700}

/* ── digest ── */
.digest{margin:2px auto 14px;max-width:760px;padding:0 14px;display:none}
.digest.show{display:block}
.digest .inner{background:linear-gradient(150deg,rgba(124,92,255,.16),rgba(34,211,238,.09));
  border:1px solid rgba(124,92,255,.32);border-radius:var(--r);padding:14px 15px}
.digest h2{margin:0 0 9px;font-size:12.5px;letter-spacing:.9px;text-transform:uppercase;
  color:var(--acc2);display:flex;align-items:center;gap:7px}
.digest ul{margin:0;padding-left:17px}
.digest li{font-size:14.5px;line-height:1.5;margin-bottom:7px;color:var(--tx)}
.digest li:last-child{margin-bottom:0}
.digest b{color:#fff}
@media (prefers-color-scheme:light){.digest b{color:#0b0d12}}

/* ── list ── */
.list{max-width:760px;margin:0 auto;padding:0 14px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:var(--r);
  padding:13px 14px;margin-bottom:9px;position:relative;overflow:hidden;
  transition:transform .12s,border-color .15s,background .15s}
.card:active{transform:scale(.985);background:var(--card2)}
.card.read{opacity:.52}
.card .bar{position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--c,#666)}
.card .top{display:flex;align-items:center;gap:7px;margin-bottom:6px;flex-wrap:wrap}
.src{font-size:11px;font-weight:800;letter-spacing:.4px;text-transform:uppercase;color:var(--c,#aaa)}
.time{font-size:11.5px;color:var(--tx3);margin-left:auto;white-space:nowrap}
.new-badge{font-size:9.5px;font-weight:900;letter-spacing:.6px;color:#06251a;background:var(--new);
  border-radius:5px;padding:2px 5px;text-transform:uppercase}
.card h3{margin:0;font-size:15.5px;line-height:1.36;font-weight:650;letter-spacing:-.15px}
.card p{margin:6px 0 0;font-size:13px;line-height:1.45;color:var(--tx2);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .tags{display:flex;gap:5px;margin-top:8px;flex-wrap:wrap}
.tag{font-size:10.5px;color:var(--tx3);border:1px solid var(--line);border-radius:6px;padding:2px 6px}
.empty{text-align:center;color:var(--tx3);padding:60px 20px;font-size:14px;line-height:1.6}

/* ── footer / tabbar ── */
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:40;display:flex;
  background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(16px);
  -webkit-backdrop-filter:blur(16px);border-top:1px solid var(--line);
  padding-bottom:env(safe-area-inset-bottom)}
.tabbar .wrap2{display:flex;width:100%;max-width:760px;margin:0 auto}
.tab{flex:1;background:none;border:0;color:var(--tx3);font-family:inherit;font-size:10.5px;
  font-weight:700;padding:9px 0 10px;display:flex;flex-direction:column;align-items:center;gap:3px;
  cursor:pointer;letter-spacing:.2px}
.tab i{font-size:17px;font-style:normal;line-height:1}
.tab.on{color:var(--acc2)}
.banner{display:none;max-width:760px;margin:0 auto 10px;padding:0 14px}
.banner.show{display:block}
.banner .bi{display:flex;align-items:center;gap:9px;background:linear-gradient(150deg,rgba(251,191,36,.14),rgba(251,191,36,.06));
  border:1px solid rgba(251,191,36,.38);border-radius:13px;padding:10px 12px;font-size:12.5px;
  line-height:1.4;color:var(--tx)}
.banner .bi.blue{background:linear-gradient(150deg,rgba(34,211,238,.14),rgba(34,211,238,.05));
  border-color:rgba(34,211,238,.4)}
.banner b{font-weight:700}
.banner button{margin-left:auto;flex:none;background:var(--tx);color:var(--bg);border:0;
  border-radius:9px;padding:7px 11px;font-size:12px;font-weight:800;font-family:inherit;cursor:pointer}
.toast{position:fixed;left:50%;transform:translateX(-50%) translateY(-20px);bottom:calc(96px + env(safe-area-inset-bottom));
  background:var(--card2);border:1px solid var(--line);color:var(--tx);padding:10px 16px;
  border-radius:12px;font-size:13px;z-index:60;opacity:0;transition:.25s;pointer-events:none;
  box-shadow:0 10px 30px rgba(0,0,0,.35);max-width:88vw;text-align:center}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(min-width:761px){.tabbar{display:none}body{padding-bottom:24px}}
</style>
</head>
<body>

<header>
  <div class="hd">
    <div class="logo">◈</div>
    <div style="min-width:0">
      <h1>AI Radar</h1>
      <div class="sub" id="stat">загрузка…</div>
    </div>
    <div class="spacer"></div>
    <button class="iconbtn" id="btnRefresh" title="Обновить">↻</button>
  </div>
  <div class="searchbox">
    <div class="search">
      <span style="color:var(--tx3);font-size:14px">⌕</span>
      <input id="q" type="search" placeholder="Поиск по заголовкам…" autocomplete="off">
      <span class="clr" id="clr">✕</span>
    </div>
  </div>
  <div class="chips" id="chips"></div>
</header>

<div class="banner" id="banner"><div class="bi" id="bannerInner"></div></div>

<div class="digest" id="digest"><div class="inner">
  <h2>✦ Главное сейчас</h2>
  <div id="digestBody"></div>
  <div id="digestLabel" style="font-size:10.5px;color:var(--tx3);margin-top:9px"></div>
</div></div>

<div class="list" id="list"></div>

<div class="tabbar"><div class="wrap2">
  <button class="tab on" data-tab="all"><i>◈</i>Все</button>
  <button class="tab" data-tab="unread"><i>◉</i>Новые</button>
  <button class="tab" data-tab="ru"><i>🌐</i>RU</button>
  <button class="tab" data-tab="saved"><i>★</i>Избранное</button>
</div></div>

<div class="toast" id="toast"></div>

<script>
const DATA = __DATA__;
const SOURCES = __SOURCES__;
const DIGEST = __DIGEST__;
const DIGESTLABEL = __DIGESTLABEL__;
const GENERATED = "__GENERATED__";

const LS_READ = 'airadar.read.v1', LS_SAVED = 'airadar.saved.v1', LS_SEEN = 'airadar.seen.v1';
const load = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d } catch(e){ return d } };
const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)) } catch(e){} };

let readSet  = new Set(load(LS_READ, []));
let savedSet = new Set(load(LS_SAVED, []));
let seenSet  = new Set(load(LS_SEEN, []));   // id, которые уже показывались → «новое» считается один раз
let filter = 'all', query = '';

DATA.forEach(it => { it._t = it.ts ? it.ts * 1000 : 0; });

/* ── «новое»: id, которых не было в прошлый визит ── */
const firstRun = seenSet.size === 0;
const freshIds = new Set(DATA.filter(i => !seenSet.has(i.id)).map(i => i.id));
DATA.forEach(i => seenSet.add(i.id));
save(LS_SEEN, [...seenSet]);
const isFresh = id => !firstRun && freshIds.has(id) && !readSet.has(id);

/* ── helpers ── */
function ago(ts){
  if(!ts) return 'без даты';
  const s = Math.floor((Date.now() - ts)/1000);
  if(s < 60) return 'только что';
  if(s < 3600) return Math.floor(s/60) + ' мин';
  if(s < 86400) return Math.floor(s/3600) + ' ч';
  if(s < 86400*7) return Math.floor(s/86400) + ' дн';
  const d = new Date(ts);
  return d.toLocaleDateString('ru-RU', {day:'numeric', month:'short'});
}
function esc(s){ return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])) }

let toastT;
function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastT); toastT = setTimeout(()=>t.classList.remove('show'), 1900);
}

/* ── chips ── */
const counts = {};
DATA.forEach(i => counts[i.src] = (counts[i.src]||0) + 1);
function buildChips(){
  const box = document.getElementById('chips');
  const mk = (key, label, color, n) =>
    `<button class="chip${filter===key?' on':''}" data-f="${key}">
       ${color?`<span class="dot" style="background:${color}"></span>`:''}${esc(label)}
       <span class="n">${n}</span></button>`;
  let h = mk('all', 'Всё', '', DATA.length);
  Object.entries(SOURCES).forEach(([k, s]) => { if(counts[k]) h += mk(k, s.name, s.color, counts[k]); });
  box.innerHTML = h;
  box.querySelectorAll('.chip').forEach(c => c.onclick = () => {
    filter = c.dataset.f; buildChips(); render();
  });
}

/* ── render ── */
function visible(){
  const q = query.trim().toLowerCase();
  return ORDERED.filter(it => {
    if(filter === 'saved'){ if(!savedSet.has(it.id)) return false; }
    else if(filter === 'unread'){ if(readSet.has(it.id)) return false; }
    else if(filter === 'ru'){ if(it.lang !== 'ru') return false; }
    else if(filter !== 'all'){ if(it.src !== filter) return false; }
    if(q && ![it.title, it.desc, it.title_orig, it.desc_orig].some(t => (t||'').toLowerCase().includes(q))) return false;
    return true;
  });
}

/* Не даём одному источнику занять весь верх: после сортировки по времени
   разбавляем «лесенку» — не больше 2 подряд из одного источника в первых 40 */
function interleave(arr, windowSize = 6, maxRun = 2){
  const out = [], rest = arr.slice();
  let runSrc = null, runLen = 0;
  while (rest.length) {
    let idx = 0;
    if (runLen >= maxRun) {
      const alt = rest.findIndex((x, i) => x.src !== runSrc && i < windowSize);
      if (alt > -1) idx = alt;
    }
    const [pick] = rest.splice(idx, 1);
    runSrc = pick.src === runSrc ? runSrc : pick.src;
    runLen = pick.src === runSrc ? runLen + 1 : 1;
    if (pick.src !== runSrc) runLen = 1;
    out.push(pick);
  }
  return out;
}
const ORDERED = interleave(DATA.slice().sort((a, b) => b._t - a._t));

function render(){
  const list = document.getElementById('list');
  const items = visible();
  const unread = DATA.filter(i => !readSet.has(i.id)).length;
  document.getElementById('stat').textContent =
    `${DATA.length} материалов · ${Object.keys(SOURCES).filter(k=>counts[k]).length} источников · ${unread} новых`;

  if(!items.length){
    list.innerHTML = `<div class="empty">Ничего не найдено.<br>Попробуй другой фильтр или запрос.</div>`;
    return;
  }
  list.innerHTML = items.map(it => {
    const s = SOURCES[it.src] || {name:it.src, color:'#777'};
    const tags = (it.cats||[]).slice(0,3).map(c=>`<span class="tag">${esc(c)}</span>`).join('');
    return `<a class="card${readSet.has(it.id)?' read':''}" href="${esc(it.link)}" target="_blank"
              rel="noopener" data-id="${esc(it.id)}" style="--c:${s.color}">
      <div class="bar"></div>
      <div class="top">
        <span class="src">${esc(s.name)}</span>
        ${isFresh(it.id)?'<span class="new-badge">new</span>':''}
        ${savedSet.has(it.id)?'<span style="color:var(--warn);font-size:12px">★</span>':''}
        <span class="time">${ago(it._t)}</span>
      </div>
      <h3>${esc(it.title)}</h3>
      ${it.desc?`<p>${esc(it.desc)}</p>`:''}
      ${tags?`<div class="tags">${tags}</div>`:''}
    </a>`;
  }).join('');

  list.querySelectorAll('.card').forEach(card => {
    card.addEventListener('click', () => {
      readSet.add(card.dataset.id);
      save(LS_READ, [...readSet]);
      card.classList.add('read');
      card.querySelector('.new-badge')?.remove();
      setTimeout(render, 350);
    });
    /* долгий тап = в избранное */
    let lt;
    card.addEventListener('touchstart', () => {
      lt = setTimeout(() => {
        const id = card.dataset.id;
        if(savedSet.has(id)) savedSet.delete(id); else savedSet.add(id);
        save(LS_SAVED, [...savedSet]);
        toast(savedSet.has(id) ? '★ Добавлено в избранное' : 'Убрано из избранного');
        navigator.vibrate?.(18);
        render();
      }, 480);
    }, {passive:true});
    ['touchend','touchmove','touchcancel'].forEach(ev =>
      card.addEventListener(ev, () => clearTimeout(lt), {passive:true}));
    card.addEventListener('contextmenu', e => e.preventDefault());
  });
}

/* ── init ── */
document.getElementById('q').addEventListener('input', e => {
  query = e.target.value;
  document.getElementById('clr').style.display = query ? 'block' : 'none';
  render();
});
document.getElementById('clr').onclick = () => {
  document.getElementById('q').value = ''; query = '';
  document.getElementById('clr').style.display='none'; render();
};
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
  t.classList.add('on'); filter = t.dataset.tab;
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));
  buildChips(); render(); window.scrollTo({top:0,behavior:'smooth'});
});
document.getElementById('btnRefresh').onclick = () => {
  if(location.protocol === 'file:'){
    toast('Это офлайн-копия. Обнови её через GitHub Actions или пересобери build.py');
  } else {
    toast('Обновляю…'); setTimeout(()=>location.reload(), 400);
  }
};

if(DIGEST){
  document.getElementById('digestBody').innerHTML = DIGEST;
  document.getElementById('digest').classList.add('show');
  const d = new Date(GENERATED);
  document.getElementById('digestLabel').textContent =
    DIGESTLABEL + ' · ' + d.toLocaleString('ru-RU', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
}

/* ── дата сборки + офлайн-режим ── */
const buildDate = new Date(GENERATED);
const buildStr = buildDate.toLocaleString('ru-RU', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
document.getElementById('stat').textContent += ' · ' + buildStr;

function banner(html, kind){
  const b = document.getElementById('banner'), i = document.getElementById('bannerInner');
  i.className = 'bi' + (kind === 'blue' ? ' blue' : '');
  i.innerHTML = html;
  b.classList.add('show');
}
function offlineBanner(){
  banner(`📴 <span><b>Офлайн-режим.</b> Показаны новости, вшитые в файл при сборке
    (${buildStr}). Новые появятся, когда файл пересоберётся и ты откроешь его с интернетом.</span>`);
}
window.addEventListener('offline', offlineBanner);
if(!navigator.onLine) offlineBanner();

/* ── автообновление: одна перезагрузка на версию, без циклов ─────────────────
   1. сравниваем свою дату сборки с version.json на сервере (cache: no-store)
   2. если на сервере новее и мы за эту версию ещё не перезагружались — reload
   3. если после reload всё равно старая (CDN отдаёт кэш) — показываем кнопку
   Состояние живёт в sessionStorage: закрыл вкладку → счётчик сбросился.        */
if(location.protocol.startsWith('http')){
  const K = 'airadar.autoreload';
  const st = (() => { try { return JSON.parse(sessionStorage.getItem(K)) || {} } catch(e){ return {} } })();
  const stamp = v => new Date(v).toLocaleString('ru-RU',
    {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});

  fetch('version.json?t=' + Date.now(), {cache:'no-store'})
    .then(r => r.ok ? r.json() : null)
    .then(v => {
      if(!v || !v.generated) return;
      const serverDate = new Date(v.generated);
      if(serverDate <= buildDate) {           // мы уже на свежей версии
        try { sessionStorage.removeItem(K) } catch(e){}
        return;
      }
      // на сервере новее
      if(st.for !== v.generated && !st.done){
        try {
          sessionStorage.setItem(K, JSON.stringify({for: v.generated, done: true}));
        } catch(e){}
        document.documentElement.style.opacity = '.6';   // мягкий переход
        location.reload();
        return;
      }
      // перезагрузились, но сервер всё ещё отдаёт кэш → предлагаем кнопку
      banner(`🔄 <span><b>Есть свежая версия</b> (${stamp(v.generated)}).</span>
        <button onclick="location.reload()">Обновить</button>`, 'blue');
    })
    .catch(() => {});
}

/* обновились с открытой вкладкой — раз в сессию подхватываем новую версию */
document.addEventListener('visibilitychange', () => {
  if(document.visibilityState === 'visible' && navigator.onLine &&
     location.protocol.startsWith('http')){
    fetch('version.json?t=' + Date.now(), {cache:'no-store'})
      .then(r => r.ok ? r.json() : null)
      .then(v => {
        if(v?.generated && new Date(v.generated) > buildDate){
          let st = {};
          try { st = JSON.parse(sessionStorage.getItem('airadar.autoreload')) || {} } catch(e){}
          if(st.for !== v.generated){ location.reload(); }
        }
      })
      .catch(() => {});
  }
});
buildChips(); render();

/* PWA: тихая регистрация service worker */
if('serviceWorker' in navigator && location.protocol.startsWith('http')){
  navigator.serviceWorker.register('sw.js').catch(()=>{});
}
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "AI Radar — новости ИИ",
    "short_name": "AI Radar",
    "description": "Актуальные новости и релизы в области ИИ из TechCrunch, Hugging Face, GitHub Trending, Habr и др.",
    "start_url": "./index.html",
    "display": "standalone",
    "background_color": "#0b0d12",
    "theme_color": "#0b0d12",
    "orientation": "portrait",
    "lang": "ru",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
    ],
}

SW = """// Кэш «сначала сеть, при офлайне — кэш»
const C = 'airadar-v1';
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(C).then(c => c.put(e.request, copy)).catch(()=>{});
      return r;
    }).catch(() => caches.match(e.request).then(r => r || caches.match('./index.html')))
  );
});
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--per-feed", type=int, default=25)
    ap.add_argument("--max-age-days", type=int, default=14)
    ap.add_argument("--no-llm", action="store_true", help="без русской сводки (быстрая сборка)")
    ap.add_argument("--llm", action="store_true", help="устаревший флаг, сводка включена по умолчанию")
    ap.add_argument("--no-translate", action="store_true", help="не переводить материалы на русский")
    args = ap.parse_args()

    print("Fetching feeds...", file=sys.stderr)
    dated, undated = build_items(args.per_feed, args.max_age_days)
    items = dated + undated[:15]
    print(f"Итого: {len(items)} материалов (свежих {len(dated)})", file=sys.stderr)
    if not items:
        print("Ничего не собрано — проверь сеть.", file=sys.stderr)
        return 1

    if not args.no_translate:
        print("Перевод на русский...", file=sys.stderr)
        translate_items(items, args.out)

    digest, digest_label = "", ""
    if not args.no_llm:
        print("Сводка (русская)...", file=sys.stderr)
        digest, digest_label = make_digest(dated)

    now = datetime.now(timezone.utc).astimezone()
    generated = now.isoformat(timespec="seconds")
    out = render(items, digest, generated, digest_label)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)
    with open(os.path.join(args.out, "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": generated, "items": len(items),
                   "sources": len({i["src"] for i in items})}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "sw.js"), "w", encoding="utf-8") as f:
        f.write(SW)
    make_icons(args.out)
    print(f"Готово: {os.path.join(args.out,'index.html')}", file=sys.stderr)
    return 0


def make_icons(out):
    """Иконки для «Добавить на главный экран». Чистый stdlib — рисуем PNG вручную не будем,
    вместо этого используем SVG + PNG-заглушки через простой zlib-энкодер."""
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#7c5cff"/><stop offset="1" stop-color="#22d3ee"/></linearGradient></defs>
<rect width="512" height="512" rx="112" fill="#0b0d12"/>
<rect x="26" y="26" width="460" height="460" rx="92" fill="url(#g)"/>
<text x="256" y="330" font-family="Helvetica,Arial" font-size="240" font-weight="bold"
 text-anchor="middle" fill="#0b0d12">◈</text></svg>"""
    with open(os.path.join(out, "icon.svg"), "w", encoding="utf-8") as f:
        f.write(svg)
    try:
        from PIL import Image, ImageDraw
        for size, name in ((192, "icon-192.png"), (512, "icon-512.png")):
            img = Image.new("RGB", (size, size), "#0b0d12")
            d = ImageDraw.Draw(img)
            r = int(size * 0.10)
            d.rounded_rectangle([size*0.05, size*0.05, size*0.95, size*0.95], radius=r,
                                fill="#7c5cff")
            d.rounded_rectangle([size*0.30, size*0.30, size*0.70, size*0.70], radius=int(size*0.08),
                                fill="#0b0d12")
            img.save(os.path.join(out, name))
    except Exception:
        pass  # без Pillow остаётся icon.svg


if __name__ == "__main__":
    sys.exit(main())
