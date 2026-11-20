"""Production entry point, for example:  gunicorn --workers 1 --threads 4 wsgi:app
Use one worker process: it also runs the reminder scheduler. Several threads are fine."""
from tbs import create_app, start_scheduler

app = create_app()
start_scheduler(app)
