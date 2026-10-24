"""Flask composition root. Importing routes starts no background work."""

import os
import sys

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from flask import Flask, jsonify
from core import config


def create_app():
    from services.auth import auth_bp
    from services.podcast_api import podcast_bp
    from services.tools_api import tools_bp
    from services.media_api import media_bp
    from services.task_query import task_query_bp
    from services.tts_api import tts_bp
    from services.task_api import task_bp

    app = Flask(
        __name__, static_folder=os.path.join(config.BASE_DIR, "web"), static_url_path=""
    )
    app.secret_key = config.SECRET_KEY
    from core.task_manager import InvalidTaskCheckpoint

    @app.errorhandler(InvalidTaskCheckpoint)
    def invalid_checkpoint(error):
        return jsonify(error=str(error), code="invalid_checkpoint"), 409

    for blueprint in (
        auth_bp,
        podcast_bp,
        tools_bp,
        media_bp,
        task_query_bp,
        tts_bp,
        task_bp,
    ):
        app.register_blueprint(blueprint)
    return app


app = create_app()


def main():
    import logging
    from services.bootstrap import (
        _check_required_dependencies,
        _check_hardware_requirements,
        start_runtime,
    )

    _check_required_dependencies()
    _check_hardware_requirements()
    start_runtime()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    port = (
        int(sys.argv[1])
        if len(sys.argv) > 1
        else int(os.environ.get("BILIMIX_PORT", "5000"))
    )
    app.run(
        host=os.environ.get("BILIMIX_HOST", "0.0.0.0"),
        port=port,
        debug=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
