from __future__ import annotations

from datetime import date, datetime

from database import get_db
from .models import ensure_schema


def now_sql():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def save_unit(data, unit_id=None):
    ensure_schema()
    db = get_db()
    code = (data.get("code") or "").strip() or f"UNIT-{int(datetime.utcnow().timestamp())}"
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("اسم الوحدة مطلوب.")
    active = 1 if str(data.get("active", "1")) not in {"0", "false", "False"} else 0
    sort_order = to_int(data.get("sort_order"), 0) or 0
    notes = (data.get("notes") or "").strip()
    if unit_id:
        db.execute("UPDATE water_units SET code=?, name=?, active=?, sort_order=?, notes=?, updated_at=? WHERE id=?", (code,name,active,sort_order,notes,now_sql(),unit_id))
    else:
        db.execute("INSERT INTO water_units(code,name,active,sort_order,notes,created_at) VALUES (?,?,?,?,?,?)", (code,name,active,sort_order,notes,now_sql()))
    db.commit()


def save_location(data, location_id=None):
    ensure_schema()
    db = get_db()
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("اسم الموقع مطلوب.")
    unit_id = to_int(data.get("unit_id"))
    parent_id = to_int(data.get("parent_id"))
    if location_id and parent_id == location_id:
        raise ValueError("لا يمكن جعل الموقع أبًا لنفسه.")
    location_type = (data.get("location_type") or "منطقة").strip()
    code = (data.get("code") or "").strip() or None
    if not code:
        code = f"LOC-{int(datetime.utcnow().timestamp())}"
        suffix = 1
        while db.execute("SELECT 1 FROM water_locations WHERE code=? LIMIT 1", (code,)).fetchone():
            suffix += 1
            code = f"LOC-{int(datetime.utcnow().timestamp())}-{suffix}"
    sort_order = to_int(data.get("sort_order"), 0) or 0
    notes = (data.get("notes") or "").strip()
    active = 1 if str(data.get("active", "1")) not in {"0","false","False"} else 0
    if parent_id:
        cursor = parent_id
        guard = set()
        while cursor and cursor not in guard:
            guard.add(cursor)
            if cursor == location_id:
                raise ValueError("لا يمكن نقل الموقع إلى أحد فروعه.")
            row = db.execute("SELECT parent_id FROM water_locations WHERE id=?", (cursor,)).fetchone()
            cursor = row["parent_id"] if row else None
    if location_id:
        db.execute("UPDATE water_locations SET unit_id=?,parent_id=?,code=?,name=?,location_type=?,active=?,sort_order=?,notes=?,updated_at=? WHERE id=?", (unit_id,parent_id,code,name,location_type,active,sort_order,notes,now_sql(),location_id))
    else:
        db.execute("INSERT INTO water_locations(unit_id,parent_id,code,name,location_type,active,sort_order,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (unit_id,parent_id,code,name,location_type,active,sort_order,notes,now_sql()))
    db.commit()


def delete_location(location_id):
    ensure_schema()
    db = get_db()
    if db.execute("SELECT COUNT(*) c FROM water_locations WHERE parent_id=?", (location_id,)).fetchone()["c"]:
        raise ValueError("لا يمكن حذف موقع له فروع. انقل الفروع أولاً.")
    if db.execute("SELECT COUNT(*) c FROM subscribers WHERE location_id=?", (location_id,)).fetchone()["c"]:
        raise ValueError("لا يمكن حذف موقع مرتبط بمشتركين.")
    if db.execute("SELECT COUNT(*) c FROM network_meters WHERE location_id=?", (location_id,)).fetchone()["c"]:
        raise ValueError("لا يمكن حذف موقع مرتبط بعدادات شبكة.")
    db.execute("DELETE FROM water_locations WHERE id=?", (location_id,))
    db.commit()


def save_meter(data, meter_id=None):
    ensure_schema()
    db = get_db()
    name = (data.get("name") or "").strip()
    meter_number = (data.get("meter_number") or "").strip()
    if not name or not meter_number:
        raise ValueError("اسم العداد ورقمه مطلوبان.")
    meter_type = (data.get("meter_type") or "main").strip()
    unit_id = to_int(data.get("unit_id"))
    location_id = to_int(data.get("location_id"))
    parent_meter_id = to_int(data.get("parent_meter_id"))
    initial_reading = to_float(data.get("initial_reading"), 0)
    installed_at = (data.get("installed_at") or "").strip() or None
    status = (data.get("status") or "active").strip()
    notes = (data.get("notes") or "").strip()
    if meter_id and parent_meter_id == meter_id:
        raise ValueError("لا يمكن ربط العداد بنفسه.")
    if parent_meter_id:
        cursor = parent_meter_id
        guard = set()
        while cursor and cursor not in guard:
            guard.add(cursor)
            if cursor == meter_id:
                raise ValueError("لا يمكن ربط العداد بأحد فروعه.")
            row = db.execute("SELECT parent_meter_id FROM network_meters WHERE id=?", (cursor,)).fetchone()
            cursor = row["parent_meter_id"] if row else None
    existing = db.execute("SELECT id FROM network_meters WHERE meter_number=? AND id<>COALESCE(?,0)", (meter_number,meter_id)).fetchone()
    if existing:
        raise ValueError("رقم العداد مستخدم مسبقًا.")
    if meter_id:
        db.execute("UPDATE network_meters SET name=?,meter_number=?,meter_type=?,unit_id=?,location_id=?,parent_meter_id=?,initial_reading=?,installed_at=?,status=?,notes=?,updated_at=? WHERE id=?", (name,meter_number,meter_type,unit_id,location_id,parent_meter_id,initial_reading,installed_at,status,notes,now_sql(),meter_id))
    else:
        db.execute("INSERT INTO network_meters(name,meter_number,meter_type,unit_id,location_id,parent_meter_id,initial_reading,installed_at,status,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (name,meter_number,meter_type,unit_id,location_id,parent_meter_id,initial_reading,installed_at,status,notes,now_sql()))
    db.commit()


def record_reading(data, user_id):
    ensure_schema()
    db = get_db()
    meter_id = to_int(data.get("meter_id"))
    reading_date = (data.get("reading_date") or date.today().isoformat()).strip()
    value = to_float(data.get("reading_value"), -1)
    if not meter_id or value < 0:
        raise ValueError("اختر العداد وأدخل قراءة صحيحة.")

    try:
        month_key = datetime.strptime(reading_date[:10], "%Y-%m-%d").strftime("%Y-%m")
    except ValueError:
        raise ValueError("تاريخ القراءة غير صحيح.")

    meter = db.execute(
        "SELECT id,initial_reading FROM network_meters WHERE id=? AND status='active'",
        (meter_id,),
    ).fetchone()
    if not meter:
        raise ValueError("العداد غير موجود أو غير نشط.")

    # القراءة السابقة تُؤخذ من آخر قراءة قبل الشهر الحالي، حتى تبقى القراءة
    # الشهرية مستقلة ولا تتأثر بقراءة أخرى داخل نفس الشهر عند التعديل.
    previous = meter_previous_before_month(
        db, meter_id, month_key, meter["initial_reading"]
    )
    existing = meter_month_reading(db, meter_id, month_key)

    if existing and value < previous:
        raise ValueError(f"القراءة الحالية أقل من السابقة ({previous:g}).")
    if not existing and value < previous:
        raise ValueError(f"القراءة الحالية أقل من السابقة ({previous:g}).")

    consumption = value - previous
    now = now_sql()

    if existing:
        db.execute(
            """
            UPDATE network_meter_readings
            SET reading_date=?, reading_value=?, previous_reading=?,
                consumption=?, note=?, created_by=?, created_at=?
            WHERE id=?
            """,
            (
                reading_date, value, previous, consumption,
                (data.get("note") or "").strip(), user_id, now, existing["id"]
            ),
        )
    else:
        db.execute(
            """
            INSERT INTO network_meter_readings
                (meter_id,reading_date,reading_value,previous_reading,consumption,note,created_by,created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                meter_id, reading_date, value, previous, consumption,
                (data.get("note") or "").strip(), user_id, now
            ),
        )
    db.commit()


def default_unit_id():
    ensure_schema()
    db = get_db()
    row = db.execute("SELECT id FROM water_units WHERE name='وحدة' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def unit_rows():
    ensure_schema()
    db = get_db()
    return db.execute("""
        SELECT u.*, COUNT(DISTINCT l.id) location_count, COUNT(DISTINCT m.id) meter_count
        FROM water_units u
        LEFT JOIN water_locations l ON l.unit_id=u.id
        LEFT JOIN network_meters m ON m.unit_id=u.id
        GROUP BY u.id ORDER BY u.sort_order,u.name
    """).fetchall()


def location_children_map():
    ensure_schema()
    db = get_db()
    # اربط أي مواقع موجودة بلا وحدة بالوحدة الافتراضية الحالية قبل بناء الشجرة،
    # لأن بعض المواقع قد تكون أُنشئت/استوردت بعد أول تشغيل لتهيئة المخطط.
    default_unit = db.execute("SELECT id FROM water_units WHERE name='وحدة' ORDER BY id LIMIT 1").fetchone()
    if default_unit:
        db.execute(
            "UPDATE water_locations SET unit_id=? WHERE unit_id IS NULL",
            (default_unit["id"],),
        )
        db.commit()
    rows = db.execute("SELECT id,unit_id,parent_id,code,name,location_type,active,sort_order,notes FROM water_locations ORDER BY sort_order,name,id").fetchall()
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    counts = {r["location_id"]: int(r["c"] or 0) for r in db.execute("SELECT location_id,COUNT(*) c FROM subscribers WHERE location_id IS NOT NULL GROUP BY location_id").fetchall()}
    meters = {r["location_id"]: int(r["c"] or 0) for r in db.execute("SELECT location_id,COUNT(*) c FROM network_meters WHERE location_id IS NOT NULL GROUP BY location_id").fetchall()}
    def node(r, depth=0):
        children = [node(c, depth + 1) for c in by_parent.get(r["id"], [])]
        # عدد المشتركين/العدادات في الأب يشمل الموقع نفسه وكل أبنائه.
        subscriber_count = counts.get(r["id"], 0) + sum(x["subscriber_count"] for x in children)
        meter_count = meters.get(r["id"], 0) + sum(x["meter_count"] for x in children)
        return {
            "id": r["id"], "unit_id": r["unit_id"], "parent_id": r["parent_id"],
            "code": r["code"], "name": r["name"], "location_type": r["location_type"],
            "active": bool(r["active"]), "sort_order": r["sort_order"],
            "subscriber_count": subscriber_count, "meter_count": meter_count,
            "direct_subscriber_count": counts.get(r["id"], 0),
            "direct_meter_count": meters.get(r["id"], 0),
            "children": children,
        }
    return [node(r) for r in by_parent.get(None, [])]


def flatten_locations(tree=None):
    tree = tree if tree is not None else location_children_map()
    out=[]
    def walk(nodes,path=""):
        for n in nodes:
            p=f"{path} / {n['name']}".strip(" /")
            item=dict(n); item["path"]=p; item["depth"]=p.count("/"); out.append(item)
            walk(n["children"],p)
    walk(tree)
    return out


def location_path(location_id):
    ensure_schema()
    db=get_db(); parts=[]; cursor=location_id
    for _ in range(50):
        if not cursor: break
        row=db.execute("SELECT id,parent_id,name FROM water_locations WHERE id=?",(cursor,)).fetchone()
        if not row: break
        parts.append(row["name"]); cursor=row["parent_id"]
    return " / ".join(reversed(parts))


def normalize_meter_month(value=None):
    value = (value or "").strip()
    try:
        return datetime.strptime(value + "-01", "%Y-%m-%d").strftime("%Y-%m")
    except ValueError:
        return date.today().strftime("%Y-%m")


def meter_month_reading(db, meter_id, month_label):
    """آخر قراءة للعداد داخل الشهر المحدد فقط."""
    month = normalize_meter_month(month_label)
    row = db.execute(
        """
        SELECT *
        FROM network_meter_readings
        WHERE meter_id=?
          AND substr(reading_date,1,7)=?
        ORDER BY reading_date DESC,id DESC
        LIMIT 1
        """,
        (meter_id, month),
    ).fetchone()
    if not row:
        return None
    return row


def meter_previous_before_month(db, meter_id, month_label, initial_reading=0):
    """آخر قراءة قبل بداية الشهر، لتثبيت القراءة السابقة الخاصة بالشهر."""
    month = normalize_meter_month(month_label)
    row = db.execute(
        """
        SELECT reading_value, reading_date, id
        FROM network_meter_readings
        WHERE meter_id=?
          AND substr(reading_date,1,7) < ?
        ORDER BY reading_date DESC,id DESC
        LIMIT 1
        """,
        (meter_id, month),
    ).fetchone()
    if row:
        return float(row["reading_value"] or 0)
    return float(initial_reading or 0)


def main_meter_month_rows(month_label=None):
    """بطاقات العدادات الرئيسية للشهر مع حالة قراءة الفروع والمفقودات."""
    ensure_schema()
    db = get_db()
    month = normalize_meter_month(month_label)
    rows = db.execute(
        """
        SELECT m.*, u.name unit_name, l.name location_name
        FROM network_meters m
        LEFT JOIN water_units u ON u.id=m.unit_id
        LEFT JOIN water_locations l ON l.id=m.location_id
        WHERE m.status='active' AND m.meter_type='main'
        ORDER BY m.name,m.id
        """
    ).fetchall()

    all_locations = flatten_locations()
    paths = {x["id"]: x["path"] for x in all_locations}
    out = []

    for meter in rows:
        current = meter_month_reading(db, meter["id"], month)
        previous = meter_previous_before_month(db, meter["id"], month, meter["initial_reading"])
        child_rows = db.execute(
            """
            SELECT m.id,m.name,m.meter_number,m.location_id,m.meter_type,
                   l.name location_name
            FROM network_meters m
            LEFT JOIN water_locations l ON l.id=m.location_id
            WHERE m.parent_meter_id=? AND m.status='active'
            ORDER BY m.name,m.id
            """,
            (meter["id"],),
        ).fetchall()

        child_cards = []
        child_read = 0
        child_consumption = 0.0
        for child in child_rows:
            cr = meter_month_reading(db, child["id"], month)
            cp = meter_previous_before_month(db, child["id"], month, 0)
            ccons = None
            if cr:
                child_read += 1
                ccons = max(0.0, float(cr["reading_value"] or 0) - cp)
                child_consumption += ccons
            child_cards.append({
                "id": child["id"],
                "name": child["name"],
                "meter_number": child["meter_number"],
                "location_name": child["location_name"],
                "reading": float(cr["reading_value"] or 0) if cr else None,
                "previous": cp,
                "consumption": ccons,
                "read": bool(cr),
            })

        subscriber_consumption = 0.0
        subscriber_count = 0
        invoice_count = 0
        if meter["location_id"] and meter["location_id"] in paths:
            root_path = paths[meter["location_id"]]
            location_ids = [
                x["id"] for x in all_locations
                if x["path"] == root_path or x["path"].startswith(root_path + " / ")
            ]
            child_location_ids = [x["location_id"] for x in child_rows if x["location_id"]]
            excluded = set()
            for child_location_id in child_location_ids:
                cp = paths.get(child_location_id)
                if not cp:
                    continue
                excluded.update(
                    x["id"] for x in all_locations
                    if x["path"] == cp or x["path"].startswith(cp + " / ")
                )
            target = [x for x in location_ids if x not in excluded]
            if target:
                marks = ",".join("?" for _ in target)
                sub_stats = db.execute(
                    f"""
                    SELECT COUNT(*) c
                    FROM subscribers s
                    WHERE s.active=1 AND s.location_id IN ({marks})
                    """,
                    target,
                ).fetchone()
                subscriber_count = int(sub_stats["c"] or 0)

                inv_stats = db.execute(
                    f"""
                    SELECT COUNT(i.id) c, COALESCE(SUM(i.consumption),0) s
                    FROM invoices i
                    JOIN subscribers s ON s.id=i.subscriber_id
                    WHERE s.active=1
                      AND s.location_id IN ({marks})
                      AND substr(COALESCE(i.month_label,i.invoice_date),1,7)=?
                    """,
                    target + [month],
                ).fetchone()
                invoice_count = int(inv_stats["c"] or 0)
                subscriber_consumption = float(inv_stats["s"] or 0)

        incoming = None
        if current:
            incoming = max(0.0, float(current["reading_value"] or 0) - previous)

        distributed = child_consumption + subscriber_consumption
        if not current:
            status = "missing_main"
            loss = None
            loss_pct = None
        elif child_rows and child_read < len(child_rows):
            status = "missing_children"
            loss = None
            loss_pct = None
        elif subscriber_count and invoice_count == 0:
            status = "missing_subscribers"
            loss = None
            loss_pct = None
        else:
            status = "complete"
            loss = (incoming or 0.0) - distributed
            loss_pct = (loss / incoming * 100) if incoming else 0.0

        out.append({
            "id": meter["id"],
            "name": meter["name"],
            "meter_number": meter["meter_number"],
            "unit_name": meter["unit_name"],
            "location_name": meter["location_name"],
            "initial_reading": float(meter["initial_reading"] or 0),
            "previous": previous,
            "reading": float(current["reading_value"] or 0) if current else None,
            "reading_date": current["reading_date"] if current else None,
            "incoming": incoming,
            "children": child_cards,
            "child_count": len(child_rows),
            "child_read_count": child_read,
            "child_unread_count": len(child_rows) - child_read,
            "subscriber_count": subscriber_count,
            "invoice_count": invoice_count,
            "subscriber_consumption": subscriber_consumption,
            "children_consumption": child_consumption,
            "distributed": distributed,
            "loss": loss,
            "loss_pct": loss_pct,
            "status": status,
        })
    return out


def meter_rows():
    ensure_schema(); db=get_db()
    return db.execute("""
      SELECT m.*, u.name unit_name, l.name location_name, p.name parent_name,
             (SELECT r.reading_value FROM network_meter_readings r WHERE r.meter_id=m.id ORDER BY r.reading_date DESC,r.id DESC LIMIT 1) last_reading,
             (SELECT r.reading_date FROM network_meter_readings r WHERE r.meter_id=m.id ORDER BY r.reading_date DESC,r.id DESC LIMIT 1) last_reading_date
      FROM network_meters m
      LEFT JOIN water_units u ON u.id=m.unit_id
      LEFT JOIN water_locations l ON l.id=m.location_id
      LEFT JOIN network_meters p ON p.id=m.parent_meter_id
      ORDER BY CASE m.meter_type WHEN 'main' THEN 0 ELSE 1 END, m.name
    """).fetchall()


def meter_detail(meter_id):
    ensure_schema(); db=get_db()
    meter=db.execute("""SELECT m.*,u.name unit_name,l.name location_name,p.name parent_name FROM network_meters m LEFT JOIN water_units u ON u.id=m.unit_id LEFT JOIN water_locations l ON l.id=m.location_id LEFT JOIN network_meters p ON p.id=m.parent_meter_id WHERE m.id=?""",(meter_id,)).fetchone()
    if not meter:return None
    children=db.execute("SELECT * FROM network_meters WHERE parent_meter_id=? ORDER BY name",(meter_id,)).fetchall()
    readings=db.execute("SELECT r.*,u.username creator_name FROM network_meter_readings r LEFT JOIN users u ON u.id=r.created_by WHERE r.meter_id=? ORDER BY r.reading_date DESC,r.id DESC LIMIT 20",(meter_id,)).fetchall()
    subscriber_count=0; subscriber_consumption=0.0
    if meter["location_id"]:
        # نجمع المشتركين في موقع العداد وفروعه؛ الفروع المرتبطة بعداد شبكي أدق تُحسب داخل ذلك العداد بدل تكرارها.
        tree_ids=[meter["location_id"]]
        all_locs=flatten_locations()
        for x in all_locs:
            if x["id"]!=meter["location_id"] and x["path"].startswith(location_path(meter["location_id"]) + " / "):
                tree_ids.append(x["id"])
        if tree_ids:
            marks=",".join("?" for _ in tree_ids)
            child_loc_ids=[r["location_id"] for r in children if r["location_id"]]
            excluded=[]
            if child_loc_ids:
                excluded = [x["id"] for x in all_locs if any(x["id"]==cl or x["path"].startswith(location_path(cl)+" / ") for cl in child_loc_ids)]
            target=[x for x in tree_ids if x not in set(excluded)]
            if target:
                marks2=",".join("?" for _ in target)
                subscriber_count=db.execute(f"SELECT COUNT(*) c FROM subscribers WHERE active=1 AND location_id IN ({marks2})",target).fetchone()["c"] or 0
                subscriber_consumption=db.execute(f"SELECT COALESCE(SUM(i.consumption),0) s FROM invoices i JOIN subscribers s ON s.id=i.subscriber_id WHERE s.active=1 AND s.location_id IN ({marks2})",target).fetchone()["s"] or 0
    last_read=float(readings[0]["reading_value"] if readings else meter["initial_reading"] or 0)
    return {"meter":meter,"children":children,"readings":readings,"subscriber_count":int(subscriber_count),"subscriber_consumption":float(subscriber_consumption),"last_reading":last_read}


def monthly_loss_summary(month_label=None):
    """ملخص المفقودات لكل شهر، مبني على قراءات ذلك الشهر فقط."""
    month = normalize_meter_month(month_label)
    rows = main_meter_month_rows(month)
    complete = [x for x in rows if x["status"] == "complete"]
    total_in = sum(float(x["incoming"] or 0) for x in complete)
    total_distributed = sum(float(x["distributed"] or 0) for x in complete)
    total_loss = total_in - total_distributed
    read_main = sum(1 for x in rows if x["reading"] is not None)
    read_children = sum(x["child_read_count"] for x in rows)
    total_children = sum(x["child_count"] for x in rows)
    return {
        "month_label": month,
        "rows": rows,
        "main_total": len(rows),
        "main_read": read_main,
        "main_unread": len(rows) - read_main,
        "child_total": total_children,
        "child_read": read_children,
        "child_unread": total_children - read_children,
        "complete_count": len(complete),
        "incomplete_count": len(rows) - len(complete),
        "total_in": total_in,
        "total_dist": total_distributed,
        "total_loss": total_loss,
        "total_pct": (total_loss / total_in * 100) if total_in else 0.0,
    }


def available_loss_months():
    """الأشهر التي توجد لها حركة قراءة أو فواتير، مع إبقاء الشهر الحالي متاحاً."""
    ensure_schema()
    db = get_db()
    rows = db.execute(
        """
        SELECT month_key FROM (
            SELECT substr(reading_date,1,7) AS month_key
            FROM network_meter_readings
            WHERE reading_date IS NOT NULL AND TRIM(reading_date) <> ''
            UNION
            SELECT substr(COALESCE(month_label,invoice_date),1,7) AS month_key
            FROM invoices
            WHERE COALESCE(month_label,invoice_date) IS NOT NULL
        )
        WHERE month_key IS NOT NULL AND month_key <> ''
        ORDER BY month_key DESC
        """
    ).fetchall()
    months = [str(r["month_key"]) for r in rows]
    current = date.today().strftime("%Y-%m")
    if current not in months:
        months.insert(0, current)
    return months


def loss_rows(start=None,end=None):
    ensure_schema()
    db=get_db()
    start=start or date(date.today().year,date.today().month,1).isoformat()
    end=end or date.today().isoformat()
    meters=db.execute(
        "SELECT m.*,u.name unit_name,l.name location_name FROM network_meters m "
        "LEFT JOIN water_units u ON u.id=m.unit_id "
        "LEFT JOIN water_locations l ON l.id=m.location_id "
        "WHERE m.status='active' ORDER BY CASE m.meter_type WHEN 'main' THEN 0 ELSE 1 END,m.name"
    ).fetchall()
    all_locations=flatten_locations()
    paths={x["id"]:x["path"] for x in all_locations}
    rows=[]
    for m in meters:
        reading_stats=db.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(consumption),0) s FROM network_meter_readings WHERE meter_id=? AND reading_date BETWEEN ? AND ?",
            (m["id"],start,end)
        ).fetchone()
        reading_count=int(reading_stats["c"] or 0)
        incoming=float(reading_stats["s"] or 0)

        child_rows=db.execute(
            "SELECT id,location_id FROM network_meters WHERE parent_meter_id=? AND status='active'",
            (m["id"],)
        ).fetchall()
        child_ids=[int(x["id"]) for x in child_rows]
        child_consumption=0.0
        child_reading_count=0
        if child_ids:
            marks=",".join("?" for _ in child_ids)
            child_stats=db.execute(
                f"SELECT COUNT(*) c, COALESCE(SUM(consumption),0) s FROM network_meter_readings WHERE meter_id IN ({marks}) AND reading_date BETWEEN ? AND ?",
                child_ids+[start,end]
            ).fetchone()
            child_reading_count=int(child_stats["c"] or 0)
            child_consumption=float(child_stats["s"] or 0)

        subscriber_consumption=0.0
        subscriber_invoice_count=0
        if m["location_id"] and m["location_id"] in paths:
            root_path=paths[m["location_id"]]
            location_ids=[x["id"] for x in all_locations if x["path"]==root_path or x["path"].startswith(root_path+" / ")]
            excluded=set()
            for child in child_rows:
                cl=child["location_id"]
                if not cl or cl not in paths:
                    continue
                cp=paths[cl]
                excluded.update(x["id"] for x in all_locations if x["path"]==cp or x["path"].startswith(cp+" / "))
            target=[x for x in location_ids if x not in excluded]
            if target:
                marks=",".join("?" for _ in target)
                sub_stats=db.execute(
                    f"SELECT COUNT(i.id) c, COALESCE(SUM(i.consumption),0) s "
                    f"FROM invoices i JOIN subscribers s ON s.id=i.subscriber_id "
                    f"WHERE s.active=1 AND s.location_id IN ({marks}) AND i.invoice_date BETWEEN ? AND ?",
                    target+[start,end]
                ).fetchone()
                subscriber_invoice_count=int(sub_stats["c"] or 0)
                subscriber_consumption=float(sub_stats["s"] or 0)

        distributed=child_consumption+subscriber_consumption
        if reading_count==0:
            status="no_meter_readings"
            loss=None
            loss_pct=None
        elif child_reading_count==0 and subscriber_invoice_count==0:
            status="no_downstream_data"
            loss=None
            loss_pct=None
        else:
            status="complete"
            loss=incoming-distributed
            loss_pct=(loss/incoming*100) if incoming else 0.0

        rows.append({
            "id":m["id"],"name":m["name"],"meter_number":m["meter_number"],
            "meter_type":m["meter_type"],"unit_name":m["unit_name"],"location_name":m["location_name"],
            "incoming":incoming,"children_consumption":child_consumption,
            "subscriber_consumption":subscriber_consumption,"distributed":distributed,
            "loss":loss,"loss_pct":loss_pct,"status":status,
            "reading_count":reading_count,"child_reading_count":child_reading_count,
            "subscriber_invoice_count":subscriber_invoice_count,
        })
    return rows
