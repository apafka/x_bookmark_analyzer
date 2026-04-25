X Bookmark Analyzer
Scrapes all your X bookmarks, fetches linked articles, summarizes each one with Claude, then gives you an honest analysis of what you actually care about and what to pursue next.

What it does
Opens your X bookmarks page in a browser window (using your existing login session)
Scrolls through and scrapes every bookmark
Fetches and reads any linked articles
Summarizes each bookmark in 1-2 sentences using Claude
Runs a full analysis: themes, surprises, a clear direction, and tensions
Results are written to ~/bookmark_analysis.md.

Setup
pip install anthropic httpx playwright browser-cookie3
playwright install chromium
Set your Anthropic API key:

export ANTHROPIC_API_KEY=sk-...
Usage
python3 bookmark_analyzer.py
The first run scrapes everything and caches to ~/bookmarks_cache.json. Subsequent runs reuse the cache — delete it to re-scrape.

Optional: Obsidian notes
If you use Obsidian, set OBSIDIAN_VAULT at the top of the script to include your notes in the analysis:

OBSIDIAN_VAULT = Path.home() / "Documents" / "My Vault"
Requirements
Python 3.10+
Chromium installed and logged into X
An Anthropic API key
