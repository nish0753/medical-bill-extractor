import pytest
import json
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.main import app


client = TestClient(app)


class TestHealthEndpoint:
    """Test health check endpoint - no external dependencies."""
    
    def test_health_returns_200(self):
        """Test that /health endpoint returns 200 status."""
        response = client.get("/health")
        assert response.status_code == 200
    
    def test_health_returns_healthy_status(self):
        """Test that /health returns healthy status."""
        response = client.get("/health")
        data = response.json()
        assert data.get("status") == "healthy"
    
    def test_health_includes_version(self):
        """Test that /health response includes version."""
        response = client.get("/health")
        data = response.json()
        assert "version" in data
        assert isinstance(data["version"], str)


class TestBillExtractionRequestValidation:
    """Test request validation on /extract-bill-data endpoint."""
    
    def test_missing_document_field(self):
        """Test endpoint rejects request without document field."""
        response = client.post("/extract-bill-data", json={})
        assert response.status_code == 422
    
    def test_null_document_field(self):
        """Test endpoint handles null document field."""
        response = client.post("/extract-bill-data", json={"document": None})
        assert response.status_code == 422
    
    def test_invalid_json_rejected(self):
        """Test that invalid JSON is rejected."""
        response = client.post(
            "/extract-bill-data",
            data="not json",
            headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422
    
    def test_invalid_url_scheme_rejected(self):
        """Test that invalid URL schemes are rejected."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "ftp://example.com/file.pdf"}
        )
        assert response.status_code == 422
    
    def test_url_without_scheme_rejected(self):
        """Test that URLs without scheme are rejected."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "example.com/file.pdf"}
        )
        assert response.status_code == 422
    
    def test_url_without_host_rejected(self):
        """Test that URLs without host are rejected."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://"}
        )
        assert response.status_code == 422
    
    def test_valid_https_url_accepted(self):
        """Test that valid HTTPS URLs pass validation."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/bill.pdf"}
        )
        # Should pass validation (may fail on download, but not on validation)
        assert response.status_code != 422
    
    def test_valid_http_url_accepted(self):
        """Test that valid HTTP URLs pass validation."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "http://example.com/bill.pdf"}
        )
        # Should pass validation
        assert response.status_code != 422


class TestBillExtractionResponseStructure:
    """Test response structure and format."""
    
    @patch('app.main.download_file_sync')
    @patch('app.extractor.extract_bill_data')
    def test_success_response_has_base_fields(self, mock_extract, mock_download):
        """Test successful response has required base fields."""
        mock_download.return_value = (b"test", "application/pdf")
        mock_extract.return_value = {"pagewise_line_items": [], "total_item_count": 0}
        
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/bill.pdf"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "is_success" in data
        assert "data" in data
        assert "token_usage" in data
    
    @patch('app.main.download_file_sync')
    @patch('app.extractor.extract_bill_data')
    def test_data_field_has_expected_structure(self, mock_extract, mock_download):
        """Test that data field contains required extraction fields."""
        mock_download.return_value = (b"test", "application/pdf")
        mock_extract.return_value = {
            "pagewise_line_items": [],
            "total_item_count": 0
        }
        
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/bill.pdf"}
        )
        
        data = response.json()
        assert "pagewise_line_items" in data.get("data", {})
        assert "total_item_count" in data.get("data", {})


class TestErrorResponses:
    """Test error response handling."""
    
    @patch('app.main.download_file_sync')
    def test_download_error_returns_valid_response(self, mock_download):
        """Test that download errors return valid error responses."""
        mock_download.side_effect = Exception("Network error")
        
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/missing.pdf"}
        )
        
        # Should return valid response (200 or 500)
        assert response.status_code in [200, 500]
        data = response.json()
        # Should have error info
        assert "is_success" in data or "error" in data
    
    @patch('app.main.download_file_sync')
    def test_file_too_large_error(self, mock_download):
        """Test handling of files exceeding size limits."""
        mock_download.side_effect = ValueError("File too large (>20MB)")
        
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/huge.pdf"}
        )
        
        # Should handle gracefully
        assert response.status_code in [200, 500]
        data = response.json()
        assert isinstance(data, dict)


class TestRequestPayloadValidation:
    """Test various request payload edge cases."""
    
    def test_url_with_query_parameters_passes(self):
        """Test that URLs with query parameters are accepted."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/bill?id=123&type=pdf"}
        )
        assert response.status_code != 422
    
    def test_url_with_fragments_passes(self):
        """Test that URLs with fragments are accepted."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/bill.pdf#page=1"}
        )
        assert response.status_code != 422
    
    def test_url_with_port_passes(self):
        """Test that URLs with explicit ports are accepted."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com:8080/bill.pdf"}
        )
        assert response.status_code != 422
    
    def test_url_with_auth_credentials_passes(self):
        """Test that URLs with auth credentials are accepted."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://user:pass@example.com/bill.pdf"}
        )
        assert response.status_code != 422
    
    def test_url_with_special_chars_passes(self):
        """Test that URLs with special characters are accepted."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/bill-%20-%40ok.pdf"}
        )
        assert response.status_code != 422


class TestInputSanitization:
    """Test input handling and sanitization."""
    
    def test_string_with_leading_trailing_spaces(self):
        """Test that URLs with spaces are stripped and validated."""
        response = client.post(
            "/extract-bill-data",
            json={"document": "  https://example.com/bill.pdf  "}
        )
        # Should pass validation (after stripping)
        assert response.status_code != 422
    
    def test_empty_string_rejected(self):
        """Test that empty strings are rejected."""
        response = client.post(
            "/extract-bill-data",
            json={"document": ""}
        )
        assert response.status_code == 422


class TestResponseJSON:
    """Test that all responses are valid JSON."""
    
    def test_health_endpoint_returns_json(self):
        """Test health endpoint returns valid JSON."""
        response = client.get("/health")
        assert response.headers.get("content-type")
        try:
            response.json()
            assert True
        except json.JSONDecodeError:
            pytest.fail("Response is not valid JSON")
    
    @patch('app.main.download_file_sync')
    def test_error_response_is_json(self, mock_download):
        """Test that error responses are valid JSON."""
        mock_download.side_effect = Exception("Error")
        
        response = client.post(
            "/extract-bill-data",
            json={"document": "https://example.com/bill.pdf"}
        )
        
        try:
            response.json()
            assert True
        except json.JSONDecodeError:
            pytest.fail("Error response is not valid JSON")


@pytest.mark.integration
class TestIntegrationLiveAPI:
    """Integration tests with actual API calls (requires server running)."""
    
    def test_live_health_endpoint(self):
        """Test health endpoint with live server."""
        response = client.get("/health")
        assert response.status_code == 200
