#!/usr/bin/env python3
import requests
import json
import time
import base64
import os
import subprocess
import logging
from datetime import datetime, timedelta
import threading
import http.server
import socketserver
import sqlite3
import html
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
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
DB_FILE = os.getenv("DATA_DB", os.path.join(os.path.dirname(__file__), "data.db"))
ENABLE_HTTP_SERVER = os.getenv("ENABLE_HTTP_SERVER", "1").lower() in ("1", "true", "yes")
LOG_FILE = os.getenv("LOG_FILE", os.path.join(os.path.dirname(__file__), "agent.log"))
HTTP_PORT = int(os.getenv("HTTP_PORT", "8082"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

HEADERS = {
    "X-BLToken": API_TOKEN,
    "Content-Type": "application/x-www-form-urlencoded"
}

last_order_data = {}

def validate_env():
    """Ensure mandatory environment variables are present."""
    required = {
        "API_TOKEN": API_TOKEN,
        "PAGE_ACCESS_TOKEN": PAGE_ACCESS_TOKEN,
        "RECIPIENT_ID": RECIPIENT_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        logger.error(
            "Brak wymaganych zmiennych środowiskowych: %s",
            ", ".join(missing),
        )
        raise SystemExit(1)

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
                        logger.error(f"Błąd migracji z {LABEL_QUEUE}: {e}")
            conn.commit()
    conn.close()

def ensure_db_init():
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
        response = requests.post(
            BASE_URL, headers=HEADERS, data=payload, timeout=10
        )
        logger.info(f"[{method}] {response.status_code}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error in call_api({method}): {e}")
    except Exception as e:
        logger.error(f"Błąd w call_api({method}): {e}")
    return {}

def get_orders():
    response = call_api("getOrders", {
        "status_id": STATUS_ID
    })
    logger.info(
        "🔁 Surowa odpowiedź:\n%s",
        json.dumps(response, indent=2, ensure_ascii=False),
    )
    orders = response.get("orders", [])
    logger.info(f"🔍 Zamówień znalezionych: {len(orders)}")
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
        result = subprocess.run(
            ["lp", "-d", PRINTER_NAME, file_path], capture_output=True
        )
        os.remove(file_path)
        if result.returncode != 0:
            logger.error(
                "Błąd drukowania (kod %s): %s",
                result.returncode,
                result.stderr.decode().strip(),
            )
        else:
            logger.info(f"📨 Etykieta wydrukowana dla zamówienia {order_id}")
    except Exception as e:
        logger.error(f"Błąd drukowania: {e}")

def print_test_page():
    try:
        file_path = "/tmp/print_test.txt"
        with open(file_path, "w") as f:
            f.write("=== TEST PRINT ===\n")
        result = subprocess.run([
            "lp",
            "-d",
            PRINTER_NAME,
            file_path,
        ], capture_output=True)
        os.remove(file_path)
        if result.returncode != 0:
            logger.error(
                "Błąd testowego druku (kod %s): %s",
                result.returncode,
                result.stderr.decode().strip(),
            )
            return False
        logger.info("🔧 Testowa strona została wysłana do drukarki.")
        return True
    except Exception as e:
        logger.error(f"Błąd testowego druku: {e}")
        return False

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
        logger.info(
            "📬 Messenger response: %s %s", response.status_code, response.text
        )
        response.raise_for_status()
        logger.info("✅ Wiadomość została wysłana przez Messengera.")
    except Exception as e:
        logger.error(f"Błąd wysyłania wiadomości: {e}")

def is_quiet_time():
    now = datetime.now().hour
    if QUIET_HOURS_START < QUIET_HOURS_END:
        return QUIET_HOURS_START <= now < QUIET_HOURS_END
    else:
        return now >= QUIET_HOURS_START or now < QUIET_HOURS_END


def render_page(title, body_html):
    """Return a full HTML document with basic styling and navigation."""
    nav_links = [
        ("/", "Strona główna"),
        ("/history", "Historia drukowania"),
        ("/logs", "Logi"),
        ("/testprint", "Testuj drukarkę"),
    ]
    if last_order_data:
        nav_links.append(("/test", "Wyślij testową wiadomość"))

    nav_html = "<nav><ul>" + "".join(
        f"<li><a href='{href}'>{text}</a></li>" for href, text in nav_links
    ) + "</ul></nav>"

    style = """
    body { font-family: Arial, sans-serif; background: #f7f7f7; color: #222; margin: 20px; }
    nav ul { list-style: none; padding: 0; margin-bottom: 20px; }
    nav li { display: inline-block; margin-right: 15px; }
    table { border-collapse: collapse; width: 100%; background: #fff; }
    th, td { border: 1px solid #ccc; padding: 4px 8px; text-align: left; }
    h1 { margin-top: 0; }
    """

    return (
        "<!DOCTYPE html>"
        "<html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        f"<style>{style}</style>"
        "</head><body>"
        f"{nav_html}<h1>{title}</h1>" + body_html + "</body></html>"
    )

class AgentRequestHandler(http.server.BaseHTTPRequestHandler):
    def _send(self, content, status=200, content_type="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.wfile.write(content)

    def do_GET(self):
        if self.path == "/test":
            if last_order_data:
                send_messenger_message(last_order_data)
                body = "<p>✅ Testowa wiadomość została wysłana.</p>"
            else:
                body = "<p>⚠️ Brak danych ostatniego zamówienia.</p>"
            self._send(render_page("Test wiadomości", body))
        elif self.path == "/testprint":
            if print_test_page():
                body = "<p>✅ Testowy wydruk wysłany.</p>"
            else:
                body = "<p>❌ Błąd testowego wydruku.</p>"
            self._send(render_page("Test wydruku", body))
        elif self.path == "/history":
            printed = load_printed_orders()
            queue = load_queue()
            rows = "".join(
                f"<tr><td>{html.escape(str(oid))}</td><td>{html.escape(str(ts))}</td></tr>"
                for oid, ts in sorted(printed.items())
            )
            qrows = "".join(
                f"<tr><td>{html.escape(str(item.get('order_id')))}</td><td>W kolejce</td></tr>"
                for item in queue
            )
            table_html = (
                "<table><tr><th>ID zamówienia</th><th>Czas</th></tr>" + rows + qrows + "</table>"
            )
            self._send(render_page("Historia drukowania", table_html))
        elif self.path == "/logs":
            try:
                with open(LOG_FILE, "r") as f:
                    lines = f.readlines()[-200:]
                log_html = "<pre>" + (
                    "".join(lines)
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                ) + "</pre>"
            except Exception as e:
                log_html = f"<p>Błąd czytania logów: {e}</p>"
            self._send(render_page("Logi", log_html))
        elif self.path == "/":
            body = "<p>Wybierz opcję z menu powyżej.</p>"
            self._send(render_page("BaseLinker Print Agent", body))
        else:
            self.send_error(404, "Nie znaleziono strony")

    def log_message(self, format, *args):
        return

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            body = "<p>Nie znaleziono strony.</p>"
            self._send(render_page("404 - Not Found", body), status=404)
        else:
            super().send_error(code, message, explain)

def start_http_server():
    with socketserver.TCPServer(("", HTTP_PORT), AgentRequestHandler) as httpd:
        logger.info(
            f"[HTTP] Serwer UI dostępny na porcie {HTTP_PORT}"
        )
        httpd.serve_forever()

if __name__ == "__main__":
    logger.info(
        "[START] Agent BaseLinker z automatycznym getLabel + Messenger + dotenv"
    )
    validate_env()
    ensure_db_init()
    if ENABLE_HTTP_SERVER:
        threading.Thread(target=start_http_server, daemon=True).start()

    while True:
        clean_old_printed_orders()
        printed = load_printed_orders()
        queue = load_queue()

        if not is_quiet_time():
            grouped = {}
            for item in queue:
                grouped.setdefault(item["order_id"], []).append(item)

            new_queue = []
            for oid, items in grouped.items():
                try:
                    for it in items:
                        print_label(it["label_data"], it.get("ext", "pdf"), it["order_id"])
                    mark_as_printed(oid)
                    printed[oid] = datetime.now()
                except Exception as e:
                    logger.error(f"Błąd przetwarzania z kolejki: {e}")
                    new_queue.extend(items)

            queue = new_queue
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

                logger.info(
                    f"📜 Zamówienie {order_id} ({last_order_data['name']})"
                )
                packages = get_order_packages(order_id)
                labels = []

                for p in packages:
                    package_id = p.get("package_id")
                    courier_code = p.get("courier_code")
                    if not package_id or not courier_code:
                        logger.warning(
                            "  Brak danych: package_id lub courier_code"
                        )
                        continue

                    logger.info(f"  📦 Paczka {package_id} (kurier: {courier_code})")

                    label_data, ext = get_label(courier_code, package_id)
                    if label_data:
                        labels.append((label_data, ext))
                    else:
                        logger.warning("  ❌ Brak etykiety (label_data = null)")

                if labels:
                    if is_quiet_time():
                        logger.info(
                            "🕒 Cisza nocna — etykiety nie zostaną wydrukowane teraz."
                        )
                        for label_data, ext in labels:
                            queue.append({
                                "order_id": order_id,
                                "label_data": label_data,
                                "ext": ext,
                                "last_order_data": last_order_data,
                            })
                        send_messenger_message(last_order_data)
                        mark_as_printed(order_id)
                        printed[order_id] = datetime.now()
                    else:
                        for label_data, ext in labels:
                            print_label(label_data, ext, order_id)
                        send_messenger_message(last_order_data)
                        mark_as_printed(order_id)
                        printed[order_id] = datetime.now()

        except Exception as e:
            logger.error(f"[BŁĄD GŁÓWNY] {e}")

        save_queue(queue)
        time.sleep(POLL_INTERVAL)
