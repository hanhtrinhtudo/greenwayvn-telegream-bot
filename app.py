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

def log_to_sheet(payload: dict):
    if not LOG_SHEET_WEBHOOK_URL:
        return
    try:
        requests.post(LOG_SHEET_WEBHOOK_URL, json=payload, timeout=15)
    except Exception as e:
        print("[WARN] Log sheet lỗi:", e)

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

    # Áp synonyms
    syn = apply_synonyms(base)
    if syn and syn not in res:
        res.append(syn)

    # Dùng health_tags_map để mở rộng
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
    """
    Tìm combo theo vấn đề sức khỏe (dùng health_tags & aliases & name),
    có sử dụng health_tags_map + synonyms.
    """
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
    """
    Tìm 1–3 sản phẩm lẻ liên quan đến vấn đề sức khoẻ,
    có dùng health_tags_map + synonyms.
    """
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

    # Giới hạn 3 sản phẩm cho đỡ loãng
    return results[:3]

def search_product_by_name_or_code(query: str):
    """
    Tìm sản phẩm theo mã hoặc tên/alias.
    """
    if not query:
        return None

    # Chuẩn hóa bằng synonyms trước khi normalize
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
    """
    Dùng OpenAI để phân tích:
    - intent
    - health_issue
    - product_query
    - needs
    - ask_upline
    Trả về dict chuẩn.
    """
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
        if any(k in t for k in ["tieu duong", "đai thao duong"]):
            base_result["intent"] = "HEALTH_COMBO"
            base_result["health_issue"] = "tiểu đường"
        elif any(k in t for k in ["da day", "dạ dày", "bao tu", "bao tử", "trao nguoc"]):
            base_result["intent"] = "HEALTH_PRODUCT"
            base_result["health_issue"] = "đau dạ dày / dạ dày"
        elif any(k in t for k in ["mua hang", "dat hang", "đặt hàng", "mua như the nao", "mua như thế nào"]):
            base_result["intent"] = "HOW_TO_BUY"
        elif any(k in t for k in ["thanh toan", "thanh toán", "chuyen khoan", "chuyển khoản"]):
            base_result["intent"] = "HOW_TO_PAY"
        elif any(k in t for k in ["fanpage", "kenh", "kênh", "website", "trang web"]):
            base_result["intent"] = "NAVIGATION"
        elif any(k in t for k in ["chinh sach", "hoa hong", "kinh doanh", "thưởng", "chiết khấu"]):
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
            f"Hiện tại em chưa tìm thấy combo phù hợp trong dữ liệu cho vấn đề: <b>{health_issue}</b>.\n"
            "Anh/chị thử mô tả rõ hơn tình trạng hoặc liên hệ tuyến trên để được hỗ trợ chi tiết hơn nhé."
        )

    name = combo.get("name", "Combo phù hợp")
    header_text = combo.get("header_text", "")
    duration_text = combo.get("duration_text", "")
    combo_url = combo.get("combo_url", "")
    products = combo.get("products", [])

    lines = []
    lines.append(f"<b>{name}</b>")
    if header_text:
        lines.append(header_text)

    # Danh sách sản phẩm trong combo
    if not needs or "products" in needs or "combo" in needs:
        if products:
            lines.append("")
            lines.append("<b>Thành phần combo:</b>")
            for idx, p in enumerate(products, start=1):
                pname = p.get("name") or p.get("product_name") or p.get("product_code") or "Sản phẩm"
                role_text = p.get("role_text", "")
                dose_text = p.get("dose_text", "")
                product_url = p.get("product_url", "")
                line = f"{idx}. {pname}"
                if role_text:
                    line += f" – {role_text}"
                if dose_text:
                    line += f"\n   👉 Cách dùng: {dose_text}"
                if product_url and ("product_links" in needs or not needs):
                    line += f"\n   🔗 Link: {product_url}"
                lines.append(line)

    # Thời gian sử dụng
    if (not needs) or ("duration" in needs):
        if duration_text:
            lines.append("")
            lines.append(f"⏱ <b>Thời gian khuyến nghị:</b> {duration_text}")

    lines.append("")
    if combo_url and ("product_links" in needs or not needs):
        lines.append(f"🛒 Link combo (nếu đặt online): {combo_url}")
        lines.append("")

    lines.append(
        "Lưu ý: Đây là sản phẩm hỗ trợ, không thay thế thuốc điều trị. "
        "Anh/chị TVV nên hỏi kỹ tình trạng và thuốc đang dùng trước khi tư vấn cho khách."
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

    name = product.get("name", "Sản phẩm")
    code = product.get("code", "")
    ingredients = product.get("ingredients_text", "")
    benefits = product.get("benefits_text", "")
    usage = product.get("usage_text", "")
    price_text = product.get("price_text", "")
    duration_text = product.get("duration_text", "")  # có thể chưa có trong file, không sao
    product_url = product.get("product_url", "")
    warnings = product.get("notes_for_tvv", "")

    lines = []
    title = f"<b>{name}</b>"
    if code:
        title += f" (Mã: {code})"
    lines.append(title)

    if price_text:
        lines.append(f"💰 Giá tham khảo: {price_text}")

    # Thành phần
    if (not needs) or ("ingredients" in needs):
        if ingredients:
            lines.append("")
            lines.append(f"<b>Thành phần chính:</b> {ingredients}")

    # Lợi ích
    if (not needs) or ("benefits" in needs):
        if benefits:
            lines.append("")
            lines.append("<b>Lợi ích nổi bật:</b>")
            lines.append(benefits)

    # Cách dùng
    if (not needs) or ("usage" in needs):
        if usage:
            lines.append("")
            lines.append(f"<b>Cách dùng khuyến nghị:</b> {usage}")

    # Thời gian sử dụng (nếu có)
    if (not needs) or ("duration" in needs):
        if duration_text:
            lines.append("")
            lines.append(f"<b>Thời gian sử dụng nên duy trì:</b> {duration_text}")

    # Link
    if (not needs) or ("product_links" in needs):
        if product_url:
            lines.append("")
            lines.append(f"🔗 <b>Link sản phẩm:</b> {product_url}")

    # Cảnh báo / lưu ý cho TVV
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
    """
    faq_list: có thể là list string hoặc list object {title, content}
    """
    if not faq_list:
        return "Hiện tại em chưa có dữ liệu hướng dẫn chi tiết trong hệ thống. Anh/chị giúp em liên hệ tuyến trên để được hỗ trợ nhé."

    # Nếu là list string
    if all(isinstance(x, str) for x in faq_list):
        return "\n".join(faq_list)

    # Nếu là list object
    lines = []
    for i, item in enumerate(faq_list, start=1):
        if isinstance(item, str):
            lines.append(item)
        elif isinstance(item, dict):
            title = item.get(key_field, f"Bước {i}")
            content = item.get("content", "")
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

def escalate_to_upline(chat_id, username, text):
    """
    Gửi câu hỏi lên tuyến trên, log lại.
    """
    if not UPLINE_CHAT_ID:
        return "Hiện tại em chưa cấu hình tuyến trên trong hệ thống. Anh/chị vui lòng liên hệ trực tiếp lãnh đạo để được hỗ trợ."

    msg = (
        f"📨 <b>YÊU CẦU HỖ TRỢ TUYẾN TRÊN</b>\n\n"
        f"👤 TVV: @{username if username else 'Không rõ'}\n"
        f"💬 Chat ID: <code>{chat_id}</code>\n\n"
        f"❓ Nội dung:\n{text}"
    )
    send_telegram_message(UPLINE_CHAT_ID, msg, parse_mode="HTML")

    return (
        "Vấn đề này thuộc nhóm chính sách/kinh doanh hoặc tình huống khó, "
        "em đã chuyển nội dung lên tuyến trên để hỗ trợ anh/chị. "
        "Khi có phản hồi, em sẽ gửi lại ngay ạ. 📞"
    )

def handle_upline_reply(upline_text: str):
    """
    Xử lý lệnh /reply từ tuyến trên:
    Format: /reply <chat_id> <nội dung>
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
    Nhờ OpenAI chỉnh câu trả lời cho mềm mại hơn, giữ nguyên thông tin chính.
    Nếu không có OpenAI, trả về core_answer luôn.
    """
    if not client:
        return core_answer

    prompt = f"""
Bạn là trợ lý AI nội bộ, xưng hô "em" với TVV, TVV là "anh/chị".
Hãy giữ nguyên các thông tin quan trọng (số lượng, liều dùng, thời gian, tên sản phẩm),
chỉ viết lại cho mềm mại, thân thiện, rõ ràng, dễ đọc.

Câu hỏi của TVV:
\"\"\"{user_text}\"\"\"

Dưới đây là nội dung cốt lõi cần truyền đạt, bạn được phép chỉnh câu chữ nhưng không được bịa thông tin mới:
\"\"\"{core_answer}\"\"\"
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Bạn là trợ lý bán hàng nội bộ cho TVV, trả lời bằng tiếng Việt, thân thiện, rõ ràng."},
                {"role": "user", "content": prompt},
            ],
        )
        return resp.choices[0].message.content
    except Exception as e:
        print("[ERROR] OpenAI build_ai_style_reply:", e)
        return core_answer

def handle_user_message(chat_id, text, username=None, msg_id=None):
    """
    Hàm trung tâm xử lý tin nhắn từ TVV.
    Giữ nguyên logic cũ, chỉ thêm bước áp synonyms + health_tags_map.
    """
    intent_info = classify_intent_with_openai(text)
    intent = intent_info.get("intent", "SMALL_TALK")
    health_issue = intent_info.get("health_issue")
    product_query = intent_info.get("product_query")
    needs = intent_info.get("needs") or []
    ask_upline = bool(intent_info.get("ask_upline", False))

    # Log sơ bộ
    log_payload = {
        "source": "telegram",
        "chat_id": str(chat_id),
        "username": username or "",
        "user_text": text,
        "intent": intent,
        "health_issue": health_issue or "",
        "product_query": product_query or "",
    }

    reply_text_core = ""

    # ====== PHÂN NHÁNH THEO INTENT ======
    if intent in ["HEALTH_COMBO"]:
        combo = search_combo_by_health_issue(health_issue or text)
        reply_text_core = format_combo_reply(combo, needs, health_issue or text)

    elif intent in ["HEALTH_PRODUCT"]:
        # Nếu truy vấn sản phẩm cụ thể
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
                # Nhiều sản phẩm, liệt kê gợi ý
                lines = [f"<b>Một số sản phẩm phù hợp với vấn đề {health_issue or text}:</b>"]
                for p in products:
                    name = p.get("name", "Sản phẩm")
                    code = p.get("code", "")
                    url = p.get("product_url", "")
                    line = f"• {name}"
                    if code:
                        line += f" (Mã: {code})"
                    if url:
                        line += f"\n   🔗 {url}"
                    lines.append(line)
                lines.append("")
                lines.append("Nếu anh/chị muốn xem chi tiết sản phẩm nào, hãy hỏi theo tên hoặc mã sản phẩm cụ thể nhé.")
                reply_text_core = "\n".join(lines)

    elif intent in ["PRODUCT_DETAIL"]:
        product = search_product_by_name_or_code(product_query or text)
        reply_text_core = format_product_reply(product, needs, health_issue=None)

    elif intent == "HOW_TO_BUY":
        reply_text_core = format_faq_reply(faq_buy_data)
    elif intent == "HOW_TO_PAY":
        reply_text_core = format_faq_reply(faq_payment_data)
    elif intent == "NAVIGATION":
        reply_text_core = format_navigation_reply()
    elif intent == "BUSINESS_QUESTION":
        # Thử trả lời từ FAQ nội bộ
        faq_answer = match_business_faq(text)
        if faq_answer:
            reply_text_core = faq_answer
        else:
            # Nếu được gợi ý ask_upline hoặc không có dữ liệu
            ask_upline = True
            reply_text_core = escalate_to_upline(chat_id, username, text)
    else:
        # SMALL_TALK hoặc không rõ
        reply_text_core = (
            "Em là trợ lý AI nội bộ hỗ trợ anh/chị TVV trong việc tư vấn sản phẩm, combo và cách chăm sóc sức khoẻ.\n\n"
            "Anh/chị có thể hỏi em về:\n"
            "• Combo cho một vấn đề sức khỏe (ví dụ: tiểu đường, dạ dày, xương khớp...)\n"
            "• Thông tin chi tiết một sản phẩm (thành phần, lợi ích, cách dùng...)\n"
            "• Cách mua hàng, thanh toán, kênh chính thức của công ty\n"
            "• Những thắc mắc về kinh doanh, chính sách (em sẽ hỗ trợ chuyển tuyến trên nếu cần) 😊"
        )

    # ====== LOG THÊM THÔNG TIN ======
    log_payload["ask_upline"] = "yes" if ask_upline else "no"
    log_payload["final_answer_preview"] = reply_text_core[:500]
    log_to_sheet(log_payload)

    # ====== NHỜ AI “MỀM HÓA” CÂU TRẢ LỜI ======
    final_reply = build_ai_style_reply(text, reply_text_core)

    send_telegram_message(chat_id, final_reply, reply_to_message_id=msg_id)

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

    # Nếu là tin từ tuyến trên (upline)
    if UPLINE_CHAT_ID and str(chat_id) == str(UPLINE_CHAT_ID):
        if text.startswith("/reply"):
            target_chat_id, content = handle_upline_reply(text)
            if not target_chat_id:
                send_telegram_message(chat_id, content)
            else:
                # Gửi nội dung cho TVV
                send_telegram_message(target_chat_id, f"📣 Phản hồi từ tuyến trên:\n\n{content}")
                send_telegram_message(chat_id, "Đã gửi trả lời cho TVV.")
        else:
            send_telegram_message(
                chat_id,
                "Đây là kênh tuyến trên. Để trả lời TVV, dùng lệnh:\n/reply <chat_id> <nội dung>",
            )
        return jsonify({"ok": True})

    # Xử lý lệnh /start
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
        return jsonify({"ok": True})

    # Các tin nhắn còn lại
    handle_user_message(chat_id, text, username=username, msg_id=message.get("message_id"))

    return jsonify({"ok": True})

# ============== MAIN ==============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
