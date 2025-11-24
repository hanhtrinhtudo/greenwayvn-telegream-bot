import os
import json
import re
import unicodedata
import requests
from flask import Flask, request, jsonify

# Optional OpenAI rephraser (set OPENAI_API_KEY to enable)
try:
    import openai
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# Config from env
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
LOG_SHEET_WEBHOOK_URL = os.getenv("LOG_SHEET_WEBHOOK_URL", "")  # Apps Script doPost URL
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", None)
if OPENAI_API_KEY and OPENAI_AVAILABLE:
    openai.api_key = OPENAI_API_KEY

# Data files (originals uploaded by anh)
PRODUCTS_FILE = os.path.join(os.path.dirname(__file__), "products.json")
COMBOS_FILE   = os.path.join(os.path.dirname(__file__), "combos.json")

app = Flask(__name__)

# ---------- Helpers ----------
def load_json_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def normalize_text(s):
    if not s:
        return ""
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r'[\u0300-\u036f]', '', s)  # remove diacritics
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

# Build indices on startup
print("Loading data files...")
products_src = load_json_file(PRODUCTS_FILE).get("products", [])  # original structure. :contentReference[oaicite:2]{index=2}
combos_src   = load_json_file(COMBOS_FILE).get("combos", [])      # original combos. :contentReference[oaicite:3]{index=3}

product_by_code = {}
product_alias_index = {}  # alias -> set(codes)
for p in products_src:
    code = str(p.get("code","")).lstrip("#").strip()
    product_by_code[code] = p
    # create aliases: name, aliases list, code
    aliases = []
    name = p.get("name","")
    aliases.append(name)
    if isinstance(p.get("aliases"), list):
        aliases.extend(p.get("aliases"))
    aliases.append(code)
    # normalize and index
    for a in aliases:
        na = normalize_text(a)
        if not na:
            continue
        product_alias_index.setdefault(na, set()).add(code)

# combos: index aliases and health keywords from combo aliases & product codes inside combo
combo_list = combos_src
combo_alias_index = {}  # normalized alias -> list(combo_objects)
for c in combo_list:
    aliases = c.get("aliases") or []
    # include name too
    aliases = aliases + [c.get("name","")]
    for a in aliases:
        na = normalize_text(a)
        if not na:
            continue
        combo_alias_index.setdefault(na, []).append(c)

# also build simple health tag extract from alias tokens
def find_combos_for_issue(text_norm):
    matches = []
    # exact alias match first (contains)
    for alias_norm, combos in combo_alias_index.items():
        if alias_norm in text_norm:
            matches.extend(combos)
    # dedupe by id and preserve order
    seen = set()
    out = []
    for c in matches:
        cid = c.get("id") or c.get("name")
        if cid not in seen:
            seen.add(cid)
            out.append(c)
    return out

def find_products_by_issue(text_norm, limit=5):
    # match product alias tokens contained in text
    matched_codes = []
    for alias_norm, codes in product_alias_index.items():
        if alias_norm in text_norm:
            for code in codes:
                if code not in matched_codes:
                    matched_codes.append(code)
    # Also match by code inside text (user might type 070703)
    for code in list(product_by_code.keys()):
        if code and code in text_norm and code not in matched_codes:
            matched_codes.insert(0, code)
    # Return product objects
    res = [product_by_code[c] for c in matched_codes if c in product_by_code]
    return res[:limit]

def find_product_by_code(code_query):
    codeq = str(code_query).lstrip("#").strip()
    return product_by_code.get(codeq)

def find_products_by_name(text_norm, limit=5):
    # fuzzy simple contains on product name and aliases
    hits = []
    for code, p in product_by_code.items():
        name = normalize_text(p.get("name",""))
        if name and name in text_norm:
            hits.append(p)
            continue
        # check aliases if present
        for al in p.get("aliases") or []:
            if normalize_text(al) in text_norm:
                hits.append(p)
                break
    # fallback: check words token by token
    if not hits:
        tokens = text_norm.split()
        for code, p in product_by_code.items():
            name = normalize_text(p.get("name",""))
            if any(t in name for t in tokens):
                hits.append(p)
    return hits[:limit]

def format_product_reply(p):
    lines = []
    code = p.get("code","")
    name = p.get("name","")
    price = p.get("price_text") or p.get("price") or ""
    url = p.get("product_url") or p.get("link") or ""
    ingredients = p.get("ingredients_text") or p.get("ingredients") or ""
    usage = p.get("usage_text") or p.get("usage") or ""
    benefits = p.get("benefits_text") or p.get("benefits") or ""
    lines.append(f"{name} ({code})")
    if price: lines.append(f"- Giá tham khảo: {price}")
    if ingredients: lines.append(f"- Thành phần: {ingredients}")
    if usage: lines.append(f"- Cách dùng gợi ý: {usage}")
    if benefits: lines.append("- Lợi ích chính:\n" + benefits)
    if url: lines.append(f"🔗 Link sản phẩm: {url}")
    return "\n\n".join(lines)

def format_combo_reply(c):
    lines = []
    name = c.get("name","")
    duration = c.get("duration_text","")
    header = c.get("header_text","")
    lines.append(f"{name}")
    if duration: lines.append(f"- Thời gian dùng: {duration}")
    if header: lines.append(header)
    lines.append("\nSản phẩm trong combo:")
    for pr in c.get("products", []):
        pcode = pr.get("product_code","")
        pname = pr.get("name","")
        pprice = pr.get("price_text","")
        purl = pr.get("product_url","")
        dose = pr.get("dose_text","")
        lines.append(f"\n{pname} ({pcode})")
        if pprice: lines.append(f"  - Giá: {pprice}")
        if dose: lines.append(f"  - Cách dùng: {dose}")
        if purl: lines.append(f"  - Link: {purl}")
    return "\n".join(lines)

def rephrase_with_openai(text, instruction="Viết lại ngắn gọn, lịch sự, rõ ràng, không thêm thông tin mới."):
    if not OPENAI_API_KEY or not OPENAI_AVAILABLE:
        return text
    try:
        prompt = f"{instruction}\n\nNguyên bản:\n{text}"
        resp = openai.Completion.create(
            engine="text-davinci-003",
            prompt=prompt,
            temperature=0.3,
            max_tokens=400
        )
        return resp.choices[0].text.strip()
    except Exception as e:
        print("OpenAI rephrase failed:", e)
        return text

def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=payload)

def log_to_sheet(payload):
    if not LOG_SHEET_WEBHOOK_URL:
        return
    try:
        requests.post(LOG_SHEET_WEBHOOK_URL, json=payload, timeout=3)
    except Exception as e:
        print("Log webhook failed:", e)

# ---------- Telegram handler ----------
MAIN_KEYBOARD = {
  "keyboard": [
    ["🧩 Combo theo vấn đề sức khỏe", "🔎 Tra cứu sản phẩm"],
    ["🛒 Hướng dẫn mua hàng", "📞 Kết nối tuyến trên"],
    ["📣 Kênh & Fanpage"]
  ],
  "resize_keyboard": True,
  "one_time_keyboard": False
}

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    # basic telegram update structure
    message = data.get("message") or data.get("edited_message") or {}
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    user_name = (message.get("from",{}).get("first_name") or "") + " " + (message.get("from",{}).get("last_name") or "")
    text = message.get("text","").strip()
    text_norm = normalize_text(text)

    # log raw question
    log_payload = {
        "chat_id": chat_id,
        "user_name": user_name,
        "text": text,
        "intent": "",
        "matched_combo_id": "",
        "matched_combo_name": "",
        "matched_product_code": "",
        "matched_product_name": ""
    }

    # Predefined commands
    if text in ["/start", "menu", "Menu"]:
        send_telegram_message(chat_id, "Chọn chức năng:", reply_markup=MAIN_KEYBOARD)
        log_payload["intent"] = "menu"
        log_to_sheet(log_payload)
        return jsonify(ok=True)

    # If user presses button "Combo theo vấn đề sức khỏe"
    if text == "🧩 Combo theo vấn đề sức khỏe":
        send_telegram_message(chat_id, "Anh/chị vui lòng nhập **vấn đề sức khỏe** (ví dụ: tiêu hóa, tiểu đường, tim mạch, cao huyết áp, thải độc...).")
        log_payload["intent"] = "ask_combo_flow"
        log_to_sheet(log_payload)
        return jsonify(ok=True)

    # If user asks product lookup short patterns
    # detect code (6 digits) e.g., 070703 or patterns like 070703?
    m_code = re.search(r'(\d{5,6})', text)
    if m_code:
        code_q = m_code.group(1)
        p = find_product_by_code(code_q)
        if p:
            reply = format_product_reply(p)
            reply = rephrase_with_openai(reply)
            send_telegram_message(chat_id, reply)
            log_payload.update({
                "intent":"product_by_code",
                "matched_product_code": p.get("code",""),
                "matched_product_name": p.get("name","")
            })
            log_to_sheet(log_payload)
            return jsonify(ok=True)

    # Try combo match by issue text
    combos_matched = find_combos_for_issue(text_norm)
    if combos_matched:
        # send top 1 combo
        c = combos_matched[0]
        reply = format_combo_reply(c)
        reply = rephrase_with_openai(reply)
        send_telegram_message(chat_id, reply)
        log_payload.update({
            "intent":"combo_by_issue",
            "matched_combo_id": c.get("id",""),
            "matched_combo_name": c.get("name","")
        })
        log_to_sheet(log_payload)
        return jsonify(ok=True)

    # Try product name search
    prods = find_products_by_name(text_norm)
    if prods:
        # send up to 3
        for p in prods[:3]:
            reply = format_product_reply(p)
            reply = rephrase_with_openai(reply)
            send_telegram_message(chat_id, reply)
            # small pause optional (not implemented blocking)
        log_payload.update({
            "intent":"product_by_name",
            "matched_product_code": prods[0].get("code",""),
            "matched_product_name": prods[0].get("name","")
        })
        log_to_sheet(log_payload)
        return jsonify(ok=True)

    # Try product by issue (health keyword)
    prods_by_issue = find_products_by_issue(text_norm)
    if prods_by_issue:
        for p in prods_by_issue[:3]:
            reply = format_product_reply(p)
            reply = rephrase_with_openai(reply)
            send_telegram_message(chat_id, reply)
        log_payload.update({"intent":"products_by_issue","matched_product_code": prods_by_issue[0].get("code","")})
        log_to_sheet(log_payload)
        return jsonify(ok=True)

    # Special flows
    if text.lower() in ["hướng dẫn mua hàng", "cách mua hàng", "mua hàng"]:
        msg = ("Hướng dẫn mua hàng:\n1) Chọn sản phẩm → gửi link cho khách.\n2) Khách chuyển khoản hoặc ship COD (theo chính sách công ty).\n3) Gửi đơn vào hệ thống Sales.\n\nThanh toán: chuyển khoản, momo, or thanh toán khi nhận (nếu hỗ trợ).")
        send_telegram_message(chat_id, msg)
        log_payload["intent"]="buying_info"
        log_to_sheet(log_payload)
        return jsonify(ok=True)

    if text.lower() in ["kết nối tuyến trên", "kết nối", "hotline", "kết nối tuyến trên"]:
        send_telegram_message(chat_id, "Đã gửi yêu cầu. Tuyến trên sẽ gọi lại: Hotline: 0xx-xxx-xxxx")
        log_payload["intent"]="connect_upline"
        log_to_sheet(log_payload)
        return jsonify(ok=True)

    if text.lower() in ["kênh", "fanpage", "kênh & fanpage", "kênh & fanpage"]:
        send_telegram_message(chat_id, "Kênh chính thức & Fanpage:\n- Fanpage: https://facebook.com/yourpage\n- Kênh Youtube: https://youtube.com/yourchannel")
        log_payload["intent"]="channels"
        log_to_sheet(log_payload)
        return jsonify(ok=True)

    # Default fallback
    send_telegram_message(chat_id,
        "Hiện tại em chưa hiểu rõ câu hỏi hoặc chưa có dữ liệu phù hợp. 🙏\n"
        "Anh/chị vui lòng mô tả cụ thể hơn: tên sản phẩm, mã sản phẩm (vd: 070703) hoặc vấn đề sức khỏe (vd: tiêu hóa, tim mạch...).\n\n"
        "Hoặc bấm nút: 🧩 Combo theo vấn đề sức khỏe / 🔎 Tra cứu sản phẩm",
        reply_markup=MAIN_KEYBOARD
    )
    log_payload["intent"]="fallback"
    log_to_sheet(log_payload)
    return jsonify(ok=True)

# Simple health check
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "products": len(product_by_code), "combos": len(combo_list)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

