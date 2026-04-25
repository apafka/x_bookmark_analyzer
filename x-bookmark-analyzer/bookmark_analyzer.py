#!/usr/bin/env python3
"""
Scrapes X bookmarks, fetches linked articles, summarizes each with Claude,
then gives an honest analysis of your interests and what to pursue next.

Usage:
    python3 bookmark_analyzer.py

Results saved to ~/bookmark_analysis.md
Bookmarks cached to ~/bookmarks_cache.json (delete to re-scrape)

Dependencies:
    pip install anthropic httpx playwright browser-cookie3
    playwright install chromium
"""
import json
import time
import re
import sys
import shutil
from pathlib import Path
from html.parser import HTMLParser

import httpx
import anthropic

CACHE = Path.home() / "bookmarks_cache.json"
COOKIES = Path.home() / ".bookmark_analyzer_cookies.json"
OUTPUT = Path.home() / "bookmark_analysis.md"
MODEL = "claude-sonnet-4-6"

# Optional: path to your Obsidian vault. Set to None to skip.
OBSIDIAN_VAULT = None  # e.g. Path.home() / "Documents" / "My Vault"

# Chromium executable — auto-detected, or override here
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")

client = anthropic.Anthropic()


# --- Article extraction ---

class TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "aside"}
    INCLUDE = {"p", "h1", "h2", "h3", "li"}

    def __init__(self):
        super().__init__()
        self._depth = 0
        self._in_block = False
        self._buf = []
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._depth += 1
        elif tag in self.INCLUDE and not self._depth:
            self._in_block = True

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._depth = max(0, self._depth - 1)
        elif tag in self.INCLUDE:
            text = "".join(self._buf).strip()
            if text:
                self.chunks.append(text)
            self._buf = []
            self._in_block = False

    def handle_data(self, data):
        if self._in_block and not self._depth:
            self._buf.append(data)


def fetch_article(url: str, max_chars=3000) -> str:
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        })
        p = TextExtractor()
        p.feed(r.text)
        text = " ".join(p.chunks)
        return text[:max_chars]
    except Exception:
        return ""


def resolve_url(url: str) -> str:
    try:
        r = httpx.head(url, timeout=5, follow_redirects=True)
        return str(r.url)
    except Exception:
        return url


# --- Scraping ---

def scrape_bookmarks() -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed. Run:\n  pip install playwright && playwright install chromium")
        sys.exit(1)

    if not CHROMIUM:
        print("Chromium not found. Install it or set the CHROMIUM path manually in the script.")
        sys.exit(1)

    bookmarks = []
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM, headless=False)

        if not COOKIES.exists():
            print("Extracting cookies from your existing Chromium session...")
            try:
                import browser_cookie3
            except ImportError:
                print("browser-cookie3 not installed. Run:\n  pip install browser-cookie3")
                sys.exit(1)
            jar = browser_cookie3.chromium(domain_name='.x.com')
            cookies_list = [
                {"name": c.name, "value": c.value, "domain": c.domain,
                 "path": c.path, "secure": bool(c.secure)}
                for c in jar
            ]
            state = {"cookies": cookies_list, "origins": []}
            COOKIES.write_text(json.dumps(state))
            print(f"Extracted {len(cookies_list)} cookies.")

        context = browser.new_context(storage_state=str(COOKIES))
        page = context.new_page()
        page.goto("https://x.com/i/bookmarks")
        page.wait_for_selector('[data-testid="tweet"]', timeout=15000)

        no_new = 0
        while no_new < 5:
            tweet_els = page.query_selector_all('[data-testid="tweet"]')
            new = 0

            for el in tweet_els:
                try:
                    link = el.query_selector('a[href*="/status/"]')
                    if not link:
                        continue
                    m = re.search(r'/status/(\d+)', link.get_attribute("href") or "")
                    if not m:
                        continue
                    tid = m.group(1)
                    if tid in seen:
                        continue
                    seen.add(tid)
                    new += 1

                    text_el = el.query_selector('[data-testid="tweetText"]')
                    text = text_el.inner_text() if text_el else ""

                    name_el = el.query_selector('[data-testid="User-Name"]')
                    author = (name_el.inner_text().split("\n")[0] if name_el else "").strip()

                    urls = []
                    for a in el.query_selector_all("a[href]"):
                        href = a.get_attribute("href") or ""
                        if href.startswith("http") and "x.com" not in href and "twitter.com" not in href:
                            urls.append(href)
                        elif "t.co/" in href and href.startswith("http"):
                            urls.append(href)

                    bookmarks.append({
                        "id": tid,
                        "author": author,
                        "text": text,
                        "tweet_url": f"https://x.com/i/web/status/{tid}",
                        "external_urls": list(set(urls)),
                        "resolved_urls": [],
                        "article_content": "",
                        "juice": "",
                    })
                except Exception:
                    continue

            if new == 0:
                no_new += 1
            else:
                no_new = 0

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2.5)
            print(f"  {len(bookmarks)} bookmarks collected...", end="\r")

        context.close()
        browser.close()

    print(f"\nScraped {len(bookmarks)} bookmarks total.")
    return bookmarks


# --- Enrichment ---

def enrich(bookmarks: list[dict]) -> list[dict]:
    print("\nResolving URLs and fetching articles...")
    for i, b in enumerate(bookmarks):
        if b.get("resolved_urls") or not b["external_urls"]:
            continue
        print(f"  {i+1}/{len(bookmarks)}: @{b['author'][:25]}", end="\r")
        resolved = []
        for url in b["external_urls"]:
            real = resolve_url(url)
            if not any(x in real for x in ["x.com", "twitter.com", "t.co"]):
                resolved.append(real)
        b["resolved_urls"] = resolved
        if resolved:
            b["article_content"] = fetch_article(resolved[0])
    return bookmarks


# --- Claude calls ---

def get_juice(b: dict) -> str:
    content = b["text"]
    if b.get("article_content"):
        content += f"\n\n[Article]: {b['article_content'][:2000]}"
    msg = client.messages.create(
        model=MODEL,
        max_tokens=120,
        messages=[{
            "role": "user",
            "content": f"In 1-2 sentences, what's the core idea here that would make someone save this?\n\n{content}"
        }]
    )
    return msg.content[0].text.strip()


def summarize_all(bookmarks: list[dict]) -> list[dict]:
    total = len(bookmarks)
    done = sum(1 for b in bookmarks if b.get("juice"))
    print(f"\nSummarizing {total} bookmarks... ({done} already done)")
    for i, b in enumerate(bookmarks):
        if b.get("juice"):
            continue
        print(f"  {i+1}/{total}: @{b['author'][:25]}", end="\r")
        b["juice"] = get_juice(b)
        CACHE.write_text(json.dumps(bookmarks, indent=2))
    return bookmarks


def read_obsidian() -> str:
    if not OBSIDIAN_VAULT or not Path(OBSIDIAN_VAULT).exists():
        return ""
    notes = []
    for f in Path(OBSIDIAN_VAULT).rglob("*.md"):
        try:
            text = f.read_text().strip()
            if len(text) > 80 and "_template" not in f.name:
                notes.append(f"### {f.stem}\n{text[:1500]}")
        except Exception:
            pass
    return "\n\n".join(notes)


def analyze(bookmarks: list[dict], obsidian: str) -> str:
    bookmark_digest = "\n\n".join(
        f"@{b['author']}: {b['juice']}" + (f"\nLink: {b['resolved_urls'][0]}" if b['resolved_urls'] else "")
        for b in bookmarks
    )

    obsidian_section = obsidian if obsidian else "(No Obsidian notes provided.)"

    prompt = f"""You're doing an honest, direct analysis of someone's X bookmarks — things they deliberately saved over time. Your job: tell them what genuinely interests them and what they should pursue next.

Here are all their X bookmarks:

{bookmark_digest}

Their Obsidian notes (if any):
{obsidian_section}

Give them:

1. **Real themes** — 3-5 specific interests you see across these bookmarks. Not "technology" — actual specific things. Be sharp.

2. **Surprises** — anything unexpected or non-obvious about what they're saving.

3. **One clear direction** — based on these interests, what should they build or pursue next? Be direct. One answer. No hedging, no "it depends." If the signal is genuinely ambiguous, say so plainly.

4. **Tensions** — any contradictions in their interests worth being aware of.

Tone: peer-level and honest. Not a cheerleader. Not therapy. You looked at their data and you're telling them what you see."""

    msg = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text.strip()


# --- Main ---

def main():
    if CACHE.exists():
        print(f"Loading cached bookmarks from {CACHE}")
        print("(Delete the cache file to re-scrape from X)")
        bookmarks = json.loads(CACHE.read_text())
    else:
        bookmarks = scrape_bookmarks()
        bookmarks = enrich(bookmarks)
        CACHE.write_text(json.dumps(bookmarks, indent=2))

    bookmarks = summarize_all(bookmarks)
    CACHE.write_text(json.dumps(bookmarks, indent=2))

    obsidian = read_obsidian()
    print("\nRunning interest analysis...")
    analysis = analyze(bookmarks, obsidian)

    out = [
        "# Bookmark Analysis",
        f"_{time.strftime('%Y-%m-%d')} — {len(bookmarks)} bookmarks_\n",
        "## Analysis\n",
        analysis,
        "\n---\n",
        "## All Bookmarks\n",
    ]
    for b in bookmarks:
        out.append(f"**@{b['author']}** — {b['juice']}")
        if b.get("resolved_urls"):
            out.append(f"<{b['resolved_urls'][0]}>")
        out.append(f"> {b['text'][:250]}\n")

    OUTPUT.write_text("\n".join(out))
    print(f"\nWritten to {OUTPUT}\n")
    print("=" * 60)
    print(analysis)


if __name__ == "__main__":
    main()
