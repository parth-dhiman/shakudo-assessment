# Third-party imports.
from flask import (
    Flask,
    Response,
)

# Local imports.
from app.api.error_handlers import register_error_handlers

# ===== Script-Level initializations =====

app = Flask(
    __name__,
    static_folder   = "../../web", # Relative to this package (app/api/), i.e. <repo>/web.
    static_url_path = "/web",
)
register_error_handlers(app)

# ========== UI ==========

@app.get("/")
def index() -> Response:
    return app.send_static_file("index.html")

# ========== Routes ==========

# Each module registers its endpoints on `app` when imported, so these imports stay last.
from app.api import deployments  # noqa: E402, F401
