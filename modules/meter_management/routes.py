from __future__ import annotations

import re
from datetime import date
from functools import wraps

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

from database import get_db
from . import bp
from .models import ensure_schema
from .services import (
    delete_location, flatten_locations, location_children_map, location_path, loss_rows, meter_detail,
    meter_rows, record_reading, save_location, save_meter, save_unit, unit_rows,
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
    stats = {
        "units": db.execute("SELECT COUNT(*) c FROM water_units WHERE active=1").fetchone()["c"],
        "locations": db.execute("SELECT COUNT(*) c FROM water_locations WHERE active=1").fetchone()["c"],
        "meters": db.execute("SELECT COUNT(*) c FROM network_meters WHERE status='active'").fetchone()["c"],
        "main_meters": db.execute("SELECT COUNT(*) c FROM network_meters WHERE status='active' AND meter_type='main'").fetchone()["c"],
        "linked_subscribers": db.execute("SELECT COUNT(*) c FROM subscribers WHERE active=1 AND location_id IS NOT NULL").fetchone()["c"],
        "unlinked_subscribers": db.execute("SELECT COUNT(*) c FROM subscribers WHERE active=1 AND (location_id IS NULL OR location_id=0)").fetchone()["c"],
    }
    return render_template(
        "meter_management/dashboard.html",
        stats=stats,
        meters=meter_rows()[:8],
        units=unit_rows()[:6],
        loss_rows=loss_rows()[:8],
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
    return render_template(
        "meter_management/location_form.html",
        mode="new",
        location={},
        units=unit_rows(),
        parents=flatten_locations(),
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
    return render_template(
        "meter_management/location_form.html",
        mode="edit",
        location=location,
        units=unit_rows(),
        parents=parents,
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
    )


@bp.route("/meters/<int:meter_id>")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def meter_detail(meter_id):
    payload = meter_detail(meter_id)
    if not payload:
        abort(404)
    return render_template("meter_management/meter_detail.html", **payload)


@bp.route("/readings")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def readings():
    db = get_db()
    meter_id = request.args.get("meter_id", type=int)
    sql = """
        SELECT r.*,m.name meter_name,m.meter_number,u.username creator_name
        FROM network_meter_readings r
        JOIN network_meters m ON m.id=r.meter_id
        LEFT JOIN users u ON u.id=r.created_by
        WHERE 1=1
    """
    params = []
    if meter_id:
        sql += " AND r.meter_id=?"
        params.append(meter_id)
    sql += " ORDER BY r.reading_date DESC,r.id DESC LIMIT 300"
    rows = db.execute(sql, params).fetchall()
    return render_template(
        "meter_management/readings.html",
        readings=rows,
        meters=meter_rows(),
        selected_meter=meter_id,
    )


@bp.route("/readings/new", methods=["GET", "POST"])
@role_required(["admin", "manager", "technician", "staff"])
def reading_new():
    if request.method == "POST":
        try:
            record_reading(request.form, session.get("user_id"))
            flash("تم حفظ القراءة وتسجيل الاستهلاك.", "success")
            meter_id = request.form.get("meter_id", type=int)
            return redirect(url_for("meter_management.readings", meter_id=meter_id))
        except Exception as exc:
            get_db().rollback()
            flash(str(exc), "danger")
    return render_template(
        "meter_management/reading_form.html",
        meters=meter_rows(),
        today=date.today().isoformat(),
        prefill_meter=request.args.get("meter_id", type=int),
    )


@bp.route("/losses")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def losses():
    start = request.args.get("start", date(date.today().year, date.today().month, 1).isoformat())
    end = request.args.get("end", date.today().isoformat())
    rows = loss_rows(start, end)
    total_in = sum(x["incoming"] for x in rows)
    total_dist = sum(x["distributed"] for x in rows)
    total_loss = total_in - total_dist
    return render_template(
        "meter_management/losses.html",
        rows=rows,
        start=start,
        end=end,
        total_in=total_in,
        total_dist=total_dist,
        total_loss=total_loss,
        total_pct=(total_loss / total_in * 100 if total_in else 0),
    )


@bp.route("/print/losses")
@role_required(["admin", "manager", "technician", "staff", "collector"])
def print_losses():
    start = request.args.get("start", date(date.today().year, date.today().month, 1).isoformat())
    end = request.args.get("end", date.today().isoformat())
    rows = loss_rows(start, end)
    total_in = sum(x["incoming"] for x in rows)
    total_dist = sum(x["distributed"] for x in rows)
    total_loss = total_in - total_dist
    return render_template(
        "meter_management/print_report.html",
        title="تقرير مفقودات المياه",
        rows=rows,
        start=start,
        end=end,
        total_in=total_in,
        total_dist=total_dist,
        total_loss=total_loss,
        total_pct=(total_loss / total_in * 100 if total_in else 0),
    )


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
