from app.api import app
from app.config import HOST, PORT

if __name__ == "__main__":
    # threaded: one slow request (e.g. waiting on a rollout) must not block the others
    app.run(host=HOST, port=PORT, threaded=True)
