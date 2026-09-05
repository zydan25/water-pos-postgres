from flask import Blueprint, request, session, flash, redirect, url_for, abort
from datetime import date
from database import get_db, log_transaction, log_audit # الدوال المساعدة

# تعريف Blueprint خاص بالعمليات المالية
financial_bp = Blueprint('financial', __name__)

@financial_bp.route("/invoices/<int:invoice_id>/payment", methods=["POST"])
def add_payment(invoice_id):
    db = get_db()
    # ... نضع هنا كود الـ add_payment بالكامل الذي كتبناه سابقاً ...
    return redirect(url_for("invoice_detail", invoice_id=invoice_id))