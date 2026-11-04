from flask import Flask, render_template_string, request, jsonify
import threading, os, re, random, string, html, json, sys, time, zipfile
from datetime import datetime
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# --- Globals ---
is_running = False
stop_requested = False
pause_event = threading.Event()
pause_event.set()
logs = []

# --- Config ---
BASE_DIR = os.getcwd()
DS_PROFILE_DIR = os.path.join(BASE_DIR, "deepseek_profile")
BOARD_PROFILE_DIR = os.path.join(BASE_DIR, "board_profile")
CACHE_DIR = os.path.join(BASE_DIR, ".cache")
TITLE_HISTORY = os.path.join(BASE_DIR, "title_history.json")
OUTPUT_LINKS = os.path.join(BASE_DIR, "thread_links.txt")

for d in [CACHE_DIR, DS_PROFILE_DIR, BOARD_PROFILE_DIR]:
    os.makedirs(d, exist_ok=True)

# ============================================
# 🎯 TITLE CATEGORIES & LOGIC
# ============================================
TITLE_CATEGORIES = {
    "magazine_style": [
        "#~^√TOP [{NUM}] Best {KW} Services in {COUNTRY}™ Real Tests & Results {YEAR}",
        "★【TOP {NUM}】Best {KW} Providers in {COUNTRY}™ Honest List {YEAR}",
        "»【{NUM} Best {KW} Subscriptions for {COUNTRY} Users™ Verified & Ranked {YEAR}】",
        "[OFFICIAL] TOP [{NUM}] Best {KW} in {COUNTRY}™ Expert Picks {YEAR}",
        "!! {NUM} Best Premium {KW} in {COUNTRY}™ 100% Working {YEAR}",
        "★【{NUM} Best {KW} for Firestick in {COUNTRY}™ Hands-On Test {YEAR}】",
        "»【TOP {NUM}】Cheap {KW} in {COUNTRY}™ Top Picks & Deals {YEAR}",
        "[NEW] {NUM} Best {KW} Services {COUNTRY}™ Full Comparison {YEAR}",
    ]
}

NUMBERS = ["3", "5", "7", "8", "10", "11", "12", "15", "20"]
COUNTRY_LIST = ["USA", "UK", "Canada", "Australia", "Germany", "France", "Netherlands", "Spain"]

DEFAULT_CUSTOM_TITLES = """#~^√TOP [{NUM}] Best {KW} Services in {COUNTRY}™ Real Tests & Results {YEAR}
★【TOP {NUM}】Best {KW} Providers in {COUNTRY}™ Honest List {YEAR}
»【{NUM} Best {KW} Subscriptions for {COUNTRY} Users™ Verified & Ranked {YEAR}】
[OFFICIAL] TOP [{NUM}] Best {KW} in {COUNTRY}™ Expert Picks {YEAR}
!! {NUM} Best Premium {KW} in {COUNTRY}™ 100% Working {YEAR}
★【{NUM} Best {KW} for Firestick in {COUNTRY}™ Hands-On Test {YEAR}】
»【TOP {NUM}】Cheap {KW} in {COUNTRY}™ Top Picks & Deals {YEAR}
[NEW] {NUM} Best {KW} Services {COUNTRY}™ Full Comparison {YEAR}"""

def log_message(msg):
    global logs
    timestamp = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{timestamp}] {msg}")
    if len(logs) > 200: logs.pop(0)
    print(msg)

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

def slugify(text):
    text = re.sub(r'[^\w\s-]', '', text.lower())
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')[:60] or "keyword"

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

    for _ in range(500):
        pattern = random.choice(patterns)
        num = random.choice(NUMBERS)
        c_list = [c.strip() for c in countries_str.split(",") if c.strip()]
        country = random.choice(c_list) if c_list else random.choice(COUNTRY_LIST)

        try:
            title = pattern.format(KW=kw_clean, YEAR=year, NUM=num, COUNTRY=country, SFX="", SFX_S="")
        except Exception:
            continue

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
# 🚀 WORKER THREAD
# ============================================
def automation_worker(login_url, board_url, keywords_text, affiliate_link, mode, custom_titles, countries):
    global is_running, stop_requested
    
    log_message("🚀 Starting Automation...")
    keywords = [k.strip() for k in keywords_text.split('\n') if k.strip()]
    
    # Extract Session
    if os.path.exists("deepseek_session.zip"):
        log_message("📦 Extracting DeepSeek Session...")
        try:
            with zipfile.ZipFile("deepseek_session.zip", 'r') as zip_ref:
                zip_ref.extractall(".")
        except Exception as e:
            log_message(f"⚠️ Session extract error: {e}")

    with sync_playwright() as p:
        log_message("🌐 Launching DeepSeek...")
        ds_context = p.chromium.launch_persistent_context(
            DS_PROFILE_DIR, headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        ds_page = ds_context.pages[0] if ds_context.pages else ds_context.new_page()
        ds_page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded", timeout=90000)
        ds_page.wait_for_timeout(6000)

        log_message("⏳ Waiting for DeepSeek...")
        ready = False
        for _ in range(150):
            if stop_requested: break
            try:
                ta = ds_page.locator("textarea[placeholder*='Message']").first
                if ta.count() > 0 and ta.is_visible():
                    ready = True; break
            except: pass
            try:
                ta = ds_page.locator("textarea").first
                if ta.count() > 0 and ta.is_visible():
                    ready = True; break
            except: pass
            ds_page.wait_for_timeout(2000)

        if not ready:
            log_message("❌ DeepSeek login issue! Upload fresh session.")
            is_running = False
            ds_context.close()
            return
        log_message("✅ DeepSeek ready")

        log_message("🌐 Launching Board...")
        board_context = p.chromium.launch_persistent_context(
            BOARD_PROFILE_DIR, headless=True,
            args=["--disable-blink-features=AutomationControlled"])
        board_page = board_context.pages[0] if board_context.pages else board_context.new_page()
        
        log_message("🔐 Checking board login...")
        board_page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        board_page.wait_for_timeout(3000)
        log_message("✅ Board Ready (Assuming logged in)")

        # Loop
        for i, kw in enumerate(keywords):
            if stop_requested: break
            
            log_message(f"\n📌 [{i+1}/{len(keywords)}] {kw}")
            unique_title = _generate_unique_title(kw, custom_titles, countries)
            
            try:
                content_html = query_deepseek(ds_page, kw, unique_title)
            except Exception as e:
                log_message(f"   ❌ DeepSeek: {e}")
                continue

            if not content_html:
                log_message(f"   ⚠️ Empty content")
                continue

            content_html = sanitize_content(content_html, affiliate_link)
            content_html = inject_ctas(content_html, affiliate_link)

            # Post Logic based on mode
            if mode == "routeyou":
                thread_url = create_routeyou_post(board_page, unique_title, content_html)
            elif mode == "guest":
                thread_url = create_guest_thread(board_page, board_url, unique_title, content_html)
            else:
                thread_url = create_thread(board_page, board_url, unique_title, content_html)

            if thread_url:
                log_message(f"   ✅ Posted: {thread_url}")
                with open(OUTPUT_LINKS, "a", encoding="utf-8") as f:
                    f.write(f"{kw}\n{thread_url}\n\n")
            else:
                log_message(f"   ❌ Post failed")

            if i < len(keywords) - 1 and not stop_requested:
                log_message(f"   ⏱ Delay 10s...")
                time.sleep(10)

        ds_context.close()
        board_context.close()
    
    is_running = False
    log_message("🎉 Automation Finished!")

# ============================================
# 🛠️ HELPER FUNCTIONS (DeepSeek & Posts)
# ============================================
def query_deepseek(page, keyword, unique_title):
    try:
        page.locator("button:has-text('New chat')").first.click(timeout=3000)
        page.wait_for_timeout(1500)
    except: pass

    prompt = f"⚠️ USE THIS EXACT TITLE AS THE FIRST LINE: <h1>{unique_title}</h1>\n\n"
    prompt += f"Write a highly detailed, 100% human-like, and SEO-optimized post about \"{keyword}\".\n"
    prompt += "🔥 CRITICAL RULES:\n1. NO AI FLUFF\n2. REAL EXPERIENCE (EEAT)\n3. LSI KEYWORDS\n"
    prompt += "4. TARGET KEYWORD DENSITY 5-7 times\n"
    prompt += "🔗 LINK PLACEHOLDER: Only use {AFF_LINK} as the link placeholder.\n"
    prompt += "ARTICLE STRUCTURE: Intro, What is IPTV, Why Ditching Cable, How I Tested, Top 5 Providers, Table, FAQ, Final Verdict.\n"
    prompt += "OUTPUT: Only HTML content. Start with <h1>."

    textarea = page.locator("textarea[placeholder*='Message']").first
    textarea.click()
    page.wait_for_timeout(500)
    try: page.keyboard.press("Control+A"); page.keyboard.press("Delete")
    except: pass

    textarea.fill(prompt)
    page.wait_for_timeout(1500)
    page.keyboard.press("Enter")

    log_message("   ⏳ Streaming...")
    last_len = 0; stable = 0; best_content = ""; stream_start = time.time()
    resp_sel = "div.ds-markdown"

    for _ in range(240):
        if stop_requested: return None
        if time.time() - stream_start > 240: break
        try: current = page.locator(resp_sel).last.inner_text()
        except: current = ""
        cur_len = len(current)
        if cur_len > last_len + 30:
            best_content = current
            last_len = cur_len
            stable = 0
        else:
            stable += 1
            if cur_len > 500 and stable >= 2:
                log_message(f"   ✅ Done — {cur_len} chars")
                cleaned = clean_text(best_content or current)
                return ensure_h1(cleaned, unique_title)
        page.wait_for_timeout(1000)

    if best_content and len(best_content) > 500:
        cleaned = clean_text(best_content)
        return ensure_h1(cleaned, unique_title)
    raise Exception("Content timeout")

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
        log_message("   🌍 Opening RouteYou Add News page...")
        page.goto("https://www.routeyou.com/news/add", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        name_box = page.locator('input[name="name"]').first
        if name_box.count() == 0: name_box = page.locator('input[type="text"]').nth(1)
        name_box.wait_for(state='visible', timeout=10000)
        clean_title = re.sub(r'https?://\S+', '', title).strip()
        name_box.fill(clean_title[:100])
        log_message(f"   ✅ Title set")

        page.wait_for_timeout(3000) 
        iframe_locator = page.locator("iframe.cke_wysiwyg_frame").first
        if iframe_locator.count() > 0:
            editor_frame = iframe_locator.content_frame
            body_locator = editor_frame.locator("body")
            body_locator.click(force=True)
            page.wait_for_timeout(500)
            body_locator.fill(content)
            log_message("   ✅ Content Injected!")
        else:
            log_message("   ⚠️ CKEditor iframe not found.")
            return None

        page.wait_for_timeout(1500)
        confirm_btn = page.locator('button:has-text("Confirm"), input[value="Confirm"]').first
        if confirm_btn.count() > 0: confirm_btn.click()
        else: page.evaluate("() => { let btns = document.querySelectorAll('button'); for(let b of btns) { if(b.innerText.includes('Confirm')) { b.click(); return; } } }")
        
        page.wait_for_timeout(5000)
        url = page.url
        if "/news/view/" in url:
            return url
        return url 
    except Exception as e:
        log_message(f"   ❌ RouteYou Error: {e}")
        return None

def create_thread(page, board_url, title, content):
    try:
        if not board_url.endswith("/"): board_url += "/"
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        clicked = False
        for sel in ['a:has-text("Create Thread")', 'a[href*="/thread/new/"]', 'a[href*="action=create"]']:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(); clicked = True; break
            except: continue
        if not clicked: return None

        page.wait_for_timeout(3000)
        try:
            subj = page.locator('input[name="subject"]').first
            subj.wait_for(state='visible', timeout=10000)
            subj.fill(title[:60])
        except: return None

        page.wait_for_timeout(2500)
        result = page.evaluate("""(htmlContent) => {
            const form = document.querySelector('form.js-form-thread-new') || document.querySelector('form[id^="form_"]');
            const formId = form ? form.id : '';
            const ta = document.querySelector('#' + formId + '_message_input') || document.querySelector('textarea[name="message"]');
            if (ta) {
                ta.value = htmlContent;
                ta.dispatchEvent(new Event('input', { bubbles: true }));
            }
            return true;
        }""", content)

        page.wait_for_timeout(1200)
        submit_btn = page.locator('input[name="post"][value="Create Thread"], input[type="submit"][value="Create Thread"]').first
        if submit_btn.count() > 0: submit_btn.click()
        else: return None

        page.wait_for_timeout(4000)
        url = page.url
        if "/thread/" in url and "/create/" not in url: return url
        return None
    except Exception as e:
        log_message(f"   ❌ Error: {e}")
        return None

def create_guest_thread(page, board_url, title, content):
    # Same as above but with guest logic
    try:
        if not board_url.endswith("/"): board_url += "/"
        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        clicked = False
        for sel in ['a:has-text("Create Thread")', 'a[href*="/thread/new/"]']:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(); clicked = True; break
            except: continue
        if not clicked: return None

        page.wait_for_timeout(3000)
        
        guest_name = f"User{random.randint(1000, 9999)}"
        guest_inp = page.locator('input[name="created_by_guest_name"]').first
        if guest_inp.count() > 0: guest_inp.fill(guest_name)

        subj = page.locator('input[name="subject"]').first
        subj.wait_for(state='visible', timeout=10000)
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
        
        # TOS & Captcha
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
        url = page.url
        if "/thread/" in url and "/create/" not in url: return url
        return None
    except Exception as e:
        log_message(f"   ❌ Error: {e}")
        return None

# ============================================
# 🌐 FLASK ROUTES & WEB UI
# ============================================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/upload_session', methods=['POST'])
def upload_session():
    if 'file' not in request.files: return jsonify({"status": "error", "msg": "No file"})
    file = request.files['file']
    if file.filename == '': return jsonify({"status": "error", "msg": "No selected file"})
    file.save("deepseek_session.zip")
    return jsonify({"status": "success", "msg": "Session Uploaded!"})

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
        data.get('custom_titles',''), data.get('countries','')
    ), daemon=True).start()
    return jsonify({"status": "success", "msg": "Started"})

@app.route('/stop', methods=['POST'])
def stop():
    global stop_requested
    stop_requested = True
    return jsonify({"status": "success", "msg": "Stopping..."})

@app.route('/logs', methods=['GET'])
def get_logs():
    return jsonify({"logs": logs, "running": is_running})

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
    </style>
</head>
<body>
    <div class="container">
        <h1>📺 IPTV Cloud Beast</h1>
        
        <div class="section">
            <label>📦 Upload DeepSeek Session (ZIP):</label>
            <input type="file" id="session_file">
            <button onclick="uploadSession()">Upload</button>
        </div>

        <div class="section">
            <label>🌍 Mode:</label>
            <select id="mode">
                <option value="normal">Normal Board</option>
                <option value="guest">Guest Board (Captcha)</option>
                <option value="routeyou">RouteYou</option>
            </select>

            <label>🔐 Login URL (Normal Board):</label>
            <input type="text" id="login_url">
            
            <label>🔗 Board URL (or RouteYou URL):</label>
            <input type="text" id="board_url" value="https://www.routeyou.com/news/add">
            
            <label>★ Affiliate Link:</label>
            <input type="text" id="affiliate_link" value="https://smartiflix.com/?coupon=sunnycoupon">
            
            <label>🌍 Countries:</label>
            <input type="text" id="countries" value="USA, UK, Canada, Germany">
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
        async function uploadSession() {
            const file = document.getElementById('session_file').files[0];
            if (!file) { alert('Select a zip file'); return; }
            const formData = new FormData();
            formData.append('file', file);
            const res = await fetch('/upload_session', { method: 'POST', body: formData });
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
                mode: document.getElementById('mode').value
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