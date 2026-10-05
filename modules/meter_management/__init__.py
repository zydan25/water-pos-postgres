from flask import Blueprint

bp = Blueprint(
    "meter_management",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

from . import routes  # noqa: E402,F401
