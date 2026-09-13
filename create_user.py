import sqlite3
from datetime import datetime
from pathlib import Path
from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "water_billing.sqlite3"


def create_user(username, password, role="Collector", wallet_id=None):
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        password_hash = generate_password_hash(password)
        now = datetime.now().isoformat()
        cursor.execute("""
            INSERT INTO users (username, password_hash, role, wallet_id, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (username, password_hash, role, wallet_id, 1, now, now))
        conn.commit()
        print(f"✅ تم إنشاء المستخدم: {username}")
    except sqlite3.IntegrityError:
        print("❌ المستخدم موجود مسبقاً!")
    except Exception as e:
        print("❌ خطأ:", e)
    finally:
        conn.close()


if __name__ == "__main__":
    print("=== إضافة مستخدم جديد ===")
    username = input("اسم المستخدم: ")
    password = input("كلمة المرور: ")
    role = input("الصلاحية (Admin / Staff / Collector / Technician): ") or "Collector"
    wallet_id = input("رقم الصندوق (اختياري - Enter للتخطي): ")
    wallet_id = int(wallet_id) if wallet_id.strip() else None
    create_user(username, password, role, wallet_id)
