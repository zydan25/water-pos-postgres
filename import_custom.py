import sqlite3
import openpyxl
import datetime

from pathlib import Path
from database import post_invoice_accounting
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'instance' / 'water_billing.sqlite3'
db_path = str(DB_PATH)
excel_path = str(next((Path(p) for p in ['كشوفات 2026.xlsx'] if Path(p).exists()), Path('كشوفات 2026.xlsx')))

def clean_val(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int, float)):
        return str(v)
    v = str(v).strip()
    if v.startswith('#'): return ""
    return v

def clean_num(v):
    if v is None: return 0
    try:
        if isinstance(v, str):
            v = v.replace(',', '')
        return float(v)
    except:
        return 0

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
db = conn.cursor()

wb = openpyxl.load_workbook(excel_path, data_only=True)
ws = wb.active

count_subs = 0
count_invs = 0
count_updated_subs = 0
now = datetime.datetime.now().isoformat()
today = datetime.date.today().isoformat()

for i, row in enumerate(ws.iter_rows(min_row=6, values_only=True)):
    account_number = clean_val(row[4])
    name = clean_val(row[8])
    if not account_number or not name:
        continue
    
    unit_price = clean_num(row[1])
    sub_fee = clean_num(row[2])
    meter_number = clean_val(row[7])
    address = f"{clean_val(row[5])} - {clean_val(row[6])}".strip(" -")
    
    # Insert or update subscriber
    existing_sub = db.execute("SELECT id FROM subscribers WHERE account_number = ?", (account_number,)).fetchone()
    if existing_sub:
        sub_id = existing_sub["id"]
        db.execute('''UPDATE subscribers 
                      SET name=?, meter_number=?, address=?, default_unit_price=?, default_subscription_fee=?, updated_at=?
                      WHERE id=?''', 
                      (name, meter_number, address, unit_price, sub_fee, now, sub_id))
        count_updated_subs += 1
    else:
        db.execute('''INSERT INTO subscribers (account_number, name, meter_number, address, default_unit_price, default_subscription_fee, active, created_at, updated_at)
                      VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)''',
                      (account_number, name, meter_number, address, unit_price, sub_fee, now, now))
        sub_id = db.lastrowid
        count_subs += 1

    # Insert invoice
    invoice_no = clean_val(row[3])
    if not invoice_no:
        continue
        
    existing_inv = db.execute("SELECT id FROM invoices WHERE invoice_no = ?", (invoice_no,)).fetchone()
    if existing_inv:
        continue
        
    prev_reading = clean_num(row[9])
    curr_reading = clean_num(row[10])
    consumption = clean_num(row[11])
    consumption_amount = clean_num(row[12])
    opening_balance = clean_num(row[13])
    total_amount = clean_num(row[14])
    paid_amount = clean_num(row[16])
    month_label = clean_val(row[19]) or today[:7]
    notes = clean_val(row[18])
    
    remaining_amount = total_amount - paid_amount
    if remaining_amount < 0:
        remaining_amount = 0

    db.execute('''INSERT INTO invoices (
                    invoice_no, subscriber_id, invoice_date, month_label, 
                    previous_reading, current_reading, consumption, unit_price, 
                    consumption_amount, subscription_fee, opening_balance, 
                    total_amount, paid_amount, remaining_amount, notes, 
                    created_at, updated_at, created_by
                  ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)''',
                  (invoice_no, sub_id, today, month_label,
                   prev_reading, curr_reading, consumption, unit_price,
                   consumption_amount, sub_fee, opening_balance,
                   total_amount, paid_amount, remaining_amount, notes,
                   now, now))
    invoice_row = db.execute("SELECT last_insert_rowid() AS id").fetchone()
    if invoice_row:
        try:
            post_invoice_accounting(conn, invoice_row["id"], 1, reverse_existing=False)
        except Exception as exc:
            print(f"Accounting post skipped for invoice {invoice_no}: {exc}")
    count_invs += 1

conn.commit()
conn.close()

print(f"Import Summary:\n- New Subscribers: {count_subs}\n- Updated Subscribers: {count_updated_subs}\n- New Invoices: {count_invs}")
