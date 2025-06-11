#!/usr/bin/env python3
import requests
import json
import time
import base64
import os
import subprocess
from datetime import datetime, timedelta
import threading
import http.server
import socketserver
import sqlite3
from dotenv import load_dotenv

# === WCZYTAJ Z .env ===
load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
RECIPIENT_ID = os.getenv("RECIPIENT_ID")
STATUS_ID = int(os.getenv("STATUS_ID", "91618"))
PRINTER_NAME = os.getenv("PRINTER_NAME", "Xprinter")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
QUIET_HOURS_START = int(os.getenv("QUIET_HOURS_START", "10"))
QUIET_HOURS_END = int(os.getenv("QUIET_HOURS_END", "22"))
BASE_URL = "https://api.baselinker.com/connector.php"
PRINTED_FILE = os.path.join(os.path.dirname(__file__), "printed_orders.txt")
PRINTED_EXPIRY_DAYS = int(os.getenv("PRINTED_EXPIRY_DAYS", "5"))
LABEL_QUEUE = os.path.join(os.path.dirname(__file__), "queued_labels.jsonl")
DB_FILE = os.getenv("DATA_DB", os.path.join(os.path.dirname(__file__), "data.db"))

HEADERS = {
    "X-BLToken": API_TOKEN,
    "Content-Type": "application/x-www-form-urlencoded"
}

last_order_data = {}

def ensure_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS printed_orders(order_id TEXT PRIMARY KEY, printed_at TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS label_queue(order_id TEXT, label_data TEXT, ext TEXT, last_order_data TEXT)"
    )
    conn.commit()

    # migrate old printed orders
    if os.path.exists(PRINTED_FILE):
        cur.execute("SELECT COUNT(*) FROM printed_orders")
        if cur.fetchone()[0] == 0:
            with open(PRINTED_FILE, "r") as f:
                for line in f:
                    if "," in line:
                        oid, ts = line.strip().split(",")
                        cur.execute(
                            "INSERT OR IGNORE INTO printed_orders(order_id, printed_at) VALUES (?, ?)",
                            (oid, ts),
                        )
            conn.commit()
    if os.path.exists(LABEL_QUEUE):
        cur.execute("SELECT COUNT(*) FROM label_queue")
        if cur.fetchone()[0] == 0:
            with open(LABEL_QUEUE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        cur.execute(
                            "INSERT INTO label_queue(order_id, label_data, ext, last_order_data) VALUES (?, ?, ?, ?)",
                            (
                                item.get("order_id"),
                                item.get("label_data"),
                                item.get("ext"),
                                json.dumps(item.get("last_order_data", {})),
                            ),
                        )
                    except Exception as e:
                        print(f"❌ Błąd migracji z {LABEL_QUEUE}: {e}")
            conn.commit()
    conn.close()

def ensure_printed_file():
    ensure_db()

def load_printed_orders():
    ensure_db()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT order_id, printed_at FROM printed_orders")
    rows = cur.fetchall()
    conn.close()
    orders = {oid: datetime.fromisoformat(ts) for oid, ts in rows}
    return orders

def mark_as_printed(order_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO printed_orders(order_id, printed_at) VALUES (?, ?)",
        (order_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()

def clean_old_printed_orders():
    threshold = datetime.now() - timedelta(days=PRINTED_EXPIRY_DAYS)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM printed_orders WHERE printed_at < ?", (threshold.isoformat(),))
    conn.commit()
    conn.close()

def ensure_queue_file():
    ensure_db()

def load_queue():
    ensure_db()
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT order_id, label_data, ext, last_order_data FROM label_queue")
    rows = cur.fetchall()
    conn.close()
    items = []
    for order_id, label_data, ext, last_order_json in rows:
        try:
            last_data = json.loads(last_order_json) if last_order_json else {}
        except Exception:
            last_data = {}
        items.append({
            "order_id": order_id,
            "label_data": label_data,
            "ext": ext,
            "last_order_data": last_data,
        })
    return items

def save_queue(items):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM label_queue")
    for item in items:
        cur.execute(
            "INSERT INTO label_queue(order_id, label_data, ext, last_order_data) VALUES (?, ?, ?, ?)",
            (
                item.get("order_id"),
                item.get("label_data"),
                item.get("ext"),
                json.dumps(item.get("last_order_data", {})),
            ),
        )
    conn.commit()
    conn.close()

def call_api(method, parameters={}):
    try:
        payload = {
            "method": method,
            "parameters": json.dumps(parameters)
        }
        response = requests.post(BASE_URL, headers=HEADERS, data=payload)
        print(f"[{method}] {response.status_code}")
        return response.json()
    except Exception as e:
        print(f"❌ Błąd w call_api({method}): {e}")
        return {}

def get_orders():
    response = call_api("getOrders", {
        "status_id": STATUS_ID
    })
    print(f"🔁 Surowa odpowiedź:\n{json.dumps(response, indent=2, ensure_ascii=False)}")
    orders = response.get("orders", [])
    print(f"🔍 Zamówień znalezionych: {len(orders)}")
    return orders

def get_order_packages(order_id):
    response = call_api("getOrderPackages", {
        "order_id": order_id
    })
    return response.get("packages", [])

def get_label(courier_code, package_id):
    response = call_api("getLabel", {
        "courier_code": courier_code,
        "package_id": package_id
    })
    return response.get("label"), response.get("extension", "pdf")

def print_label(base64_data, extension, order_id):
    try:
        file_path = f"/tmp/label_{order_id}.{extension}"
        pdf_data = base64.b64decode(base64_data)
        with open(file_path, "wb") as f:
            f.write(pdf_data)
        subprocess.run(["lp", "-d", PRINTER_NAME, file_path])
        os.remove(file_path)
        print(f"📨 Etykieta wydrukowana dla zamówienia {order_id}")
    except Exception as e:
        print(f"❌ Błąd drukowania: {e}")

def shorten_product_name(full_name):
    words = full_name.strip().split()
    if len(words) >= 3:
        return f"{words[0]} {' '.join(words[-2:])}"
    return full_name

def send_messenger_message(data):
    try:
        message = (
            f"📦 Nowe zamówienie od: {data.get('name', '-')}\n"
            f"🛒 Produkty:\n" +
            ''.join(f"- {shorten_product_name(p['name'])} (x{p['quantity']})\n" for p in data.get("products", [])) +
            f"🚚 Wysyłka: {data.get('shipping', '-')}\n"
            f"🌐 Platforma: {data.get('platform', '-')}\n"
            f"📎 ID: {data.get('order_id', '-')}")

        response = requests.post(
            "https://graph.facebook.com/v17.0/me/messages",
            headers={
                "Authorization": f"Bearer {PAGE_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            },
            data=json.dumps({
                "recipient": {"id": RECIPIENT_ID},
                "message": {"text": message}
            })
        )
        print(f"📬 Messenger response: {response.status_code} {response.text}")
        response.raise_for_status()
        print("✅ Wiadomość została wysłana przez Messengera.")
    except Exception as e:
        print(f"❌ Błąd wysyłania wiadomości: {e}")

def is_quiet_time():
    now = datetime.now().hour
    if QUIET_HOURS_START < QUIET_HOURS_END:
        return QUIET_HOURS_START <= now < QUIET_HOURS_END
    else:
        return now >= QUIET_HOURS_START or now < QUIET_HOURS_END

class TestRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/test" and last_order_data else 404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

        if self.path == "/test":
            if last_order_data:
                send_messenger_message(last_order_data)
                self.wfile.write("✅ Testowa wiadomość została wysłana.".encode("utf-8"))
            else:
                self.wfile.write("⚠️ Brak danych ostatniego zamówienia.".encode("utf-8"))
        else:
            self.wfile.write("❌ Nieznany endpoint.".encode("utf-8"))

    def log_message(self, format, *args):
        return

def start_http_server():
    PORT = 8082
    with socketserver.TCPServer(("", PORT), TestRequestHandler) as httpd:
        print(f"[HTTP] Endpoint testowy dostępny na porcie {PORT} — GET /test")
        httpd.serve_forever()

if __name__ == "__main__":
    print("[START] Agent BaseLinker z automatycznym getLabel + Messenger + dotenv")
    ensure_printed_file()
    threading.Thread(target=start_http_server, daemon=True).start()

    while True:
        clean_old_printed_orders()
        printed = load_printed_orders()
        queue = load_queue()

        if not is_quiet_time():
            for item in queue[:]:
                try:
                    print_label(item["label_data"], item.get("ext", "pdf"), item["order_id"])
                    mark_as_printed(item["order_id"])
                    send_messenger_message(item.get("last_order_data", {}))
                except Exception as e:
                    print(f"❌ Błąd przetwarzania z kolejki: {e}")
                    continue
                queue.remove(item)
            save_queue(queue)

        try:
            orders = get_orders()
            for order in orders:
                order_id = str(order["order_id"])

                last_order_data = {
                    "order_id": order_id,
                    "name": order.get("delivery_fullname", "Nieznany klient"),
                    "platform": order.get("order_source", "brak"),
                    "shipping": order.get("delivery_method", "brak"),
                    "products": order.get("products", [])
                }

                if order_id in printed:
                    continue

                print(f"📜 Zamówienie {order_id} ({last_order_data['name']})")
                packages = get_order_packages(order_id)

                for p in packages:
                    package_id = p.get("package_id")
                    courier_code = p.get("courier_code")
                    if not package_id or not courier_code:
                        print("  ⚠️ Brak danych: package_id lub courier_code")
                        continue

                    print(f"  📦 Paczka {package_id} (kurier: {courier_code})")

                    label_data, ext = get_label(courier_code, package_id)
                    if label_data:
                        if is_quiet_time():
                            print("🕒 Cisza nocna — etykieta nie zostanie wydrukowana teraz.")
                            queue.append({
                                "order_id": order_id,
                                "label_data": label_data,
                                "ext": ext,
                                "last_order_data": last_order_data,
                            })
                        else:
                            print_label(label_data, ext, order_id)
                            mark_as_printed(order_id)
                            send_messenger_message(last_order_data)
                    else:
                        print("  ❌ Brak etykiety (label_data = null)")

        except Exception as e:
            print(f"[BŁĄD GŁÓWNY] {e}")

        save_queue(queue)
        time.sleep(POLL_INTERVAL)
