import pytest
from unittest.mock import patch, MagicMock
from src.scheduler import attachment_path_for_send
from src.models import AppConfig

@patch("src.scheduler.db.resolve_project_path")
def test_attachment_path_for_send(mock_resolve):
    config = AppConfig()

    # Campaign with no attachment
    campaign_no_attachment = {"attachment_path": ""}
    assert attachment_path_for_send(config, campaign_no_attachment) is None

    # Filesystem paths are never resolved or attached.
    campaign_with_attachment = {"attachment_path": "test.pdf"}
    assert attachment_path_for_send(config, campaign_with_attachment) is None
    mock_resolve.assert_not_called()

