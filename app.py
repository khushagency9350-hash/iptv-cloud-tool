from flask import Flask, render_template_string, request, jsonify
import threading, os, re, random, string, html, json, sys, time
from datetime import datetime
from playwright.sync_api import sync_playwright
import requests

app = Flask(__name__)

# --- Globals ---
is_running = False
stop_requested = False
logs = []
active_browser = None  # To force close

BASE_DIR = os.environ.get("DATA_DIR", os.getcwd())
BOARD_PROFILE_DIR = os.path.join(BASE_DIR, "board_profile")
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
TITLE_HISTORY = os.path.join(BASE_DIR, "title_history.json")
OUTPUT_LINKS = os.path.join(BASE_DIR, "thread_links.txt")
BOARD_COOKIES_FILE = os.path.join(BASE_DIR, "board_cookies.json")

for d in [CACHE_DIR, BOARD_PROFILE_DIR]:
    os.makedirs(d, exist_ok=True)

def log_message(msg):
    global logs
    timestamp = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{timestamp}] {msg}")
    if len(logs) > 200: logs.pop(0)
    print(msg)

# ============================================
# 🎯 TITLES & LOGIC
# ============================================
NUMBERS = ["3", "5", "7", "8", "10", "11", "12", "15", "20"]
COUNTRY_LIST = ["USA", "UK", "Canada", "Australia", "Germany"]
DEFAULT_CUSTOM_TITLES = """#~^√TOP [{NUM}] Best {KW} Services in {COUNTRY}™ Real Tests & Results {YEAR}
★【TOP {NUM}】Best {KW} Providers in {COUNTRY}™ Honest List {YEAR}"""

def slugify(text):
    text = re.sub(r'[^\w\s-]', '', text.lower())
    text = re.sub(r'[\s_]+', '-', text)
    return text.strip('-')[:60] or "keyword"

def _smart_title_case(text):
    ACRONYMS = {"iptv": "IPTV", "4k": "4K", "8k": "8K", "hd": "HD", "usa": "USA", "uk": "UK", "uae": "UAE", "vpn": "VPN", "epg": "EPG", "m3u": "M3U", "tv": "TV"}
    SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "with"}
    words = text.strip().split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in ACRONYMS: out.append(ACRONYMS[lw])
        elif i > 0 and lw in SMALL: out.append(lw)
        else: out.append(w.capitalize())
    return " ".join(out)

def _generate_unique_title(keyword, custom_titles_str, countries_str):
    hist = {}
    if os.path.exists(TITLE_HISTORY):
        try:
            with open(TITLE_HISTORY, "r", encoding="utf-8") as f: hist = json.load(f)
        except: pass
    kw_key = slugify(keyword)
    used_titles = set(hist.get(kw_key, []))
    kw_clean = _smart_title_case(keyword)
    year = datetime.now().strftime("%Y")
    patterns = [l.strip() for l in custom_titles_str.splitlines() if l.strip()]
    if not patterns: patterns = DEFAULT_CUSTOM_TITLES.split("\n")

    for _ in range(100):
        pattern = random.choice(patterns)
        num = random.choice(NUMBERS)
        c_list = [c.strip() for c in countries_str.split(",") if c.strip()]
        country = random.choice(c_list) if c_list else random.choice(COUNTRY_LIST)
        try:
            title = pattern.format(KW=kw_clean, YEAR=year, NUM=num, COUNTRY=country, SFX="", SFX_S="")
        except: continue
        title = re.sub(r'\s+', ' ', title).strip()
        if title not in used_titles:
            if kw_key not in hist: hist[kw_key] = []
            hist[kw_key].append(title)
            hist[kw_key] = hist[kw_key][-100:]
            try:
                with open(TITLE_HISTORY, "w", encoding="utf-8") as f: json.dump(hist, f, indent=2)
            except: pass
            log_message(f"   🏷️ Title: {title}")
            return title
    return f"#~^√TOP [{random.choice(NUMBERS)}] {kw_clean} Services™ {year}"

# ============================================
# 🤖 DEEPSEEK API
# ============================================
def query_deepseek_api(keyword, unique_title, api_key):
    """DeepSeek API se HTML article generate karta hai (no browser)."""
    prompt = f"⚠️ USE THIS EXACT TITLE AS THE FIRST LINE: <h1>{unique_title}</h1>\n\n"
    prompt += f"Write a highly detailed, 100% human-like, and SEO-optimized post about \"{keyword}\".\n"
    prompt += "🔥 CRITICAL RULES:\n1. NO AI FLUFF\n2. REAL EXPERIENCE (EEAT)\n3. LSI KEYWORDS\n"
    prompt += "4. TARGET KEYWORD DENSITY 5-7 times\n"
    prompt += "🔗 LINK PLACEHOLDER: Only use {AFF_LINK}.\n"
    prompt += "ARTICLE: Intro, What is IPTV, Why Ditching Cable, How I Tested, Top 5, Table, FAQ, Verdict.\n"
    prompt += "OUTPUT: Only HTML. Start with <h1>."

    log_message("   🤖 DeepSeek API call...")
    try:
        resp = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": 0.7,
                "max_tokens": 4096
            },
            timeout=180
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        log_message(f"   ✅ API se content mila — {len(content)} chars")
        return ensure_h1(clean_text(content), unique_title)
    except requests.exceptions.Timeout:
        log_message("   ❌ API timeout (180s)")
        return None
    except requests.exceptions.RequestException as e:
        log_message(f"   ❌ API error: {e}")
        return None
    except (KeyError, IndexError) as e:
        log_message(f"   ❌ API response format error: {e}")
        return None

# ============================================
# 🚀 WORKER THREAD
# ============================================
def automation_worker(login_url, board_url, keywords_text, affiliate_link, mode, custom_titles, countries, delay_sec, wait_min, max_rounds, cheetah_key, api_key):
    global is_running, stop_requested, active_browser
    
    log_message("🚀 Starting Automation (API Mode)...")
    keywords = [k.strip() for k in keywords_text.split('\n') if k.strip()]
    log_message(f"📊 Total Keywords: {len(keywords)}")
    
    if not api_key or not api_key.strip():
        log_message("❌ DeepSeek API Key missing! Dashboard me daalo.")
        is_running = False
        return

    # Load Board cookies
    board_cookies_data = None
    if os.path.exists(BOARD_COOKIES_FILE):
        log_message("🍪 Loading Board Cookies...")
        try:
            with open(BOARD_COOKIES_FILE, "r", encoding="utf-8") as f:
                board_cookies_data = json.load(f)
        except Exception as e:
            log_message(f"⚠️ Board cookies error: {e}")

    try:
        with sync_playwright() as p:
            # --- Sirf Board Browser ---
            log_message("🌐 Launching Board...")
            board_context = p.chromium.launch_persistent_context(
                BOARD_PROFILE_DIR, headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"])
            active_browser = board_context

            if board_cookies_data:
                log_message("   🍪 Injecting Board Cookies...")
                try:
                    clean_cookies = []
                    for c in board_cookies_data:
                        cookie = {
                            "name": c["name"],
                            "value": c["value"],
                            "domain": c.get("domain", ""),
                            "path": c.get("path", "/"),
                        }
                        exp = c.get("expirationDate") or c.get("expires")
                        if exp and exp not in (-1, 0):
                            cookie["expires"] = float(exp)
                        else:
                            cookie["expires"] = -1
                        if c.get("httpOnly"):
                            cookie["httpOnly"] = True
                        if c.get("secure"):
                            cookie["secure"] = True
                        ss = (c.get("sameSite") or "").strip().lower()
                        if ss == "strict":
                            cookie["sameSite"] = "Strict"
                        elif ss == "lax":
                            cookie["sameSite"] = "Lax"
                        elif ss == "none":
                            cookie["sameSite"] = "None"
                        clean_cookies.append(cookie)

                    board_context.add_cookies(clean_cookies)
                    log_message(f"   🍪 Injected {len(clean_cookies)} cookies")
                except Exception as e:
                    log_message(f"   ⚠️ Board Cookie error: {e}")

            board_page = board_context.pages[0] if board_context.pages else board_context.new_page()

            log_message("🔐 Checking Board Login...")
            try:
                board_page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
                log_message("✅ Board Page Loaded")
            except Exception as e:
                log_message(f"⚠️ Board Load Warn: {e}")

            board_page.wait_for_timeout(3000)

            round_num = 1
            while not stop_requested:
                if max_rounds > 0 and round_num > max_rounds:
                    log_message(f"🔚 Reached max rounds ({max_rounds})")
                    break

                log_message(f"\n{'█'*40}\n🔄 ROUND {round_num}\n{'█'*40}")

                for i, kw in enumerate(keywords):
                    if stop_requested: break
                    log_message(f"\n📌 [{i+1}/{len(keywords)}] {kw}")
                    unique_title = _generate_unique_title(kw, custom_titles, countries)

                    content_html = query_deepseek_api(kw, unique_title, api_key)

                    if not content_html:
                        log_message(f"   ⚠️ Empty content, skipping")
                        continue

                    content_html = sanitize_content(content_html, affiliate_link)
                    content_html = inject_ctas(content_html, affiliate_link)

                    if mode == "routeyou":
                        thread_url = create_routeyou_post(board_page, unique_title, content_html)
                    elif mode == "guest":
                        thread_url = create_guest_thread(board_page, board_url, unique_title, content_html)
                    else:
                        thread_url = create_thread(board_page, board_url, unique_title, content_html)

                    if thread_url:
                        log_message(f"   ✅ Posted: {thread_url}")
                        with open(OUTPUT_LINKS, "a", encoding="utf-8") as f:
                            f.write(f"R{round_num} | {kw}\n{thread_url}\n\n")
                        if cheetah_key:
                            log_message("   🚀 Indexing...")
                            submit_to_cheetah(board_page, thread_url, cheetah_key)
                    else:
                        log_message(f"   ❌ Post failed")

                    if i < len(keywords) - 1 and not stop_requested:
                        log_message(f"   ⏱ Delay {delay_sec}s...")
                        for _ in range(delay_sec):
                            if stop_requested: break
                            time.sleep(1)

                if stop_requested: break
                if max_rounds > 0 and round_num >= max_rounds: break

                log_message(f"\n⏳ Waiting {wait_min} min before next round...")
                for _ in range(wait_min * 60):
                    if stop_requested: break
                    time.sleep(1)
                round_num += 1

            board_context.close()

    except Exception as e:
        log_message(f"❌ CRITICAL ERROR: {e}")
    finally:
        is_running = False
        active_browser = None
        log_message("🎉 Automation Finished!")

# --- HELPER FUNCTIONS ---
def submit_to_cheetah(page, url, api_key):
    try:
        cheetah_page = page.context.new_page()
        cheetah_page.goto("https://cheetahindexer.com/", wait_until="domcontentloaded", timeout=45000)
        cheetah_page.wait_for_timeout(2000)
        result = cheetah_page.evaluate("""
            async (data) => {
                try {
                    const resp = await fetch('/api/indexing/submit', {
                        method: 'POST',
                        headers: { 'Authorization': 'Bearer ' + data.apiKey, 'Content-Type': 'application/json' },
                        body: JSON.stringify({urls: [data.url]})
                    });
                    return { ok: resp.ok, status: resp.status, data: await resp.json() };
                } catch(e) { return { ok: false, error: e.message }; }
            }
        """, {"apiKey": api_key, "url": url})
        cheetah_page.close()
        if result.get("ok"):
            log_message(f"   🚀 Indexed! Credits: {result['data'].get('remainingCredits', '?')}")
        else:
            log_message(f"   ❌ Index failed: {result.get('status')}")
    except Exception as e:
        log_message(f"   ❌ Index error: {e}")

def clean_text(text):
    t = text.strip()
    t = re.sub(r'^```html\s*', '', t, flags=re.IGNORECASE)
    t = re.sub(r'^```\s*', '', t)
    t = re.sub(r'\s*```\s*$', '', t)
    return t.strip()

def ensure_h1(content, title):
    content = re.sub(r'<h1[^>]*>.*?</h1>\s*', '', content, flags=re.IGNORECASE | re.DOTALL)
    return f"<h1>{html.escape(title)}</h1>\n\n{content}"

def sanitize_content(content, aff):
    if not aff: return content
    content = content.replace("{AFF_LINK}", aff)
    content = content.replace("{WHATSAPP_LINK}", aff)
    content = content.replace("{TELEGRAM_LINK}", aff)
    return content

def inject_ctas(html_content, aff):
    starting_cta = (
        f'<div style="text-align:center; padding: 25px 15px; margin: 25px 0; background: linear-gradient(135deg, #0f2027, #203a43, #2c5364); border-radius: 12px; box-shadow: 0 8px 20px rgba(0,0,0,0.4); border: 2px solid #FFD700;">'
        f'<h2 style="color: #FFD700; margin: 0 0 10px 0; font-size: 22pt; text-shadow: 1px 1px 2px #000; border: none;">👑 UNLOCK PREMIUM IPTV TODAY</h2>'
        f'<h3 style="color: #e0e0e0; margin: 0 0 15px 0; font-weight: normal; border: none;">Instant Activation • 15,000+ Live Channels • 99.9% Uptime</h3>'
        f'<a href="{aff}" style="display: inline-block; background: linear-gradient(to right, #FFD700, #FFA500); color: #000000; font-size: 14pt; font-weight: bold; text-decoration: none; padding: 12px 35px; border-radius: 30px; box-shadow: 0 4px 15px rgba(255,215,0,0.4); text-transform: uppercase; margin-top: 10px;">'
        f'🚀 Claim 80% OFF Now</a>'
        f'<div style="font-size: 9pt; color: #aaa; margin-top: 12px;">⭐ Rated 4.9/5 by 10,000+ Users</div>'
        f'</div>'
    )
    if '</p>' in html_content:
        html_content = html_content.replace('</p>', '</p>\n' + starting_cta, 1)
    else:
        html_content = starting_cta + "\n" + html_content
    final_cta = (
        f'<div style="text-align:center; margin: 30px 0 15px; padding: 30px 20px; background: linear-gradient(135deg, #1a1a2e, #16213e); border: 2px solid #e94560; border-radius: 12px;">'
        f'<h1 style="color: #e94560; margin: 0 0 10px 0; font-size: 22pt; border: none; text-align: center;">🎬 READY TO START YOUR IPTV JOURNEY?</h1>'
        f'<p style="color: #e0e0e0; font-size: 13pt; margin-bottom: 20px;">Instant activation • 24/7 support • Money-back guarantee</p>'
        f'<a href="{aff}" style="display: inline-block; padding: 15px 45px; font-size: 16pt; font-weight: bold; color: #fff; background: linear-gradient(to right, #e94560, #ff6b81); text-decoration: none; border-radius: 30px; box-shadow: 0 4px 15px rgba(233,69,96,0.4);">🚀 GET IPTV NOW</a>'
        f'<p style="color: #aaa; font-size: 10pt; margin-top: 15px;">✅ 30-day money back • ⚡ Instant setup • 🎯 99.9% uptime</p>'
        f'</div>'
    )
    html_content += '\n\n' + final_cta
    return html_content

def create_routeyou_post(page, title, content):
    try:
        log_message("   🌍 Opening RouteYou...")
        page.goto("https://www.routeyou.com/news/add", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        name_box = page.locator('input[name="name"]').first
        if name_box.count() == 0: name_box = page.locator('input[type="text"]').nth(1)
        name_box.wait_for(state='visible', timeout=10000)
        clean_title = re.sub(r'https?://\S+', '', title).strip()
        name_box.fill(clean_title[:100])
        page.wait_for_timeout(3000)
        iframe_locator = page.locator("iframe.cke_wysiwyg_frame").first
        if iframe_locator.count() > 0:
            editor_frame = iframe_locator.content_frame
            body_locator = editor_frame.locator("body")
            body_locator.click(force=True)
            page.wait_for_timeout(500)
            body_locator.fill(content)
            log_message("   ✅ Content Injected!")
        else: return None
        page.wait_for_timeout(1500)
        confirm_btn = page.locator('button:has-text("Confirm"), input[value="Confirm"]').first
        if confirm_btn.count() > 0: confirm_btn.click()
        page.wait_for_timeout(5000)
        return page.url
    except Exception as e:
        log_message(f"   ❌ RouteYou Error: {e}")
        return None

def create_thread(page, board_url, title, content):
    try:
        if not board_url.endswith("/"): board_url += "/"
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        for sel in ['a:has-text("Create Thread")', 'a[href*="/thread/new/"]']:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(); break
            except: continue
        page.wait_for_timeout(3000)
        subj = page.locator('input[name="subject"]').first
        subj.wait_for(state='visible', timeout=10000)
        subj.fill(title[:60])
        page.wait_for_timeout(2500)
        page.evaluate("""(htmlContent) => {
            const ta = document.querySelector('textarea[name="message"]');
            if (ta) { ta.value = htmlContent; ta.dispatchEvent(new Event('input', { bubbles: true })); }
        }""", content)
        page.wait_for_timeout(1200)
        submit_btn = page.locator('input[value="Create Thread"]').first
        if submit_btn.count() > 0: submit_btn.click()
        page.wait_for_timeout(4000)
        return page.url
    except Exception as e:
        log_message(f"   ❌ Error: {e}")
        return None

def create_guest_thread(page, board_url, title, content):
    try:
        if not board_url.endswith("/"): board_url += "/"
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        for sel in ['a:has-text("Create Thread")', 'a[href*="/thread/new/"]']:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(); break
            except: continue
        page.wait_for_timeout(3000)
        guest_inp = page.locator('input[name="created_by_guest_name"]').first
        if guest_inp.count() > 0: guest_inp.fill(f"User{random.randint(1000, 9999)}")
        subj = page.locator('input[name="subject"]').first
        subj.fill(title[:60])
        page.wait_for_timeout(2000)
        page.evaluate("""(htmlContent) => {
            const ta = document.querySelector('textarea[name="message"]');
            if (ta) { ta.value = htmlContent; ta.dispatchEvent(new Event('input', { bubbles: true })); }
        }""", content)
        page.wait_for_timeout(1000)
        submit_btn = page.locator('input[value="Create Thread"]').first
        if submit_btn.count() > 0: submit_btn.click()
        page.wait_for_timeout(3000)
        page.evaluate("""() => {
            let tos = document.querySelector('input[name="tos_checkbox"]');
            if (tos && !tos.checked) tos.click();
            let captchaFrame = document.querySelector('iframe[title*="reCAPTCHA"]');
            if (captchaFrame) {
                try {
                    let innerDoc = captchaFrame.contentWindow.document;
                    let captchaBox = innerDoc.querySelector('.recaptcha-checkbox-border');
                    if (captchaBox) captchaBox.click();
                } catch(e) {}
            }
        }""")
        page.wait_for_timeout(10000)
        popup_submit = page.locator("text='Submit'").last
        if popup_submit.count() > 0: popup_submit.click(force=True)
        page.wait_for_timeout(5000)
        return page.url
    except Exception as e:
        log_message(f"   ❌ Error: {e}")
        return None

# ============================================
# 🌐 FLASK ROUTES & WEB UI
# ============================================
@app.route('/')
def index(): return render_template_string(HTML_TEMPLATE)

@app.route('/upload_board', methods=['POST'])
def upload_board():
    if 'file' not in request.files: return jsonify({"status": "error", "msg": "No file"})
    file = request.files['file']
    if file.filename == '': return jsonify({"status": "error", "msg": "No selected file"})
    file.save(BOARD_COOKIES_FILE)
    return jsonify({"status": "success", "msg": "Board Cookies Uploaded!"})

@app.route('/start', methods=['POST'])
def start():
    global is_running, stop_requested
    if is_running: return jsonify({"status": "error", "msg": "Already running"})
    data = request.json
    is_running = True
    stop_requested = False
    logs.clear()
    threading.Thread(target=automation_worker, args=(
        data.get('login_url',''), data.get('board_url',''), data.get('keywords',''),
        data.get('affiliate_link',''), data.get('mode','normal'),
        data.get('custom_titles',''), data.get('countries',''),
        int(data.get('delay','10')), int(data.get('wait','30')), int(data.get('rounds','0')),
        data.get('cheetah_key',''),
        data.get('deepseek_api_key','')
    ), daemon=True).start()
    return jsonify({"status": "success", "msg": "Started"})

@app.route('/stop', methods=['POST'])
def stop():
    global stop_requested, is_running, active_browser
    stop_requested = True
    log_message("⏹ Stop Requested...")
    if active_browser:
        try:
            active_browser.close()
        except: pass
    is_running = False
    return jsonify({"status": "success", "msg": "Stopping..."})

@app.route('/logs', methods=['GET'])
def get_logs(): return jsonify({"logs": logs, "running": is_running})

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>📺 IPTV Cloud Beast</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: Arial; background: #1a1a2e; color: #fff; padding: 20px; }
        .container { max-width: 800px; margin: auto; }
        h1 { color: #e94560; text-align: center; }
        input, textarea, select { width: 100%; padding: 10px; margin-bottom: 10px; border-radius: 5px; border: none; background: #0d1b2a; color: #fff; box-sizing: border-box; }
        label { font-weight: bold; color: #00d9ff; }
        button { background: #2ecc71; color: #fff; border: none; padding: 12px 24px; font-size: 16px; border-radius: 5px; cursor: pointer; margin: 5px; }
        button.stop { background: #e94560; }
        #logs { background: #0d1b2a; padding: 15px; border-radius: 5px; height: 300px; overflow-y: auto; font-family: Consolas; font-size: 14px; }
        .section { background: #16213e; padding: 15px; border-radius: 8px; margin-bottom: 15px; }
        .row { display: flex; gap: 10px; }
        .row > div { flex: 1; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📺 IPTV Cloud Beast</h1>

        <div class="section">
            <label>🔑 DeepSeek API Key:</label>
            <input type="text" id="deepseek_api_key" placeholder="sk-... (platform.deepseek.com se)">
        </div>

        <div class="section">
            <label>📦 Upload Board Cookies (JSON):</label>
            <input type="file" id="board_session_file" accept=".json">
            <button onclick="uploadBoardSession()">Upload</button>
        </div>

        <div class="section">
            <label>🌍 Mode:</label>
            <select id="mode">
                <option value="normal">Normal Board</option>
                <option value="guest">Guest Board (Captcha)</option>
                <option value="routeyou">RouteYou</option>
            </select>
            <div class="row">
                <div><label>🔐 Login URL:</label><input type="text" id="login_url"></div>
                <div><label>🔗 Board URL:</label><input type="text" id="board_url" value="https://www.routeyou.com/news/add"></div>
            </div>
            <div class="row">
                <div><label>★ Affiliate Link:</label><input type="text" id="affiliate_link" value="https://smartiflix.com/?coupon=sunnycoupon"></div>
                <div><label>🌍 Countries:</label><input type="text" id="countries" value="USA, UK, Canada, Germany"></div>
            </div>
        </div>

        <div class="section">
            <div class="row">
                <div><label>⏱ Delay (s):</label><input type="number" id="delay" value="10"></div>
                <div><label>⏳ Wait (min):</label><input type="number" id="wait" value="30"></div>
                <div><label>🔁 Rounds (0=∞):</label><input type="number" id="rounds" value="0"></div>
            </div>
            <label>🚀 Cheetah API Key (For Indexing):</label>
            <input type="text" id="cheetah_key" placeholder="Optional">
        </div>

        <div class="section">
            <label>🎨 Custom Titles:</label>
            <textarea id="custom_titles" rows="4">#~^√TOP [{NUM}] Best {KW} Services in {COUNTRY}™ Real Tests & Results {YEAR}</textarea>

            <label>🎯 Keywords:</label>
            <textarea id="keywords" rows="4">iptv subscription
best iptv service</textarea>
        </div>

        <div style="text-align: center;">
            <button onclick="startAutomation()">🚀 Start</button>
            <button onclick="stopAutomation()" class="stop">⏹ Stop</button>
        </div>

        <h3>Live Logs</h3>
        <div id="logs">Waiting...</div>
    </div>

    <script>
        async function uploadBoardSession() {
            const file = document.getElementById('board_session_file').files[0];
            if (!file) { alert('Select a json file'); return; }
            const formData = new FormData();
            formData.append('file', file);
            const res = await fetch('/upload_board', { method: 'POST', body: formData });
            alert((await res.json()).msg);
        }

        async function startAutomation() {
            const data = {
                login_url: document.getElementById('login_url').value,
                board_url: document.getElementById('board_url').value,
                affiliate_link: document.getElementById('affiliate_link').value,
                countries: document.getElementById('countries').value,
                custom_titles: document.getElementById('custom_titles').value,
                keywords: document.getElementById('keywords').value,
                mode: document.getElementById('mode').value,
                delay: document.getElementById('delay').value,
                wait: document.getElementById('wait').value,
                rounds: document.getElementById('rounds').value,
                cheetah_key: document.getElementById('cheetah_key').value,
                deepseek_api_key: document.getElementById('deepseek_api_key').value
            };
            const res = await fetch('/start', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
            alert((await res.json()).msg);
        }
        async function stopAutomation() { await fetch('/stop', {method: 'POST'}); }
        async function pollLogs() {
            const res = await fetch('/logs');
            const data = await res.json();
            const logDiv = document.getElementById('logs');
            logDiv.innerHTML = data.logs.join('<br>');
            logDiv.scrollTop = logDiv.scrollHeight;
        }
        setInterval(pollLogs, 2000);
        pollLogs();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
