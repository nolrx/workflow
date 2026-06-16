"""
Request Logging Middleware

Logs all HTTP requests with timing information.
"""
import time
import logging
from flask import Flask, request, g

logger = logging.getLogger(__name__)


def setup_request_logging(app: Flask):
    """
    Set up request logging middleware for the Flask app.

    This adds before_request and after_request hooks to log:
    - Request method and path
    - Response status code
    - Request duration

    Args:
        app: Flask application instance
    """

    @app.before_request
    def before_request():
        """Record request start time"""
        g.start_time = time.time()

    @app.after_request
    def after_request(response):
        """Log request after completion"""
        # Calculate request duration
        duration = time.time() - g.get('start_time', time.time())

        # Get request info
        method = request.method
        path = request.path
        status_code = response.status_code

        # Skip health check endpoint from logs to reduce noise
        if path == '/health':
            return response

        # Log format: "POST /api/auth/login - 200 - 0.123s"
        log_level = logging.INFO
        if status_code >= 500:
            log_level = logging.ERROR
        elif status_code >= 400:
            log_level = logging.WARNING

        logger.log(
            log_level,
            f"{method} {path} - {status_code} - {duration:.3f}s"
        )

        return response

    @app.teardown_request
    def teardown_request(exception):
        """Log any unhandled exceptions during request.

        Streaming responses (e.g. the agent SSE stream) raise ``GeneratorExit``
        into their generator when the client disconnects — that is the normal
        end-of-stream signal, not an error, so it is ignored here to avoid noisy
        tracebacks on every closed stream.
        """
        if exception and not isinstance(exception, GeneratorExit):
            logger.error(
                f"Request teardown exception: {exception}",
                exc_info=True
            )
