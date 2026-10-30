import time
import logging
from typing import Any, Dict, List, Optional
import requests

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ConcurClient")


class ConcurError(Exception):
    """Base exception for Concur client errors."""
    pass


class ConcurAuthError(ConcurError):
    """Exception raised when authentication fails."""
    pass


class ConcurAPIError(ConcurError):
    """Exception raised when API requests return error status codes."""
    def __init__(self, message: str, status_code: int, response_text: str):
        super().__init__(f"{message} (Status: {status_code}) - Response: {response_text}")
        self.status_code = status_code
        self.response_text = response_text


class ConcurClient:
    """Client for SAP Concur APIs using OAuth2 Client Credentials grant."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_url: str = "https://us.api.concursolutions.com/oauth2/v0/token",
        base_url: str = "https://us.api.concursolutions.com",
    ):
        if not client_id or not client_secret:
            raise ValueError("Both client_id and client_secret must be provided")

        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.base_url = base_url.rstrip("/")
        
        # Token caching properties
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def get_token(self) -> str:
        """Retrieves an access token. Uses cached token if it is still valid."""
        # Buffer of 60 seconds to ensure the token doesn't expire during an API call
        if self._access_token and time.time() < (self._token_expires_at - 60):
            return self._access_token

        logger.info("Requesting new access token from Concur...")
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        try:
            response = requests.post(
                self.token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15
            )
            
            if response.status_code != 200:
                raise ConcurAuthError(
                    f"Failed to authenticate (HTTP {response.status_code}): {response.text}"
                )

            token_data = response.json()
            self._access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", 3600)
            self._token_expires_at = time.time() + float(expires_in)
            
            logger.info("Successfully authenticated with SAP Concur.")
            return self._access_token

        except requests.RequestException as e:
            raise ConcurAuthError(f"HTTP request failed during authentication: {str(e)}")
        except (ValueError, KeyError) as e:
            raise ConcurAuthError(f"Failed to parse token response: {str(e)}")

    def _get_headers(self) -> Dict[str, str]:
        """Utility to construct authorization and content headers."""
        token = self.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def list_reports(
        self, 
        user_login_id: str, 
        limit: int = 10,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Retrieves expense reports for the specified user.
        
        API reference: GET /api/v3.0/expense/reports
        """
        url = f"{self.base_url}/api/v3.0/expense/reports"
        params: Dict[str, Any] = {
            "user": user_login_id,
            "limit": limit
        }
        if status:
            params["status"] = status

        logger.info(f"Listing expense reports for user {user_login_id}...")
        try:
            response = requests.get(
                url, 
                headers=self._get_headers(), 
                params=params,
                timeout=15
            )
            
            if response.status_code != 200:
                raise ConcurAPIError("Failed to list expense reports", response.status_code, response.text)
            
            data = response.json()
            
            # The v3 reports list API normally wraps elements in an "Items" list, or returns it directly
            if isinstance(data, dict):
                return data.get("Items", [])
            elif isinstance(data, list):
                return data
            return []

        except requests.RequestException as e:
            raise ConcurAPIError(f"HTTP request failed: {str(e)}", 500, "")

    def create_draft_report(
        self,
        user_login_id: str,
        name: str,
        purpose: Optional[str] = None,
        comment: Optional[str] = None,
        policy_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Creates a draft expense report for the specified user.
        
        API reference: POST /api/v3.0/expense/reports
        """
        url = f"{self.base_url}/api/v3.0/expense/reports"
        params = {
            "user": user_login_id
        }

        # Build report header payload
        payload = {
            "Name": name
        }
        if purpose:
            payload["Purpose"] = purpose
        if comment:
            payload["Comment"] = comment
        if policy_id:
            payload["PolicyID"] = policy_id

        logger.info(f"Creating draft expense report '{name}' for user {user_login_id}...")
        try:
            response = requests.post(
                url,
                headers=self._get_headers(),
                params=params,
                json=payload,
                timeout=15
            )
            
            # 200 OK or 201 Created is typical for a successful post
            if response.status_code not in (200, 201):
                raise ConcurAPIError("Failed to create draft expense report", response.status_code, response.text)

            created_report = response.json()
            logger.info(f"Draft report created successfully. ReportID: {created_report.get('ReportID') or created_report.get('ID')}")
            return created_report

        except requests.RequestException as e:
            raise ConcurAPIError(f"HTTP request failed: {str(e)}", 500, "")
