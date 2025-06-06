from flask import Flask, request, jsonify
import os
import re
import tempfile
import mysql.connector as mysql
from dotenv import load_dotenv
from PIL import Image
import pytesseract

load_dotenv()

app = Flask(__name__)

# MySQL config from .env
mysql_config = {
    "host": os.getenv("MYSQL_HOST"),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE")
}

def extract_boxes(image_path, conf_threshold=0.6):
    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    boxes = []
    for i in range(len(data['level'])):
        conf = float(data['conf'][i])
        text = data['text'][i].strip()
        if conf > conf_threshold * 100 and text:
            x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
            box = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
            boxes.append({
                'text': text,
                'conf': conf / 100,
                'box': box,
                'x': x + w / 2,
                'y': y + h / 2
            })
    return boxes

def group_by_rows(boxes, y_thresh=15):
    boxes.sort(key=lambda b: b["y"])
    rows, current_row, last_y = [], [], -1000
    for box in boxes:
        if abs(box["y"] - last_y) > y_thresh:
            if current_row:
                rows.append(sorted(current_row, key=lambda b: b["x"]))
            current_row = [box]
        else:
            current_row.append(box)
        last_y = box["y"]
    if current_row:
        rows.append(sorted(current_row, key=lambda b: b["x"]))
    return rows

def detect_price(text):
    return re.search(r'(\u20B9|Rs\.?)?\s?\d{1,4}([.,]\d{1,2})?', text)

def is_valid_item(text):
    if not text or len(text.strip()) <= 2 or (text.strip().isupper() and len(text.strip()) <= 3):
        return False
    noise_keywords = { 'am', 'pm', 'yo', 'l', 't', 'a', 'b', '/', '-', '|', ':', '.', ',', '–', '—', '_', '(', ')',
                       'daily', 'only', 'each', 'per', 'day', 'week', 'month', 'with', 'served', 'includes',
                       'available', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'timings', 'timing',
                       'from', 'at', 'till', 'until', 'for', 'special', 'offer', 'extra', 'add-on',
                       'optional', 'combo', 'set', 'option', 'mrp', 'gst', 'inclusive', 'exclusive',
                       'taxes', 'inc.', 'excl.', 'incl.' }
    clean = re.sub(r'[^\w]', '', text).lower()
    return clean not in noise_keywords and re.search(r'[a-zA-Z]', text)

def assign_categories(rows):
    categorized_rows = []
    current_category = "Uncategorized"
    for row in rows:
        texts = [box["text"] for box in row]
        full_line = " ".join(texts).strip()
        if detect_price(full_line):
            for box in row:
                box["category"] = current_category
            categorized_rows.append(row)
            continue
        words = full_line.split()
        uppercase_words = sum(1 for w in words if w.isupper() or w.istitle())
        if uppercase_words >= max(1, len(words) // 2) and len(full_line) <= 35 and len(words) <= 4:
            current_category = full_line
            continue
        for box in row:
            box["category"] = current_category
        categorized_rows.append(row)
    return categorized_rows

def parse_rows_to_menu(categorized_rows, image_name="unknown"):
    menu = []
    last_entry = None
    for row in categorized_rows:
        row.sort(key=lambda b: b["x"])
        line = " ".join([b["text"] for b in row])
        cat = row[0].get("category", "Uncategorized")
        price_matches = list(re.finditer(r'(₹|Rs\.?)?\s?\d{1,5}([.,]\d{1,2})?', line))
        if price_matches:
            items, prices = [], []
            start = 0
            for match in price_matches:
                price = match.group().strip()
                item_chunk = line[start:match.start()].strip(" -–—|,")
                item_texts = re.split(r'\s{2,}|,|/| - | \| |\. ', item_chunk)
                for it in item_texts:
                    it = re.sub(r'\(.*?\)', '', it).strip()
                    if is_valid_item(it):
                        items.append(it)
                        prices.append(price)
                start = match.end()
            for i in range(min(len(items), len(prices))):
                entry = {"image": image_name, "category": cat, "item": items[i], "price": prices[i], "description": ""}
                last_entry = entry
                menu.append(entry)
        elif last_entry and cat == last_entry["category"]:
            last_entry["description"] += " " + line
    return menu

def insert_into_mysql(data, config, vendor_id):
    try:
        conn = mysql.connect(**config)
        cursor = conn.cursor()
        query = """
        INSERT INTO menu_or_services (category, item_or_service, price, description, vendor_id, image_path)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        for entry in data:
            cursor.execute(query, (
                entry["category"], entry["item"], entry["price"],
                entry["description"], vendor_id, entry["image"]
            ))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except mysql.Error as err:
        print("MySQL Error:", err)
        return False

@app.route("/upload", methods=["POST"])
def upload():
    if "image" not in request.files or "vendor_id" not in request.form:
        return jsonify({"error": "Image file and vendor_id required"}), 400

    file = request.files["image"]
    vendor_id = int(request.form["vendor_id"])

    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        file.save(tmp.name)
        boxes = extract_boxes(tmp.name)
        rows = group_by_rows(boxes)
        final_rows = assign_categories(rows)
        menu_data = parse_rows_to_menu(final_rows, image_name=file.filename)

    if not menu_data:
        return jsonify({"message": "No menu items found"}), 204

    inserted = insert_into_mysql(menu_data, mysql_config, vendor_id)

    os.unlink(tmp.name)
    return jsonify({"success": inserted, "items": menu_data if inserted else []})

if __name__ == "__main__":
    app.run(debug=True)
