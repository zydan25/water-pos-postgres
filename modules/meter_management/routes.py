from __future__ import annotations

import re
from datetime import date
from functools import wraps

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from database import get_db
from . import bp
from .models import ensure_schema
from .services import (
    delete_location, flatten_locations, location_children_map, location_path, loss_rows, monthly_loss_summary, available_loss_months,
    main_meter_month_rows, normalize_meter_month, meter_detail as get_meter_detail,
    default_unit_id, meter_rows, record_reading, save_location, save_meter, save_unit, unit_rows,
)


def role_required(roles):
    allowed = {r.lower() for r in roles}

    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login"))
            role = (getattr(current_app, "user_role", "") or "").lower()
            try:
                from flask import g
                role = (g.user["role"] or "").lower() if g.user else role
            except Exception:
                pass
            if role not in allowed:
                abort(403)
            return fn(*args, **kwargs)

        return wrapped

    return deco


@bp.before_app_request
def _ensure_module_schema_once():
    if not current_app.extensions.get("meter_management_schema_ready"):
        ensure_schema()
        current_app.extensions["meter_management_schema_ready"] = True


@bp.after_app_request
def _capture_subscriber_location(response):
    # نحفظ الموقع فقط بعد نجاح المسار الأصلي للمشترك.
    if request.method != "POST" or response.status_code not in {301, 302, 303, 307, 308}:
        return response
    location_id = request.form.get("location_id", type=int)
    if not location_id:
        return response

    path = request.path
    subscriber_id = None

    if path == "/subscribers/new":
        account = (request.form.get("account_number") or "").strip()
        if account:
            row = get_db().execute(
                "SELECT id FROM subscribers WHERE account_number=?", (account,)
            ).fetchone()
            subscriber_id = row["id"] if row else None
    else:
        match = re.fullmatch(r"/subscribers/(\d+)/edit", path)
        if match:
            subscriber_id = int(match.group(1))

    if subscriber_id:
        db = get_db()
        loc = db.execute(
            "SELECT id,name FROM water_locations WHERE id=? AND active=1", (location_id,)
        ).fetchone()
        if loc:
            db.execute(
                "UPDATE subscribers SET location_id=?, village=?, updated_at=? WHERE id=?",
                (
                    location_id,
                    loc["name"],
                    __import__("datetime").datetime.utcnow().isoformat(),
                    subscriber_id,
                ),
            )
            db.commit()
    return response


@bp.route("/")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def dashboard():
    db = get_db()
    month_label = normalize_meter_month(request.args.get("month_label"))
    stats = {
        "units": db.execute("SELECT COUNT(*) c FROM water_units WHERE active=1").fetchone()["c"],
        "locations": db.execute("SELECT COUNT(*) c FROM water_locations WHERE active=1").fetchone()["c"],
        "meters": db.execute("SELECT COUNT(*) c FROM network_meters WHERE status='active'").fetchone()["c"],
        "main_meters": db.execute("SELECT COUNT(*) c FROM network_meters WHERE status='active' AND meter_type='main'").fetchone()["c"],
        "linked_subscribers": db.execute("SELECT COUNT(*) c FROM subscribers WHERE active=1 AND location_id IS NOT NULL").fetchone()["c"],
        "unlinked_subscribers": db.execute("SELECT COUNT(*) c FROM subscribers WHERE active=1 AND (location_id IS NULL OR location_id=0)").fetchone()["c"],
    }
    monthly = monthly_loss_summary(month_label)
    return render_template(
        "meter_management/dashboard.html",
        stats=stats,
        meters=meter_rows()[:8],
        units=unit_rows()[:6],
        loss_rows=monthly["rows"],
        main_meters=monthly["rows"],
        month_label=month_label,
        monthly=monthly,
        tree=location_children_map(),
    )

@bp.route("/units")
@role_required(["admin", "manager", "technician", "staff"])
def units():
    return render_template("meter_management/units.html", units=unit_rows())


@bp.route("/units/new", methods=["GET", "POST"])
@role_required(["admin", "manager", "technician"])
def unit_new():
    if request.method == "POST":
        try:
            save_unit(request.form)
            flash("تمت إضافة الوحدة.", "success")
            return redirect(url_for("meter_management.units"))
        except Exception as exc:
            get_db().rollback()
            flash(str(exc), "danger")
    return render_template("meter_management/unit_form.html", mode="new", unit={})


@bp.route("/units/<int:unit_id>/edit", methods=["GET", "POST"])
@role_required(["admin", "manager", "technician"])
def unit_edit(unit_id):
    db = get_db()
    unit = db.execute("SELECT * FROM water_units WHERE id=?", (unit_id,)).fetchone()
    if not unit:
        abort(404)
    if request.method == "POST":
        try:
            save_unit(request.form, unit_id)
            flash("تم تحديث الوحدة.", "success")
            return redirect(url_for("meter_management.units"))
        except Exception as exc:
            db.rollback()
            flash(str(exc), "danger")
    return render_template("meter_management/unit_form.html", mode="edit", unit=unit)


@bp.route("/locations")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def locations():
    return render_template(
        "meter_management/locations.html",
        tree=location_children_map(),
        locations=flatten_locations(),
        units=unit_rows(),
    )


@bp.route("/locations/new", methods=["GET", "POST"])
@role_required(["admin", "manager", "technician"])
def location_new():
    if request.method == "POST":
        try:
            save_location(request.form)
            flash("تمت إضافة الموقع.", "success")
            return redirect(url_for("meter_management.locations"))
        except Exception as exc:
            get_db().rollback()
            flash(str(exc), "danger")
    units = unit_rows()
    return render_template(
        "meter_management/location_form.html",
        mode="new",
        location={},
        units=units,
        parents=flatten_locations(),
        current_parent_path="",
        blocked_location_id=None,
        default_unit_id=default_unit_id(),
    )


@bp.route("/locations/<int:location_id>/edit", methods=["GET", "POST"])
@role_required(["admin", "manager", "technician"])
def location_edit(location_id):
    db = get_db()
    location = db.execute("SELECT * FROM water_locations WHERE id=?", (location_id,)).fetchone()
    if not location:
        abort(404)
    if request.method == "POST":
        try:
            save_location(request.form, location_id)
            flash("تم تحديث الموقع.", "success")
            return redirect(url_for("meter_management.locations"))
        except Exception as exc:
            db.rollback()
            flash(str(exc), "danger")

    own_path = location_path(location_id)
    parents = [
        p for p in flatten_locations()
        if p["id"] != location_id
        and not p["path"].startswith(own_path + " / ")
    ]
    units = unit_rows()
    return render_template(
        "meter_management/location_form.html",
        mode="edit",
        location=location,
        units=units,
        parents=parents,
        current_parent_path=location_path(location["parent_id"]) if location["parent_id"] else "",
        blocked_location_id=location_id,
        default_unit_id=default_unit_id(),
    )


@bp.route("/locations/<int:location_id>/delete", methods=["POST"])
@role_required(["admin", "manager", "technician"])
def location_delete(location_id):
    try:
        delete_location(location_id)
        flash("تم حذف الموقع.", "success")
    except Exception as exc:
        get_db().rollback()
        flash(str(exc), "danger")
    return redirect(url_for("meter_management.locations"))


@bp.route("/meters")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def meters():
    return render_template("meter_management/meters.html", meters=meter_rows())


@bp.route("/meters/new", methods=["GET", "POST"])
@role_required(["admin", "manager", "technician"])
def meter_new():
    if request.method == "POST":
        try:
            save_meter(request.form)
            flash("تمت إضافة عداد الشبكة.", "success")
            return redirect(url_for("meter_management.meters"))
        except Exception as exc:
            get_db().rollback()
            flash(str(exc), "danger")
    return render_template(
        "meter_management/meter_form.html",
        mode="new",
        meter={},
        units=unit_rows(),
        locations=flatten_locations(),
        parents=meter_rows(),
        current_location_path="",
        default_unit_id=default_unit_id(),
    )


@bp.route("/meters/<int:meter_id>/edit", methods=["GET", "POST"])
@role_required(["admin", "manager", "technician"])
def meter_edit(meter_id):
    db = get_db()
    meter = db.execute("SELECT * FROM network_meters WHERE id=?", (meter_id,)).fetchone()
    if not meter:
        abort(404)
    if request.method == "POST":
        try:
            save_meter(request.form, meter_id)
            flash("تم تحديث العداد.", "success")
            return redirect(url_for("meter_management.meter_detail", meter_id=meter_id))
        except Exception as exc:
            db.rollback()
            flash(str(exc), "danger")
    parents = [p for p in meter_rows() if p["id"] != meter_id]
    return render_template(
        "meter_management/meter_form.html",
        mode="edit",
        meter=meter,
        units=unit_rows(),
        locations=flatten_locations(),
        parents=parents,
        current_location_path=location_path(meter["location_id"]) if meter["location_id"] else "",
        default_unit_id=default_unit_id(),
    )


@bp.route("/meters/<int:meter_id>")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def meter_detail(meter_id):
    payload = get_meter_detail(meter_id)
    if not payload:
        abort(404)
    return render_template("meter_management/meter_detail.html", **payload)


@bp.route("/readings")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def readings():
    db = get_db()
    meter_id = request.args.get("meter_id", type=int)
    month_param = request.args.get("month_label")
    if month_param is not None and month_param.strip().lower() == "all":
        month_label = ""
    else:
        month_label = normalize_meter_month(month_param)
    meter_type = request.args.get("meter_type", "").strip()
    sort = request.args.get("sort", "date").strip()
    sql = """
        SELECT r.*,m.name meter_name,m.meter_number,m.meter_type,
               l.name location_name,u.username creator_name
        FROM network_meter_readings r
        JOIN network_meters m ON m.id=r.meter_id
        LEFT JOIN water_locations l ON l.id=m.location_id
        LEFT JOIN users u ON u.id=r.created_by
        WHERE 1=1
    """
    params = []
    if meter_id:
        sql += " AND r.meter_id=?"
        params.append(meter_id)
    if month_label:
        month_label = normalize_meter_month(month_label)
        sql += " AND substr(r.reading_date,1,7)=?"
        params.append(month_label)
    if meter_type in {"main", "sub"}:
        sql += " AND m.meter_type=?"
        params.append("main" if meter_type == "main" else "sub")
    order_map = {
        "date": "r.reading_date DESC,r.id DESC",
        "meter": "LOWER(m.name) ASC,r.reading_date DESC",
        "location": "LOWER(COALESCE(l.name,'')) ASC,LOWER(m.name) ASC,r.reading_date DESC",
        "consumption": "r.consumption DESC,r.reading_date DESC",
        "reading": "r.reading_value DESC,r.reading_date DESC",
    }
    sql += " ORDER BY " + order_map.get(sort, order_map["date"]) + " LIMIT 500"
    rows = db.execute(sql, params).fetchall()
    months = available_loss_months()
    return render_template(
        "meter_management/readings.html",
        readings=rows,
        meters=meter_rows(),
        selected_meter=meter_id,
        month_label=month_label,
        meter_type=meter_type,
        sort=sort,
        months=months,
    )

@bp.route("/readings/new", methods=["GET", "POST"])
@role_required(["admin", "manager", "technician", "staff"])
def reading_new():
    requested_month = (
        request.form.get("month_label", "").strip()
        if request.method == "POST"
        else request.args.get("month_label", "").strip()
    )
    month_label = normalize_meter_month(requested_month) if requested_month else date.today().strftime("%Y-%m")

    if request.method == "POST":
        try:
            record_reading(request.form, session.get("user_id"))
            flash("تم حفظ قراءة العداد للشهر المحدد.", "success")
            meter_id = request.form.get("meter_id", type=int)
            return redirect(url_for("meter_management.readings", meter_id=meter_id, month_label=month_label))
        except Exception as exc:
            get_db().rollback()
            flash(str(exc), "danger")

    meter_id = request.args.get("meter_id", type=int)
    current = meter_month_reading(get_db(), meter_id, month_label) if meter_id else None
    selected_date = current["reading_date"] if current else date.today().isoformat()
    if not current and month_label != date.today().strftime("%Y-%m"):
        y, m = [int(x) for x in month_label.split("-")]
        if m == 12:
            next_month = date(y + 1, 1, 1)
        else:
            next_month = date(y, m + 1, 1)
        selected_date = (next_month - __import__("datetime").timedelta(days=1)).isoformat()

    return render_template(
        "meter_management/reading_form.html",
        meters=meter_rows(),
        today=selected_date,
        month_label=month_label,
        prefill_meter=meter_id,
        existing_reading=current,
    )


@bp.route("/losses")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def losses():
    month_param = (request.args.get("month_label") or "").strip().lower()
    meter_id = request.args.get("meter_id", type=int)
    months = available_loss_months()

    if month_param == "all":
        summaries = [monthly_loss_summary(m) for m in months]
        return render_template(
            "meter_management/losses.html",
            all_months=True,
            month_summaries=summaries,
            months=months,
            month_label="all",
            meter_id=None,
            rows=[],
            main_total=0, main_read=0, main_unread=0,
            child_total=0, child_read=0, child_unread=0,
            total_in=0, total_dist=0, total_loss=0, total_pct=0,
            complete_count=0, incomplete_count=0,
        )

    month_label = normalize_meter_month(request.args.get("month_label"))
    monthly = monthly_loss_summary(month_label)
    rows = [r for r in monthly["rows"] if not meter_id or r["id"] == meter_id]
    return render_template(
        "meter_management/losses.html",
        all_months=False,
        month_summaries=[],
        rows=rows,
        month_label=month_label,
        months=months,
        meter_id=meter_id,
        main_total=monthly["main_total"],
        main_read=monthly["main_read"],
        main_unread=monthly["main_unread"],
        child_total=monthly["child_total"],
        child_read=monthly["child_read"],
        child_unread=monthly["child_unread"],
        total_in=monthly["total_in"],
        total_dist=monthly["total_dist"],
        total_loss=monthly["total_loss"],
        total_pct=monthly["total_pct"],
        complete_count=monthly["complete_count"],
        incomplete_count=monthly["incomplete_count"],
    )


@bp.route("/print/losses")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def print_losses():
    month_label = normalize_meter_month(request.args.get("month_label"))
    monthly = monthly_loss_summary(month_label)
    return render_template(
        "meter_management/print_report.html",
        title="تقرير مفقودات المياه",
        rows=monthly["rows"],
        month_label=month_label,
        total_in=monthly["total_in"],
        total_dist=monthly["total_dist"],
        total_loss=monthly["total_loss"],
        total_pct=monthly["total_pct"],
    )

@bp.route("/print/meter/<int:meter_id>")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def print_meter(meter_id):
    payload = meter_detail(meter_id)
    if not payload:
        abort(404)
    return render_template("meter_management/print_meter.html", **payload)


@bp.route("/print/network")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def print_network():
    return render_template(
        "meter_management/print_network.html",
        units=unit_rows(),
        tree=location_children_map(),
        meters=meter_rows(),
    )


@bp.route("/api/locations/tree")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def api_locations_tree():
    return jsonify({"tree": location_children_map()})


@bp.route("/api/meters/<int:meter_id>/location")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def api_meter_location(meter_id):
    db = get_db()
    row = db.execute(
        """SELECT m.location_id, l.name location_name
           FROM network_meters m
           LEFT JOIN water_locations l ON l.id=m.location_id
           WHERE m.id=?""",
        (meter_id,),
    ).fetchone()
    if not row:
        abort(404)
    lid = row["location_id"]
    return jsonify({
        "location_id": lid,
        "name": row["location_name"] or "",
        "path": location_path(lid) if lid else "",
    })


@bp.route("/api/subscribers/<int:subscriber_id>/location")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def api_subscriber_location(subscriber_id):
    db = get_db()
    row = db.execute(
        "SELECT location_id,village FROM subscribers WHERE id=?", (subscriber_id,)
    ).fetchone()
    if not row:
        abort(404)
    lid = row["location_id"]
    return jsonify({
        "location_id": lid,
        "name": row["village"] or "",
        "path": location_path(lid) if lid else (row["village"] or ""),
    })
