import os
import json
import re
import time
from collections import defaultdict
from datetime import datetime

import requests
from dotenv import load_dotenv

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ========== ENV ==========
load_dotenv()

OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")
LOG_SHEET_EXPORT_URL = os.getenv("LOG_SHEET_EXPORT_URL", "")
BASE_DIR             = os.path.dirname(__file__)
DATA_DIR             = os.path.join(BASE_DIR, "data")
PRODUCTS_PATH        = os.path.join(DATA_DIR, "products.json")
COMBOS_PATH          = os.path.join(DATA_DIR, "combos.json")
OUT_PATH             = os.path.join(DATA_DIR, "update_suggestions.json")

if not OPENAI_API_KEY or OpenAI is None:
    raise RuntimeError("Cần OPENAI_API_KEY và thư viện openai để chạy script tự học.")

client = OpenAI(api_key=OPENAI_API_KEY)


# ========== Helper ==========
def normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_logs():
    if not LOG_SHEET_EXPORT_URL:
        raise RuntimeError("Thiếu LOG_SHEET_EXPORT_URL trong .env")
    print("[*] Fetching logs from Google Sheet...")
    resp = requests.get(LOG_SHEET_EXPORT_URL, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Lỗi khi export logs: {data}")
    return data.get("logs", [])


# ========== Chuẩn bị dữ liệu sản phẩm / combo ==========
def load_products_and_combos():
    products_data = load_json(PRODUCTS_PATH)
    combos_data   = load_json(COMBOS_PATH)

    products = products_data.get("products", products_data)
    combos   = combos_data.get("combos", combos_data)

    # Map code → product
    product_map = {}
    for p in products:
        code = str(p.get("code", "")).lstrip("#").strip()
        if not code:
            continue
        p["code"] = code
        if "aliases" not in p or not isinstance(p["aliases"], list):
            p["aliases"] = []
        product_map[code] = p

    # Map id → combo
    combo_map = {}
    for c in combos:
        cid = c.get("id") or normalize_text(c.get("name", ""))
        c["id"] = cid
        if "aliases" not in c or not isinstance(c["aliases"], list):
            c["aliases"] = []
        combo_map[cid] = c

    return products_data, combos_data, product_map, combo_map


# ========== Gọi OpenAI để gợi ý alias & mapping ==========
def ask_model_for_mapping(question_text: str, product_map, combo_map):
    """
    Trả về dict:
    {
      "type": "product" | "combo" | "none",
      "codes": [...],
      "combo_id": "..." hoặc null,
      "confidence": 0..1,
      "aliases_to_add": ["...","..."]
    }
    """
    products_brief = []
    for code, p in product_map.items():
        products_brief.append({
            "code": code,
            "name": p.get("name", ""),
            "aliases": p.get("aliases", []),
            "health_tags": p.get("health_tags", [])
        })

    combos_brief = []
    for cid, c in combo_map.items():
        combos_brief.append({
            "id": cid,
            "name": c.get("name", ""),
            "aliases": c.get("aliases", []),
            "health_tags": c.get("health_tags", []),
            "product_codes": [str(it.get("product_code","")) for it in c.get("products", [])]
        })

    system_prompt = (
        "Bạn là trợ lý giúp map câu hỏi của TVV với sản phẩm hoặc combo trong danh mục.\n"
        "DỮ LIỆU cho sẵn đã CHÍNH XÁC, bạn KHÔNG được tự bịa thêm sản phẩm hay combo mới.\n\n"
        "YÊU CẦU:\n"
        "1. Đọc câu hỏi của TVV.\n"
        "2. Xem danh sách products & combos.\n"
        "3. Nếu thấy rõ ràng nên gợi ý 1 hoặc vài sản phẩm → chọn type = 'product', điền 'codes'.\n"
        "4. Nếu thấy rõ ràng nên gợi ý 1 combo → type = 'combo', điền 'combo_id'.\n"
        "5. Nếu không tự tin → type = 'none', confidence < 0.7.\n"
        "6. 'aliases_to_add' là các cụm từ trong câu hỏi nên thêm vào aliases của sản phẩm/combo để lần sau dễ nhận.\n"
        "7. Trả về JSON với keys: type, codes, combo_id, confidence, aliases_to_add.\n"
        "8. 'confidence' là số từ 0.0 đến 1.0.\n"
        "9. Tuyệt đối không thêm claim công dụng ngoài những gì dữ liệu gốc đã ám chỉ.\n"
    )

    user_content = {
        "question": question_text,
        "products": products_brief,
        "combos": combos_brief
    }

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_content, ensure_ascii=False)}
        ]
    )

    raw = resp.choices[0].message.content.strip()
    try:
        data = json.loads(raw)
    except Exception:
        print("!! Model trả về không parse được JSON, content:", raw[:200])
        return {
            "type": "none",
            "codes": [],
            "combo_id": None,
            "confidence": 0.0,
            "aliases_to_add": []
        }

    return {
        "type": data.get("type", "none"),
        "codes": data.get("codes", []),
        "combo_id": data.get("combo_id"),
        "confidence": float(data.get("confidence", 0.0)),
        "aliases_to_add": data.get("aliases_to_add", [])
    }


# ========== Tự học alias từ Logs → sinh update_suggestions.json ==========
def main():
    logs = fetch_logs()
    if not logs:
        print("[*] Không có log nào, dừng.")
        return

    # Gom theo câu hỏi (Text chuẩn hóa)
    grouped = defaultdict(list)
    for row in logs:
        text = (row.get("Text") or "").strip()
        if not text:
            continue
        grouped[normalize_text(text)].append(text)

    print(f"[*] Có {len(grouped)} nhóm câu hỏi để xem xét.")

    _, _, product_map, combo_map = load_products_and_combos()

    MIN_COUNT = 3          # Ít nhất 3 lần xuất hiện mới gợi ý
    CONF_THRESHOLD = 0.9   # Chỉ nhận khi confidence >= 0.9

    product_alias_suggestions = []
    combo_alias_suggestions   = []

    for norm_q, texts in grouped.items():
        if len(texts) < MIN_COUNT:
            continue

        question_example = texts[0]
        print(f"\n=== XỬ LÝ CÂU HỎI: \"{question_example}\" (xuất hiện {len(texts)} lần) ===")

        mapping = ask_model_for_mapping(question_example, product_map, combo_map)
        mtype   = mapping["type"]
        conf    = mapping["confidence"]
        aliases_to_add = mapping.get("aliases_to_add", []) or []

        print("Model đề xuất:", mapping)

        if conf < CONF_THRESHOLD or mtype == "none":
            print("→ BỎ QUA (confidence thấp hoặc type=none)")
            continue

        if mtype == "product":
            codes = []
            names = []
            for code in mapping["codes"]:
                code = str(code).strip()
                p = product_map.get(code)
                if not p:
                    continue
                codes.append(code)
                names.append(p.get("name", ""))
            if not codes:
                print("→ Không tìm được product hợp lệ, bỏ qua.")
                continue

            sugg_aliases = list({question_example, *aliases_to_add})
            product_alias_suggestions.append({
                "question_example": question_example,
                "times_seen": len(texts),
                "codes": codes,
                "product_names": names,
                "suggested_aliases": sugg_aliases,
                "confidence": conf
            })

        elif mtype == "combo":
            cid = mapping.get("combo_id")
            c = combo_map.get(cid)
            if not c:
                print("→ combo_id không tồn tại, bỏ qua.")
                continue

            sugg_aliases = list({question_example, *aliases_to_add})
            combo_alias_suggestions.append({
                "question_example": question_example,
                "times_seen": len(texts),
                "combo_id": cid,
                "combo_name": c.get("name", ""),
                "suggested_aliases": sugg_aliases,
                "confidence": conf
            })

        time.sleep(0.5)  # tránh spam OpenAI quá nhanh

    update_obj = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "from_logs_url": LOG_SHEET_EXPORT_URL,
        "stats": {
            "question_groups_total": len(grouped),
            "min_count_threshold": MIN_COUNT,
            "confidence_threshold": CONF_THRESHOLD,
            "product_alias_suggestions": len(product_alias_suggestions),
            "combo_alias_suggestions": len(combo_alias_suggestions),
        },
        "product_aliases": product_alias_suggestions,
        "combo_aliases": combo_alias_suggestions
    }

    save_json(OUT_PATH, update_obj)
    print("\n✅ Đã sinh file gợi ý:", OUT_PATH)


if __name__ == "__main__":
    main()
