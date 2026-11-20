"""Development entry point: python run.py"""
import os

from tbs import create_app, start_scheduler

app = create_app()

if __name__ == "__main__":
    # With the reloader on, only the child process should start the background worker.
    debug = os.environ.get("TBS_DEBUG", "0") == "1"
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler(app)
    app.run(host=os.environ.get("TBS_HOST", "127.0.0.1"), port=int(os.environ.get("TBS_PORT", "5000")), debug=debug)
