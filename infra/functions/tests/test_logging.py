"""Storage request URLs (blob paths = record and file names) stay out of the logs."""

import logging

import function_app as fa  # noqa: F401  (the cap is set at import)


def test_azure_sdk_request_logging_is_capped_at_warning():
    logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")
    assert logger.level == logging.WARNING
    assert not logger.isEnabledFor(logging.INFO)
