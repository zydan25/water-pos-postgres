import os
import sys
from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5006))

    # استبعاد مجلدات مكتبات النظام من المراقبة لمنع إعادة التشغيل غير الضرورية
    python_lib = os.path.dirname(os.__file__)
    site_packages = os.path.join(python_lib, "site-packages")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
        extra_files=[],
        exclude_patterns=[
            os.path.join(python_lib, "**"),
            os.path.join(site_packages, "**"),
        ]
    )
