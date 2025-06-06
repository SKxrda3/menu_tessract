import os
import re
import pandas as pd
from tabulate import tabulate
import mysql.connector as mysql
from dotenv import load_dotenv
import pytesseract
from PIL import Image

load_dotenv()

mysql_config = {
    "host": os.getenv("MYSQL_HOST"),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE")
}

# Agar Windows pe hain, to yahan Tesseract.exe path set karna zaroori ho sakta hai:
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

def extract_boxes(image_path, conf_threshold=0.6):
    """
    Tesseract se text aur bounding boxes extract karna.
    Returns list of dicts with keys: text, conf, box (4 points), x, y (center)
    """
    img = Image.open(image_path)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

    boxes = []
    n_boxes = len(data['level'])
    for i in range(n_boxes):
        conf = float(data['conf'][i])
        text = data['text'][i].strip()
        if conf > conf_threshold * 100 and text != '':
            # Bounding box points: left, top, width, height
            x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
            box = [
                [x, y],
                [x + w, y],
                [x + w, y + h],
                [x, y + h]
            ]
            x_center = x + w / 2
            y_center = y + h / 2

            boxes.append({
                'text': text,
                'conf': conf / 100,  # normalize to 0-1
                'box': box,
                'x': x_center,
                'y': y_center
            })

    return boxes


def group_by_rows(boxes, y_thresh=15):
    boxes.sort(key=lambda b: b["y"])
    rows = []
    current_row = []
    last_y = -1000

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
        is_probable_category = (
            uppercase_words >= max(1, len(words) // 2) and
            len(full_line) <= 35 and
            len(words) <= 4
        )

        if is_probable_category:
            current_category = full_line
            continue

        for box in row:
            box["category"] = current_category
        categorized_rows.append(row)

    return categorized_rows


def detect_price(text):
    return re.search(r'(\u20B9|Rs\.?)?\s?\d{1,4}([.,]\d{1,2})?', text)


def is_valid_item(text):
    if not text or len(text.strip()) <= 2:
        return False

    if text.strip().isupper() and len(text.strip()) <= 3:
        return False

    noise_keywords = { 'am', 'pm', 'yo', 'l', 't', 'a', 'b', '/', '-', '|', ':', '.', ',', '–', '—', '_', '(', ')',
                       'daily', 'only', 'each', 'per', 'day', 'week', 'month', 'with', 'served', 'includes',
                       'available', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'timings', 'timing',
                       'from', 'at', 'till', 'until', 'for', 'special', 'offer', 'extra', 'add-on',
                       'optional', 'combo', 'set', 'option', 'mrp', 'gst', 'inclusive', 'exclusive',
                       'taxes', 'inc.', 'excl.', 'incl.' }

    clean_text = re.sub(r'[^\w]', '', text).lower()
    if clean_text in noise_keywords:
        return False

    if not re.search(r'[a-zA-Z]', text):
        return False

    return True


def parse_rows_to_menu(categorized_rows, image_name="unknown"):
    menu = []
    last_item_entry = None

    for row in categorized_rows:
        row.sort(key=lambda b: b["x"])
        full_line = " ".join([b["text"] for b in row]).strip()
        current_category = row[0].get("category", "Uncategorized")

        price_matches = list(re.finditer(r'(₹|Rs\.?)?\s?\d{1,5}([.,]\d{1,2})?', full_line))

        if price_matches:
            items = []
            prices = []
            start = 0

            for match in price_matches:
                price_text = match.group().strip()
                price_start = match.start()
                item_chunk = full_line[start:price_start].strip(" -–—|,")
                item_texts = re.split(r'\s{2,}|,|/| - | \| |\. ', item_chunk)

                for item_text in item_texts:
                    item_text = re.sub(r'\(.*?\)', '', item_text).strip()
                    if not is_valid_item(item_text):
                        continue
                    items.append(item_text)
                    prices.append(price_text)

                start = match.end()

            for i in range(min(len(items), len(prices))):
                entry = {
                    "image": image_name,
                    "category": current_category,
                    "item": items[i],
                    "price": prices[i],
                    "description": ""
                }
                last_item_entry = entry
                menu.append(entry)

        elif last_item_entry and current_category == last_item_entry["category"]:
            last_item_entry["description"] += " " + full_line

    return menu


def insert_into_mysql(data, host, user, password, database, vender_id):
    connection = None
    cursor = None
    try:
        connection = mysql.connect(
            host=host,
            user=user,
            password=password,
            database=database,
            port=3306,
            use_pure=True
        )
        cursor = connection.cursor()

        insert_query = """
        INSERT INTO menu_or_services (category, item_or_service, price, description, vendor_id, image_path)
        VALUES (%s, %s, %s, %s, %s, %s)
        """

        for entry in data:
            cursor.execute(insert_query, (
                entry["category"],
                entry["item"],
                entry["price"],
                entry["description"],
                vender_id,
                entry["image"]
            ))

        connection.commit()
        print("\n✅ Extracted menu data inserted into MySQL database.")

    except mysql.Error as err:
        print(f"❌ MySQL error: {err}")

    finally:
        if connection and connection.is_connected():
            if cursor:
                cursor.close()
            connection.close()


def process_folder(folder_path, mysql_config, vender_id):
    all_data = []

    for filename in os.listdir(folder_path):
        if filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".tiff")):
            image_path = os.path.join(folder_path, filename)
            print(f"Processing {filename} ...")

            boxes = extract_boxes(image_path)
            rows = group_by_rows(boxes)
            final_data = assign_categories(rows)
            menu = parse_rows_to_menu(final_data, image_name=filename)

            ignore_phrases = ["preparation time", "serving size", "cooking time", "calories"]

            for entry in menu:
                combined_text = (entry["item"] + " " + entry["category"] + " " + entry.get("description", "")).lower()
                if any(phrase in combined_text for phrase in ignore_phrases):
                    continue
                all_data.append(entry)

    # if all_data:
    #     insert_into_mysql(all_data, vender_id=vender_id, **mysql_config)
    # else:
    #     print("No menu data extracted from images.")

    if all_data:
        print("\n📋 Parsed Menu Data:\n")
        print(tabulate(all_data, headers="keys", tablefmt="grid"))
        insert_into_mysql(all_data, vender_id=vender_id, **mysql_config)
    else:
        print("No menu data extracted from images.")


if __name__ == "__main__":
    folder_path = "menu1"  # Your image folder path
    vender_id = 1         # Replace with actual vender_id
    process_folder(folder_path, mysql_config, vender_id)
