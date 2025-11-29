import os
import json
import re
import unicodedata
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ============== OpenAI (để hiểu intent & “mượt hóa” câu trả lời) ==============
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ============== ENV ==============
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Hotline, link điều hướng, tuyến trên
HOTLINE_TUYEN_TREN = os.getenv("HOTLINE_TUYEN_TREN", "09xx.xxx.xxx")
LINK_KENH_TELEGRAM = os.getenv("LINK_KENH_TELEGRAM", "https://t.me/your_channel")
LINK_FANPAGE = os.getenv("LINK_FANPAGE", "https://facebook.com/your_fanpage")
LINK_WEBSITE = os.getenv("LINK_WEBSITE", "https://your-website.com")

# ID Telegram của tuyến trên (upline), dạng số (string trong .env)
UPLINE_CHAT_ID = os.getenv("UPLINE_CHAT_ID", "")

# Lưu câu hỏi gần nhất của từng chat
LAST_USER_TEXT = {}

# Trạng thái quy trình chuyển tuyến trên cho từng chat
PENDING_UPLINE_STATE = {}  # "", "waiting_content", "waiting_confirm"
PENDING_UPLINE_TEXT = {}   # {"main_question": "..."}

# Webhook Apps Script để log vào Google Sheets
LOG_SHEET_WEBHOOK_URL = os.getenv("LOG_SHEET_WEBHOOK_URL", "")

# ============== KIỂM TRA ENV ==============
if not TELEGRAM_TOKEN:
    raise ValueError("Thiếu TELEGRAM_TOKEN trong .env")

# ============== OpenAI CLIENT ==============
client = None
if OpenAI and OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

# ============== FLASK APP ==============
app = Flask(__name__)

# ============== ĐƯỜNG DẪN JSON ==============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_PATH = os.path.join(BASE_DIR, "products.json")
COMBOS_PATH = os.path.join(BASE_DIR, "combos.json")
FAQ_BUY_PATH = os.path.join(BASE_DIR, "faq_buy.json")
FAQ_PAYMENT_PATH = os.path.join(BASE_DIR, "faq_payment.json")
FAQ_BUSINESS_PATH = os.path.join(BASE_DIR, "faq_business.json")

# 2 file mới:
HEALTH_TAGS_MAP_PATH = os.path.join(BASE_DIR, "health_tags_map.json")
SYNONYMS_PATH = os.path.join(BASE_DIR, "synonyms.json")

# ============== TẢI DỮ LIỆU JSON ==============
def safe_load_json(path, default=None):
    if default is None:
        default = {}
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        print(f"[WARN] Không đọc được JSON {path}: {e}")
        return default


def extract_list(data, key=None):
    """
    combos.json: { "combos": [ ... ] }
    products.json: { "products": [ ... ] }
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and key and isinstance(data.get(key), list):
        return data[key]
    return []


# đọc dữ liệu thật sự dùng
combos_raw = safe_load_json(COMBOS_PATH, default={"combos": []})
products_raw = safe_load_json(PRODUCTS_PATH, default={"products": []})
faq_buy_data = safe_load_json(FAQ_BUY_PATH, default=[])
faq_payment_data = safe_load_json(FAQ_PAYMENT_PATH, default=[])
faq_business_data = safe_load_json(FAQ_BUSINESS_PATH, default=[])

combos_list = extract_list(combos_raw, "combos")
products_list = extract_list(products_raw, "products")

# 2 file mới
health_tags_map_data = safe_load_json(HEALTH_TAGS_MAP_PATH, default={})
synonyms_data = safe_load_json(SYNONYMS_PATH, default={})

# ============== HÀM TIỆN ÍCH CHUNG ==============
def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text


def strip_markdown(text: str) -> str:
    """
    Loại bỏ các ký hiệu markdown đơn giản như **bold**, *italic* trong chuỗi.
    Không động vào thẻ HTML (<b>...</b>) mà Telegram đang dùng.
    """
    if not isinstance(text, str):
        return text
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = text.replace("*", "")
    return text.strip()


def text_contains(text: str, keyword: str) -> bool:
    return normalize_text(keyword) in normalize_text(text)


def send_telegram_message(chat_id, text, reply_to_message_id=None, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            print("[ERROR] Telegram sendMessage:", resp.text)
    except Exception as e:
        print("[ERROR] Gửi tin nhắn Telegram lỗi:", e)

# ============== LOG VÀO GOOGLE SHEET ==============
def log_event(
    log_type,
    chat_id,
    username="",
    role="",
    user_text="",
    bot_reply="",
    intent="",
    health_issue="",
    product_query="",
    ask_upline="",
    extra="",
    raw_payload=None,
):
    """
    Gửi 1 dòng log sang Apps Script (sheet "Welllab Bot Logs").
    Apps Script sẽ tự tạo header, nên mình chỉ cần gửi key-value.
    """
    if not LOG_SHEET_WEBHOOK_URL:
        return
    try:
        payload = {
            "log_type": log_type,
            "source": "telegram",
            "chat_id": str(chat_id),
            "username": username or "",
            "role": role or "",
            "user_text": user_text or "",
            "bot_reply": bot_reply or "",
            "intent": intent or "",
            "health_issue": health_issue or "",
            "product_query": product_query or "",
            "ask_upline": ask_upline or "",
            "extra": extra or "",
        }
        if raw_payload is not None:
            payload["raw_payload"] = raw_payload
        requests.post(LOG_SHEET_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print("[WARN] log_event lỗi:", e)


def fetch_last_upline_question(chat_id: str):
    """
    Hỏi Apps Script xem câu hỏi tuyến trên gần nhất của chat_id là gì.
    (Apps Script xử lý action=getLastUplineQuestion)
    """
    if not LOG_SHEET_WEBHOOK_URL:
        return None
    try:
        url = f"{LOG_SHEET_WEBHOOK_URL}?action=getLastUplineQuestion&chat_id={chat_id}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print("[WARN] fetch_last_upline_question HTTP:", resp.text)
            return None
        data = resp.json()
        if not data.get("ok"):
            return None
        q = (data.get("question") or "").strip()
        return q or None
    except Exception as e:
        print("[WARN] fetch_last_upline_question lỗi:", e)
        return None


def fetch_history(chat_id: str, limit: int = 20):
    """
    (Chuẩn bị cho tương lai) Lấy lịch sử hội thoại gần nhất từ Apps Script.
    """
    if not LOG_SHEET_WEBHOOK_URL:
        return []
    try:
        url = f"{LOG_SHEET_WEBHOOK_URL}?action=getHistory&chat_id={chat_id}&limit={limit}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print("[WARN] fetch_history HTTP:", resp.text)
            return []
        data = resp.json()
        if not data.get("ok"):
            return []
        return data.get("items") or []
    except Exception as e:
        print("[WARN] fetch_history lỗi:", e)
        return []

# ============== ĐỒNG BỘ SYNONYMS & HEALTH TAGS ==============
def apply_synonyms(text: str) -> str:
    """
    Thay thế các cụm từ theo synonyms.json (bao tử -> dạ dày, v.v.)
    Không phá vỡ nội dung, chỉ chuẩn hóa cách gọi.
    """
    if not text or not isinstance(synonyms_data, dict):
        return text
    result = text
    for k, v in synonyms_data.items():
        if not k or not v:
            continue
        try:
            pattern = re.compile(re.escape(k), flags=re.IGNORECASE)
            result = pattern.sub(v, result)
        except re.error:
            continue
    return result


def expand_health_issue(health_issue: str):
    """
    Từ 1 câu/ cụm 'vấn đề sức khoẻ' → trả về list:
    - [câu gốc, câu sau khi áp synonyms, các health_tags trong health_tags_map nếu match]
    """
    res = []
    if not health_issue:
        return res

    base = health_issue.strip()
    if base:
        res.append(base)

    syn = apply_synonyms(base)
    if syn and syn not in res:
        res.append(syn)

    try:
        h_norm = normalize_text(base)
        if isinstance(health_tags_map_data, dict):
            for key, tags in health_tags_map_data.items():
                try:
                    key_norm = normalize_text(str(key))
                except Exception:
                    continue
                if not key_norm:
                    continue
                if key_norm in h_norm or h_norm in key_norm:
                    if isinstance(tags, list):
                        for t in tags:
                            if t and t not in res:
                                res.append(t)
                    else:
                        if tags and tags not in res:
                            res.append(tags)
    except Exception as e:
        print("[WARN] expand_health_issue:", e)

    return res

# ============== TÌM KIẾM SẢN PHẨM & COMBO ==============
def search_combo_by_health_issue(health_issue: str):
    if not health_issue:
        return None

    issues = expand_health_issue(health_issue)
    if not issues:
        issues = [health_issue]

    best_score = 0
    best_combo = None

    for combo in combos_list:
        name = combo.get("name", "")
        aliases = combo.get("aliases", [])
        health_tags = combo.get("health_tags", [])
        fields = [name] + aliases + health_tags

        score = 0
        for issue in issues:
            i_norm = normalize_text(issue)
            for field in fields:
                if text_contains(field, i_norm) or text_contains(i_norm, field):
                    score += 1

        if score > best_score:
            best_score = score
            best_combo = combo

    return best_combo


def search_product_by_health_issue(health_issue: str):
    if not health_issue:
        return []

    issues = expand_health_issue(health_issue)
    if not issues:
        issues = [health_issue]

    results = []
    for p in products_list:
        fields = []
        fields.append(p.get("name", ""))
        fields.extend(p.get("aliases", []))
        fields.extend(p.get("health_tags", []))
        main_tag = p.get("main_health_tag")
        if main_tag:
            fields.append(main_tag)

        match = False
        for issue in issues:
            i_norm = normalize_text(issue)
            for field in fields:
                if text_contains(field, i_norm) or text_contains(i_norm, field):
                    match = True
                    break
            if match:
                break

        if match:
            results.append(p)

    return results[:3]


def search_product_by_name_or_code(query: str):
    if not query:
        return None
    query = apply_synonyms(query)
    q_norm = normalize_text(query)
    best_score = 0
    best_product = None

    for p in products_list:
        code = p.get("code", "")
        name = p.get("name", "")
        aliases = p.get("aliases", [])
        fields = [code, name] + aliases
        score = 0
        for field in fields:
            if not field:
                continue
            if text_contains(field, q_norm) or text_contains(q_norm, field):
                score += 1
        if score > best_score:
            best_score = score
            best_product = p

    return best_product

# ============== OPENAI – PHÂN TÍCH INTENT & NHU CẦU ==============
def classify_intent_with_openai(user_text: str) -> dict:
    base_result = {
        "intent": "SMALL_TALK",
        "health_issue": None,
        "product_query": None,
        "needs": [],
        "ask_upline": False,
        "raw_reasoning": "",
    }

    if not client:
        # Nếu không có OpenAI thì fallback keyword đơn giản
        t_raw = apply_synonyms(user_text or "")
        t = normalize_text(t_raw)

        # Hỏi lịch sử / câu vừa hỏi
        if any(
            kw in t
            for kw in [
                "vua hoi gi",
                "vua hoi em gi",
                "vua hoi em cau gi",
                "vua hoi em cau hoi gi",
                "xem lai lich su",
                "xem lai cuoc tro chuyen",
                "lich su cuoc tro chuyen",
            ]
        ):
            base_result["intent"] = "META_HISTORY"
            return base_result

        # Gặp tuyến trên
        if any(
            k in t
            for k in [
                "ket noi tuyen tren",
                "ket noi voi tuyen tren",
                "gap tuyen tren",
                "muon gap tuyen tren",
                "muon noi voi tuyen tren",
                "chuyen cho tuyen tren",
                "can tuyen tren ho tro",
            ]
        ):
            base_result["intent"] = "BUSINESS_QUESTION"
            base_result["ask_upline"] = True
            return base_result

        if any(k in t for k in ["tieu duong", "dai thao duong"]):
            base_result["intent"] = "HEALTH_COMBO"
            base_result["health_issue"] = "tiểu đường"
        elif any(k in t for k in ["da day", "bao tu", "trao nguoc"]):
            base_result["intent"] = "HEALTH_PRODUCT"
            base_result["health_issue"] = "đau dạ dày / dạ dày"
        elif any(k in t for k in ["mua hang", "dat hang", "mua nhu the nao"]):
            base_result["intent"] = "HOW_TO_BUY"
        elif any(k in t for k in ["thanh toan", "chuyen khoan"]):
            base_result["intent"] = "HOW_TO_PAY"
        elif any(k in t for k in ["fanpage", "kenh", "website", "trang web"]):
            base_result["intent"] = "NAVIGATION"
        elif any(k in t for k in ["chinh sach", "hoa hong", "kinh doanh", "thuong", "chiet khau"]):
            base_result["intent"] = "BUSINESS_QUESTION"
        return base_result

    system_prompt = """
Bạn là trợ lý AI nội bộ hỗ trợ đội ngũ tư vấn viên (TVV) của công ty thực phẩm chăm sóc sức khỏe.
Nhiệm vụ: phân tích câu hỏi và trả về JSON theo cấu trúc.

Các INTENT chính:
- HEALTH_COMBO: TVV hỏi combo cho một vấn đề sức khỏe (ví dụ: tiểu đường, huyết áp, mỡ máu...)
- HEALTH_PRODUCT: TVV hỏi sản phẩm lẻ cho một vấn đề sức khỏe.
- PRODUCT_DETAIL: TVV hỏi thông tin chi tiết về một sản phẩm cụ thể (theo mã hoặc tên).
- HOW_TO_BUY: Hỏi cách mua hàng, đặt hàng, quy trình.
- HOW_TO_PAY: Hỏi về cách thanh toán, chuyển khoản, COD.
- BUSINESS_QUESTION: Hỏi về chính sách kinh doanh, hoa hồng, chiết khấu, thưởng, quy định nội bộ.
- NAVIGATION: Hỏi xin link fanpage, kênh telegram, website, group chính thức.
- SMALL_TALK: Chào hỏi, cảm ơn, câu chuyện chung chung.
- META_HISTORY: TVV hỏi về chính cuộc trò chuyện, ví dụ:
  "anh vừa hỏi gì nhỉ?", "xem lại lịch sử cuộc trò chuyện này", "lần trước em nói gì với anh?"

- Nếu TVV nói các câu như: "kết nối tuyến trên", "anh muốn gặp tuyến trên", 
  "nhờ tuyến trên trả lời giúp", "chuyển câu này cho tuyến trên", 
  thì:
  + intent = "BUSINESS_QUESTION"
  + ask_upline = true
  + health_issue có thể để null

Trường "needs" là danh sách các nhu cầu cụ thể trong cùng 1 câu:
- "combo": cần tên combo
- "products": cần danh sách sản phẩm trong combo
- "usage": cần cách dùng/cách uống
- "duration": cần thời gian dùng bao lâu để có kết quả
- "product_links": cần link sản phẩm
- "benefits": cần lợi ích/công dụng
- "ingredients": cần thành phần sản phẩm
- "how_to_buy": cần hướng dẫn mua hàng
- "how_to_pay": cần hướng dẫn thanh toán

Trường "ask_upline":
- true: nếu câu hỏi thuộc dạng BUSINESS_QUESTION khó hoặc nhạy cảm, nên chuyển tuyến trên.
- false: còn lại.

Trả về JSON với các field:
{
  "intent": "...",
  "health_issue": "... hoặc null",
  "product_query": "... hoặc null",
  "needs": [...],
  "ask_upline": false,
  "raw_reasoning": "giải thích ngắn gọn vì sao phân loại như vậy"
}

Luôn trả về đúng dạng JSON hợp lệ.
"""

    # Áp synonyms vào text trước khi gửi lên OpenAI cho dễ hiểu
    processed_text = apply_synonyms(user_text or "")

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": processed_text},
            ],
        )
        content = resp.choices[0].message.content
        data = json.loads(content)
        
        for k, v in base_result.items():
            if k not in data:
                data[k] = v

        # Chuẩn hóa health_issue bằng synonyms luôn
        if data.get("health_issue"):
            data["health_issue"] = apply_synonyms(data["health_issue"])

        return data
    except Exception as e:
        print("[ERROR] OpenAI classify_intent:", e)
        return base_result

# ============== BUILD CÂU TRẢ LỜI ==============
def format_combo_reply(combo, needs, health_issue):
    if not combo:
        return (
            f"Hiện tại em chưa tìm thấy combo phù hợp cho vấn đề: <b>{health_issue}</b>.\n"
            "Anh/chị mô tả rõ hơn tình trạng sức khoẻ để em hỗ trợ chính xác hơn nhé."
        )

    raw_name = combo.get("name", "Combo phù hợp")
    raw_header_text = combo.get("header_text", "")
    duration_text = combo.get("duration_text", "")
    combo_url = combo.get("combo_url", "")
    products = combo.get("products", [])

    name = strip_markdown(raw_name)
    header_text = strip_markdown(raw_header_text)
    duration_text = strip_markdown(duration_text)

    lines = []
    lines.append(f"🎯 <b>{name}</b>")
    if header_text:
        lines.append(f"📌 {header_text}")

    if products:
        lines.append("\n🧩 <b>Các sản phẩm trong combo:</b>")
        for idx, p in enumerate(products, start=1):
            pname_combo = p.get("name") or p.get("product_name") or p.get("product_code") or "Sản phẩm"
            pname_combo = strip_markdown(pname_combo)

            # Tìm sản phẩm chi tiết
            product_detail = None
            for prod in products_list:
                if normalize_text(prod.get("name", "")) == normalize_text(pname_combo):
                    product_detail = prod
                    break
                if p.get("code") and normalize_text(prod.get("code", "")) == normalize_text(p.get("code", "")):
                    product_detail = prod
                    break

            price_text = strip_markdown(product_detail.get("price_text", "")) if product_detail else ""
            usage = strip_markdown(product_detail.get("usage_text", "")) if product_detail else ""
            product_url = (product_detail.get("product_url", "") or "").strip() if product_detail else ""
            role_text = strip_markdown(p.get("role_text", "")) if p.get("role_text") else ""
            dose_text = strip_markdown(p.get("dose_text", "")) if p.get("dose_text") else ""

            block_lines = []
            block_lines.append(f"\n<b>{idx}. {pname_combo}</b>")
            if role_text:
                block_lines.append(f"▪️ Công dụng chính: {role_text}")
            if price_text:
                block_lines.append(f"💵 Giá tham khảo: {price_text}")
            if dose_text:
                block_lines.append(f"💊 Cách dùng (trong combo): {dose_text}")
            elif usage:
                block_lines.append(f"💊 Cách dùng gợi ý: {usage}")
            if product_url:
                block_lines.append(f"🔗 Link sản phẩm: {product_url}")
            else:
                block_lines.append(
                    "⚠ Sản phẩm này hiện <b>không có link trên hệ thống</b>, "
                    "có thể đang tạm hết hàng hoặc chưa mở bán online. "
                    "Anh/chị TVV kiểm tra lại kho/trang web trước khi tư vấn giúp em nhé."
                )

            lines.append("\n".join(block_lines))

    if duration_text:
        lines.append(f"\n⏱ <b>Thời gian khuyến nghị:</b> {duration_text}")
    if combo_url:
        lines.append(f"\n🛒 <b>Link combo:</b> {combo_url}")

    lines.append(
        "\n⚠️ <i>Lưu ý: Đây là sản phẩm hỗ trợ, không thay thế thuốc điều trị. "
        "TVV nên hỏi kỹ tình trạng bệnh và thuốc khách đang dùng trước khi tư vấn, "
        "đặc biệt với bệnh nền nặng hoặc đang điều trị chuyên khoa.</i>"
    )
    return "\n".join(lines)

def format_product_reply(product, needs, health_issue=None):
    if not product:
        if health_issue:
            return (
                f"Em chưa tìm thấy sản phẩm phù hợp trong dữ liệu cho vấn đề: <b>{health_issue}</b>.\n"
                "Anh/chị thử mô tả rõ hơn triệu chứng hoặc xin combo tổng thể để tư vấn dễ hơn nhé."
            )
        return "Em chưa tìm thấy sản phẩm phù hợp trong dữ liệu. Anh/chị kiểm tra lại tên hoặc mã sản phẩm giúp em nhé."

    name = strip_markdown(product.get("name", "Sản phẩm"))
    code = strip_markdown(product.get("code", ""))
    ingredients = strip_markdown(product.get("ingredients_text", ""))
    benefits = strip_markdown(product.get("benefits_text", ""))
    usage = strip_markdown(product.get("usage_text", ""))
    price_text = strip_markdown(product.get("price_text", ""))
    duration_text = strip_markdown(product.get("duration_text", ""))
    product_url = (product.get("product_url", "") or "").strip()
    warnings = strip_markdown(product.get("notes_for_tvv", ""))

    lines = []
    title = f"<b>{name}</b>"
    if code:
        title += f" (Mã: {code})"
    lines.append(title)

    if price_text:
        lines.append(f"💰 Giá tham khảo: {price_text}")

    if (not needs) or ("ingredients" in needs):
        if ingredients:
            lines.append("")
            lines.append(f"<b>Thành phần chính:</b> {ingredients}")

    if (not needs) or ("benefits" in needs):
        if benefits:
            lines.append("")
            lines.append("<b>Lợi ích nổi bật:</b>")
            lines.append(benefits)

    if (not needs) or ("usage" in needs):
        if usage:
            lines.append("")
            lines.append(f"<b>Cách dùng khuyến nghị:</b> {usage}")

    if (not needs) or ("duration" in needs):
        if duration_text:
            lines.append("")
            lines.append(f"<b>Thời gian sử dụng nên duy trì:</b> {duration_text}")

    if (not needs) or ("product_links" in needs):
        if product_url:
            lines.append("")
            lines.append(f"🔗 <b>Link sản phẩm:</b> {product_url}")

    if warnings:
        lines.append("")
        lines.append(f"⚠ <b>Lưu ý cho TVV:</b> {warnings}")

    lines.append("")
    lines.append(
        "Anh/chị TVV lưu ý tư vấn rõ đây là sản phẩm hỗ trợ, không thay thế thuốc điều trị, "
        "khuyến khích khách tham khảo ý kiến bác sĩ nếu đang dùng thuốc hoặc có bệnh nền nặng."
    )
    return "\n".join(lines)

def format_faq_reply(faq_list, key_field="title"):
    if not faq_list:
        return "Hiện tại em chưa có dữ liệu hướng dẫn chi tiết trong hệ thống. Anh/chị giúp em liên hệ tuyến trên để được hỗ trợ nhé."

    if all(isinstance(x, str) for x in faq_list):
        return "\n".join(faq_list)

    lines = []
    for i, item in enumerate(faq_list, start=1):
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, dict):
            title = strip_markdown(item.get(key_field, f"Bước {i}"))
            content = strip_markdown(item.get("content", ""))
            line = f"{i}. <b>{title}</b>"
            if content:
                line += f"\n   {content}"
            lines.append(line)
    return "\n\n".join(lines)

def format_navigation_reply():
    lines = []
    lines.append("<b>Các kênh chính thức của công ty:</b>")
    if LINK_KENH_TELEGRAM:
        lines.append(f"📢 Kênh Telegram: {LINK_KENH_TELEGRAM}")
    if LINK_FANPAGE:
        lines.append(f"👍 Fanpage Facebook: {LINK_FANPAGE}")
    if LINK_WEBSITE:
        lines.append(f"🌐 Website: {LINK_WEBSITE}")
    lines.append("")
    lines.append("Anh/chị TVV nhớ ưu tiên dẫn khách vào các kênh chính thức này để theo dõi chương trình và thông tin mới nhất nhé.")
    return "\n".join(lines)

# ============== XỬ LÝ CÂU HỎI KINH DOANH & CHUYỂN TUYẾN TRÊN ==============
def match_business_faq(user_text: str):
    """
    Tìm câu trả lời trong faq_business_data nếu có.
    Cấu trúc gợi ý: [{"q_keywords":["hoa hồng","chiết khấu"], "answer":"..."}]
    """
    if not faq_business_data:
        return None

    t_raw = apply_synonyms(user_text or "")
    t = normalize_text(t_raw)

    for item in faq_business_data:
        try:
            keywords = item.get("q_keywords", [])
            if not keywords:
                continue
            if all(normalize_text(k) in t for k in keywords):
                return item.get("answer")
        except Exception:
            continue
    return None

def escalate_to_upline(chat_id, username, main_question, extra_note=None):
    """
    Gửi câu hỏi lên tuyến trên + log vào Sheet.
    """
    if not UPLINE_CHAT_ID:
        return (
            "Hiện tại em chưa cấu hình tuyến trên trong hệ thống. "
            "Anh/chị vui lòng liên hệ trực tiếp lãnh đạo để được hỗ trợ."
        )

    # Log riêng câu hỏi chính gửi tuyến trên
    log_event(
        log_type="UPLINE_QUESTION",
        chat_id=str(chat_id),
        username=username or "",
        role="user",
        user_text=main_question or "",
        ask_upline="yes",
        extra=extra_note or "",
    )

    msg_lines = [
        "📨 <b>YÊU CẦU HỖ TRỢ TUYẾN TRÊN</b>",
        "",
        f"👤 TVV: @{username if username else 'Không rõ'}",
        f"💬 Chat ID: <code>{chat_id}</code>",
        "",
    ]

    if main_question:
        msg_lines.append("❓ <b>Câu hỏi chính của TVV:</b>")
        msg_lines.append(main_question)
        msg_lines.append("")
    if extra_note and extra_note.strip() != (main_question or "").strip():
        msg_lines.append("📝 <b>Ghi chú thêm của TVV:</b>")
        msg_lines.append(extra_note)
        msg_lines.append("")

    msg = "\n".join(msg_lines)
    send_telegram_message(UPLINE_CHAT_ID, msg, parse_mode="HTML")

    # Tin nhắn trả lại cho TVV (echo lại nội dung đã gửi)
    if main_question:
        return (
            "Em đã gửi nội dung sau lên tuyến trên giúp anh/chị:\n"
            f"\"{main_question}\"\n\n"
            "Khi có phản hồi, em sẽ gửi lại ngay ạ. 📞"
        )
    else:
        return (
            "Em đã chuyển yêu cầu của anh/chị lên tuyến trên để được hỗ trợ. "
            "Khi có phản hồi, em sẽ báo lại ngay ạ. 📞"
        )

def handle_upline_reply(upline_text: str):
    """
    Xử lý lệnh /reply từ tuyến trên: /reply <chat_id> <nội dung>
    """
    parts = upline_text.split(maxsplit=2)
    if len(parts) < 3:
        return None, "Sai cú pháp. Dùng: /reply <chat_id> <nội dung>"

    _, chat_id_str, content = parts
    if not chat_id_str.isdigit():
        return None, "Chat ID phải là số. Ví dụ: /reply 123456789 Nội dung trả lời"

    return int(chat_id_str), content

# ============== XỬ LÝ LOGIC CHÍNH ==============
def build_ai_style_reply(user_text: str, core_answer: str) -> str:
    """
    Dùng OpenAI để làm mượt câu trả lời, giữ nguyên nội dung core.
    """
    if not client:
        return core_answer

    prompt = f"""
Bạn là trợ lý AI nội bộ, xưng hô "em" với TVV, TVV là "anh/chị".

YÊU CẦU BẮT BUỘC:
- Trả lời bằng tiếng Việt, thân thiện, rõ ràng, dễ đọc.
- CHỈ dùng định dạng HTML dành cho Telegram: <b>...</b>, <i>...</i>.
- KHÔNG dùng Markdown, KHÔNG dùng **...**, *...* hoặc bất kỳ ký tự * để in đậm.
- Không được xoá hay bịa thêm thông tin về sản phẩm, liều dùng, giá, thời gian sử dụng.
- Giữ nguyên các link (http/https) nếu có.

Câu hỏi của TVV:
\"\"\"{user_text}\"\"\"

Dưới đây là nội dung cốt lõi cần truyền đạt, bạn được phép chỉnh câu chữ nhưng không được bịa thông tin mới:
\"\"\"{core_answer}\"\"\"
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Bạn là trợ lý bán hàng nội bộ cho đội ngũ TVV. "
                        "Luôn trả lời bằng tiếng Việt, thân thiện, rõ ràng. "
                        "Không dùng Markdown, chỉ dùng HTML (<b>, <i>) nếu cần nhấn mạnh."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        content = resp.choices[0].message.content or core_answer
        # Xoá toàn bộ dấu **, * mà OpenAI có thể lỡ chèn
        content = strip_markdown(content)
        return content
    except Exception as e:
        print("[ERROR] OpenAI build_ai_style_reply:", e)
        return core_answer

# ============== HELPER CHO FLOW TUYẾN TRÊN & LỊCH SỬ ==============

def is_cancel_flow(text_norm: str) -> bool:
    """
    Nhận diện ý 'thôi / huỷ / không gửi nữa' để thoát flow tuyến trên.
    """
    cancel_keywords = [
        "thoi", "thôi",
        "huy", "huỷ", "hủy",
        "khong gui nua", "không gửi nữa",
        "khong can gui", "không cần gửi",
        "khong can nua", "không cần nữa",
        "bo qua", "bỏ qua",
        "cancel",
    ]
    return any(kw in text_norm for kw in cancel_keywords)


def is_confirm_send(text_norm: str) -> bool:
    """
    Nhận diện các câu xác nhận: đồng ý gửi / ok gửi / gửi đi...
    Dùng khi đang ở state 'waiting_confirm'.
    """
    # Câu trả lời rất ngắn kiểu "ok", "đồng ý"
    short_confirms = [
        "ok",
        "ok em",
        "oke",
        "oke em",
        "dong y",
        "đồng ý",
        "chuan", "chuẩn",
        "duoc", "được",
    ]
    if text_norm in short_confirms:
        return True

    # Câu dài hơn nhưng có cụm xác nhận
    confirm_phrases = [
        "dong y gui", "đồng ý gửi",
        "ok gui", "ok gửi",
        "gui di", "gửi đi",
        "gui len tuyen tren", "gửi lên tuyến trên",
        "gui giup", "gửi giúp",
    ]
    return any(kw in text_norm for kw in confirm_phrases)


def is_meta_history_query(text_norm: str) -> bool:
    """
    Câu kiểu: 'anh vừa hỏi gì', 'anh vừa yêu cầu em gì',
    'xem lại lịch sử', 'em vừa nói gì'...
    Dùng để trả lời lịch sử, KHÔNG dùng làm nội dung gửi tuyến trên.
    """
    history_patterns = [
        "anh vua hoi gi", "anh vừa hỏi gì",
        "anh vua yeu cau gi", "anh vừa yêu cầu gì",
        "anh vua yeu cau em gi", "anh vừa yêu cầu em gì",
        "xem lai lich su", "xem lại lịch sử",
        "em vua noi gi", "em vừa nói gì",
        "em vua tra loi gi", "em vừa trả lời gì",
        "anh vua hoi em cau gi", "anh vừa hỏi em câu gì",
    ]
    return any(kw in text_norm for kw in history_patterns)


# ============== XỬ LÝ TIN NHẮN CHÍNH ==============

def handle_user_message(chat_id, text, username=None, msg_id=None):
    global LAST_USER_TEXT, PENDING_UPLINE_STATE, PENDING_UPLINE_TEXT

    chat_key = str(chat_id)
    state = PENDING_UPLINE_STATE.get(chat_key, "")

    # Log tin nhắn người dùng (luôn log ngay đầu)
    log_event(
        log_type="USER_MESSAGE",
        chat_id=chat_id,
        username=username or "",
        role="user",
        user_text=text,
    )

        # ===== CÂU HỎI LỊCH SỬ / META_HISTORY =====
    t_norm = normalize_text(text)
    is_meta_history = False

    if ("lich su" in t_norm or "lịch sử" in t_norm or "vua hoi" in t_norm or 
        "vừa hỏi" in t_norm or "hoi gi nhi" in t_norm or "hỏi gì nhỉ" in t_norm):

        is_meta_history = True

    if is_meta_history and state not in ["waiting_content", "waiting_confirm"]:
        # 1) Lấy lịch sử gần nhất
        history = fetch_history(chat_key, limit=10) or []

        # 2) Loại bỏ chính câu vừa hỏi
        filtered = []
        for item in history:
            q = (item.get("user_text") or "").strip()
            if normalize_text(q) == t_norm:
                continue  # bỏ câu hiện tại
            filtered.append(item)

        # 3) Rút gọn tối đa 3–4 cặp
        filtered = filtered[:4]

        # 4) Format rút gọn – không in full câu dài
        lines = ["Em tóm tắt một vài lượt trao đổi gần đây nhé:\n"]

        if not filtered:
            lines.append("• Hiện tại em chưa tìm thấy lịch sử trước đó ạ.")
        else:
            for item in filtered:
                q = (item.get("user_text") or "").strip()
                a = (item.get("bot_reply") or "").strip()

                # Rút gọn phần trả lời quá dài
                if len(a) > 200:
                    a = a[:200].rstrip() + "…"

                if q:
                    lines.append(f"• Anh/chị hỏi: {q}")
                if a:
                    lines.append(f"  → Em trả lời: {a}")
                lines.append("")

        reply_text_core = "\n".join(lines).strip()
        final_reply = build_ai_style_reply(text, reply_text_core)
        send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

        log_event(
            log_type="BOT_REPLY",
            chat_id=chat_id,
            username=username or "",
            role="bot",
            bot_reply=final_reply,
            intent="META_HISTORY",
        )
        LAST_USER_TEXT[chat_key] = text
        return

    # ===== 0.1. META_HISTORY CHUNG: 'anh vừa hỏi gì / vừa yêu cầu gì / xem lại lịch sử...' =====
    if is_meta_history_query(t_norm):
        last_user = LAST_USER_TEXT.get(chat_key, "")
        history_items = fetch_history(chat_key, limit=5)  # có thể rỗng nếu Apps Script chưa làm

        if history_items:
            # Tuỳ cấu trúc Apps Script trả về, giả sử mỗi item có: user_text, bot_reply
            lines = ["Em tóm tắt một vài lượt trao đổi gần đây nhé:"]
            for item in history_items:
                u = item.get("user_text", "").strip()
                b = item.get("bot_reply", "").strip()
                if not u and not b:
                    continue
                lines.append(f"• Anh/chị hỏi: {u}")
                if b:
                    lines.append(f"  → Em trả lời: {b[:200]}{'...' if len(b) > 200 else ''}")
            reply_text_core = "\n".join(lines)
        elif last_user:
            reply_text_core = (
                "Ngay trước câu này, anh/chị vừa hỏi em:\n"
                f"\"{last_user}\"\n\n"
                "Nếu anh/chị muốn em gửi câu hỏi nào lên tuyến trên thì nhắn lại rõ nội dung giúp em nhé."
            )
        else:
            reply_text_core = (
                "Hiện tại em chưa lưu được lịch sử câu hỏi trước đó của anh/chị trong phiên này. "
                "Anh/chị có thể nhắn lại nội dung cần hỏi, em sẽ hỗ trợ ngay ạ."
            )

        final_reply = build_ai_style_reply(text, reply_text_core)
        send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

        log_event(
            log_type="BOT_REPLY",
            chat_id=chat_id,
            username=username or "",
            role="bot",
            bot_reply=final_reply,
            intent="META_HISTORY",
        )

        LAST_USER_TEXT[chat_key] = text
        return

    # ===== 0.2. NẾU ĐANG Ở FLOW TUYẾN TRÊN MÀ NGƯỜI DÙNG NÓI 'THÔI / HUỶ' → THOÁT FLOW =====
    if state in ("waiting_content", "waiting_confirm") and is_cancel_flow(t_norm):
        PENDING_UPLINE_STATE.pop(chat_key, None)
        PENDING_UPLINE_TEXT.pop(chat_key, None)

        reply_text_core = (
            "Dạ em đã <b>hủy việc gửi câu hỏi lên tuyến trên</b> cho cuộc trò chuyện này.\n"
            "Anh/chị cứ tiếp tục hỏi các nội dung khác, em sẽ hỗ trợ như bình thường ạ."
        )

        final_reply = build_ai_style_reply(text, reply_text_core)
        send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

        log_event(
            log_type="BOT_REPLY",
            chat_id=chat_id,
            username=username or "",
            role="bot",
            bot_reply=final_reply,
            intent="CANCEL_UPLINE_FLOW",
        )

        LAST_USER_TEXT[chat_key] = text
        return

    reply_text_core = ""
    ask_upline_flag = False

    # ===== 1. ĐANG Ở TRẠNG THÁI CHỜ TVV NHẬP NỘI DUNG CÂU HỎI GỬI TUYẾN TRÊN =====
    if state == "waiting_content":
        main_question = text.strip()
        if not main_question:
            reply_text_core = (
                "Em chưa thấy anh/chị nhập nội dung câu hỏi. "
                "Anh/chị gõ rõ giúp em nội dung muốn gửi tuyến trên nhé."
            )
            final_reply = build_ai_style_reply(text, reply_text_core)
            send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

            log_event(
                log_type="BOT_REPLY",
                chat_id=chat_id,
                username=username or "",
                role="bot",
                bot_reply=final_reply,
                intent="BUSINESS_QUESTION",
                ask_upline="pending",
            )
            LAST_USER_TEXT[chat_key] = text
            return

        # Lưu câu hỏi, chuyển sang bước xác nhận
        PENDING_UPLINE_TEXT[chat_key] = {"main_question": main_question}
        PENDING_UPLINE_STATE[chat_key] = "waiting_confirm"

        reply_text_core = (
            "Em ghi lại nội dung câu hỏi để gửi tuyến trên như sau:\n"
            f"\"{main_question}\"\n\n"
            "Anh/chị xem giúp em đã đúng ý chưa ạ?\n"
            "• Nếu ĐÚNG, anh/chị trả lời: <b>Đồng ý</b>, <b>Đồng ý gửi</b>, <b>OK</b> hoặc <b>Gửi đi</b>.\n"
            "• Nếu CẦN SỬA, anh/chị nhắn lại nội dung mới, em sẽ cập nhật trước khi gửi."
        )

        final_reply = build_ai_style_reply(text, reply_text_core)
        send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

        log_event(
            log_type="BOT_REPLY",
            chat_id=chat_id,
            username=username or "",
            role="bot",
            bot_reply=final_reply,
            intent="BUSINESS_QUESTION",
            ask_upline="waiting_confirm",
        )
        LAST_USER_TEXT[chat_key] = text
        return

    # ===== 2. ĐANG Ở TRẠNG THÁI CHỜ XÁC NHẬN GỬI TUYẾN TRÊN =====
    if state == "waiting_confirm":
        confirm_norm = t_norm
        main_question = (PENDING_UPLINE_TEXT.get(chat_key) or {}).get("main_question", "")

        if is_confirm_send(confirm_norm) and main_question:
            # Gửi tuyến trên thật sự
            reply_text_core = escalate_to_upline(
                chat_id=chat_id,
                username=username,
                main_question=main_question,
                extra_note=None,
            )

            # Xoá trạng thái chờ
            PENDING_UPLINE_STATE.pop(chat_key, None)
            PENDING_UPLINE_TEXT.pop(chat_key, None)

            final_reply = build_ai_style_reply(text, reply_text_core)
            send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

            log_event(
                log_type="BOT_REPLY",
                chat_id=chat_id,
                username=username or "",
                role="bot",
                bot_reply=final_reply,
                intent="BUSINESS_QUESTION",
                ask_upline="yes",
            )
            LAST_USER_TEXT[chat_key] = text
            return
        else:
            # Xem tin nhắn này như nội dung MỚI cần gửi tuyến trên
            main_question = text.strip()
            PENDING_UPLINE_TEXT[chat_key] = {"main_question": main_question}
            PENDING_UPLINE_STATE[chat_key] = "waiting_confirm"

            reply_text_core = (
                "Em hiểu là anh/chị muốn chỉnh lại nội dung câu hỏi. "
                "Hiện tại em sẽ chuẩn bị gửi với nội dung:\n"
                f"\"{main_question}\"\n\n"
                "Anh/chị kiểm tra giúp em, nếu ĐÚNG thì trả lời: <b>Đồng ý</b> hoặc <b>OK gửi</b>. "
                "Nếu vẫn chưa đúng, anh/chị gõ lại nội dung mới nhé."
            )

            final_reply = build_ai_style_reply(text, reply_text_core)
            send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

            log_event(
                log_type="BOT_REPLY",
                chat_id=chat_id,
                username=username or "",
                role="bot",
                bot_reply=final_reply,
                intent="BUSINESS_QUESTION",
                ask_upline="waiting_confirm",
            )
            LAST_USER_TEXT[chat_key] = text
            return

    # ===== 3. TRƯỜNG HỢP BÌNH THƯỜNG: PHÂN TÍCH INTENT & TRẢ LỜI =====
    intent_info = classify_intent_with_openai(text)
    intent = intent_info.get("intent", "SMALL_TALK")
    health_issue = intent_info.get("health_issue")
    product_query = intent_info.get("product_query")
    needs = intent_info.get("needs") or []
    ask_upline_flag = bool(intent_info.get("ask_upline", False))

    if intent == "HEALTH_COMBO":
        combo = search_combo_by_health_issue(health_issue or text)
        reply_text_core = format_combo_reply(combo, needs, health_issue or text)

    elif intent == "HEALTH_PRODUCT":
        if product_query:
            product = search_product_by_name_or_code(product_query)
            reply_text_core = format_product_reply(product, needs, health_issue=None)
        else:
            products = search_product_by_health_issue(health_issue or text)
            if not products:
                reply_text_core = format_product_reply(None, needs, health_issue or text)
            elif len(products) == 1:
                reply_text_core = format_product_reply(products[0], needs, health_issue or text)
            else:
                lines = [f"<b>Một số sản phẩm phù hợp với vấn đề {health_issue or text}:</b>"]
                for p in products:
                    name = strip_markdown(p.get("name", "Sản phẩm"))
                    code = strip_markdown(p.get("code", ""))
                    url = (p.get("product_url", "") or "").strip()
                    line = f"• {name}"
                    if code:
                        line += f" (Mã: {code})"
                    if url:
                        line += f"\n   🔗 {url}"
                    lines.append(line)
                lines.append("")
                lines.append("Nếu anh/chị muốn xem chi tiết sản phẩm nào, hãy hỏi theo tên hoặc mã sản phẩm cụ thể nhé.")
                reply_text_core = "\n".join(lines)

    elif intent == "PRODUCT_DETAIL":
        product = search_product_by_name_or_code(product_query or text)
        reply_text_core = format_product_reply(product, needs, health_issue=None)

    elif intent == "HOW_TO_BUY":
        reply_text_core = format_faq_reply(faq_buy_data)

    elif intent == "HOW_TO_PAY":
        reply_text_core = format_faq_reply(faq_payment_data)

    elif intent == "NAVIGATION":
        reply_text_core = format_navigation_reply()

    elif intent == "BUSINESS_QUESTION":
        faq_answer = match_business_faq(text)
        if faq_answer:
            reply_text_core = faq_answer
        else:
            # Luôn bắt người dùng nhập nội dung cụ thể trước khi gửi tuyến trên
            ask_upline_flag = True
            PENDING_UPLINE_STATE[chat_key] = "waiting_content"
            PENDING_UPLINE_TEXT.pop(chat_key, None)
            reply_text_core = (
                "Vấn đề này thuộc nhóm chính sách/kinh doanh hoặc tình huống khó.\n\n"
                "Anh/chị cho em <b>nội dung câu hỏi cụ thể</b> muốn gửi tuyến trên "
                "(tình huống, sản phẩm/combo, mức giá, chính sách...), "
                "em sẽ ghi lại rồi nhắc lại để anh/chị xác nhận trước khi gửi đi ạ."
            )

    else:
        reply_text_core = (
            "Em là trợ lý AI nội bộ hỗ trợ anh/chị TVV trong việc tư vấn sản phẩm, combo và cách chăm sóc sức khoẻ.\n\n"
            "Anh/chị có thể hỏi em về:\n"
            "• Combo cho một vấn đề sức khỏe (ví dụ: tiểu đường, dạ dày, xương khớp...)\n"
            "• Thông tin chi tiết một sản phẩm (thành phần, lợi ích, cách dùng...)\n"
            "• Cách mua hàng, thanh toán, kênh chính thức của công ty\n"
            "• Những thắc mắc về kinh doanh, chính sách (em sẽ hỗ trợ chuyển tuyến trên nếu cần) 😊"
        )

    final_reply = build_ai_style_reply(text, reply_text_core)
    send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

    log_event(
        log_type="BOT_REPLY",
        chat_id=chat_id,
        username=username or "",
        role="bot",
        bot_reply=final_reply,
        intent=intent,
        health_issue=health_issue or "",
        product_query=product_query or "",
        ask_upline="yes" if ask_upline_flag else "no",
    )

    LAST_USER_TEXT[chat_key] = text

# ============== ROUTES FLASK ==============
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "message": "Welllab AI Assistant is running."})

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    from_user = message.get("from", {})
    username = from_user.get("username") or from_user.get("first_name")
    text = message.get("text", "") or ""

    if not chat_id:
        return jsonify({"ok": True})

    # Tin nhắn từ tuyến trên
    if UPLINE_CHAT_ID and str(chat_id) == str(UPLINE_CHAT_ID):
        if text.startswith("/reply"):
            target_chat_id, content = handle_upline_reply(text)
            if not target_chat_id:
                send_telegram_message(chat_id, content)
            else:
                # Gửi nội dung cho TVV
                send_telegram_message(target_chat_id, f"📣 Phản hồi từ tuyến trên:\n\n{content}")
                send_telegram_message(chat_id, "Đã gửi trả lời cho TVV.")

                # Log lại phản hồi tuyến trên
                log_event(
                    log_type="UPLINE_REPLY",
                    chat_id=target_chat_id,
                    username=username or "",
                    role="upline",
                    bot_reply=content,
                )
        else:
            send_telegram_message(
                chat_id,
                "Đây là kênh tuyến trên. Để trả lời TVV, dùng lệnh:\n/reply <chat_id> <nội dung>",
            )
        return jsonify({"ok": True})

    # Lệnh /start
    if text.startswith("/start"):
        welcome = (
            "Chào anh/chị, em là <b>Trợ lý AI Welllab</b> hỗ trợ đội ngũ TVV 💚\n\n"
            "Anh/chị có thể hỏi em về:\n"
            "• Combo cho các vấn đề sức khỏe (tiểu đường, dạ dày, mỡ máu, xương khớp...)\n"
            "• Thông tin chi tiết sản phẩm (thành phần, lợi ích, cách dùng...)\n"
            "• Cách mua hàng, thanh toán, kênh chính thức của công ty\n"
            "• Câu hỏi kinh doanh, chính sách (em sẽ hỗ trợ chuyển tuyến trên nếu cần)\n\n"
            "Anh/chị cứ nhắn tự nhiên như đang hỏi một leader nhé 🥰"
        )
        send_telegram_message(chat_id, welcome, reply_to_message_id=message.get("message_id"))

        log_event(
            log_type="BOT_REPLY",
            chat_id=chat_id,
            username=username or "",
            role="bot",
            bot_reply=welcome,
            intent="START",
        )
        return jsonify({"ok": True})

    # Các tin nhắn còn lại
    handle_user_message(chat_id, text, username=username, msg_id=message.get("message_id"))

    return jsonify({"ok": True})

# ============== MAIN ==============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)


