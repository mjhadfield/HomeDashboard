"""`python -m app` runs the server."""
import logging

import uvicorn

from . import config

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info", access_log=False,
                timeout_graceful_shutdown=3)   # open event-stream connections would otherwise hold a restart up for 10 s until systemd kills it
