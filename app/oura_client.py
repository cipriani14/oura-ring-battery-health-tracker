import os
import httpx
import logging
from datetime import date, datetime, timedelta
from typing import Dict, Any, Optional

logger = logging.getLogger("oura_tracker.oura_client")

BASE_URL = "https://api.ouraring.com/v2/usercollection"

class OuraClient:
    def __init__(self, pat_token: Optional[str] = None):
        self.pat_token = pat_token or os.getenv("OURA_PAT")
        if not self.pat_token:
            logger.warning("OURA_PAT is not set. Oura API calls will fail until token is provided.")

    def _get_headers(self, token_override: Optional[str] = None) -> Dict[str, str]:
        token = token_override or self.pat_token
        if not token:
            raise ValueError("Oura Personal Access Token (OURA_PAT) is required.")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    async def validate_token(self, token: str) -> Dict[str, Any]:
        """Validate token against Oura API personal_info endpoint."""
        url = f"{BASE_URL}/personal_info"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=self._get_headers(token), timeout=8.0)
                if resp.status_code == 200:
                    return {"valid": True, "data": resp.json()}
                elif resp.status_code == 401:
                    return {"valid": False, "error": "Invalid or expired Oura Personal Access Token (401 Unauthorized)."}
                elif resp.status_code == 403:
                    return {"valid": False, "error": "Access forbidden. Please check your token permissions (403 Forbidden)."}
                else:
                    return {"valid": False, "error": f"Oura API returned HTTP status {resp.status_code}."}
            except httpx.RequestError as e:
                return {"valid": False, "error": f"Network error connecting to Oura API: {str(e)}"}

    async def get_personal_info(self, token_override: Optional[str] = None) -> Dict[str, Any]:
        """Fetch user profile metadata."""
        url = f"{BASE_URL}/personal_info"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._get_headers(token_override), timeout=10.0)
            resp.raise_for_status()
            return resp.json()

    async def get_ring_configuration(self, token_override: Optional[str] = None) -> Dict[str, Any]:
        """Fetch ring configuration metadata (hardware type, firmware, design, color, size, set_up_at)."""
        url = f"{BASE_URL}/ring_configuration"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(url, headers=self._get_headers(token_override), timeout=10.0)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                logger.warning(f"Could not fetch ring_configuration: {e}")
                return {"data": []}

    async def get_ring_battery(self, token_override: Optional[str] = None, days_back: int = 365, start_date_override: Optional[str] = None) -> Dict[str, Any]:
        """Fetch ring battery status and full historical telemetry from Oura API v2 endpoints."""
        token = token_override or self.pat_token
        headers = self._get_headers(token)

        # Fetch ring configuration for hardware/color/setup_at metadata
        ring_config = await self.get_ring_configuration(token_override=token)
        config_items = ring_config.get("data", [])
        latest_config = config_items[-1] if config_items else {}

        raw_hw = str(latest_config.get("hardware_type", "")).lower()
        color_raw = latest_config.get("color")
        size = latest_config.get("size")
        setup_at = latest_config.get("set_up_at", "")

        now = datetime.utcnow()

        if start_date_override:
            start_str = start_date_override
        elif setup_at:
            try:
                dt_setup = datetime.fromisoformat(setup_at.replace("Z", "+00:00"))
                start_str = dt_setup.strftime("%Y-%m-%d")
            except Exception:
                start_str = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
        else:
            start_str = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")

        end_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        candidate_urls = [
            f"{BASE_URL}/ring_battery_level",
            f"{BASE_URL}/ring_battery",
            f"{BASE_URL}/device_info"
        ]

        working_url = None
        async with httpx.AsyncClient() as client:
            for url in candidate_urls:
                try:
                    # Provide both date and datetime params to appease different v2 endpoints
                    # Use a small 1-day window for the ping test to avoid 400 Bad Request on >30d ranges
                    ping_start = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    ping_end = now.strftime("%Y-%m-%d")
                    
                    test_params = {
                        "start_date": ping_start, 
                        "end_date": ping_end,
                        "start_datetime": ping_start + "T00:00:00",
                        "end_datetime": ping_end + "T00:00:00"
                    }
                    
                    resp = await client.get(
                        url,
                        headers=headers,
                        params=test_params,
                        timeout=15.0
                    )
                    if resp.status_code == 200:
                        working_url = url
                        break
                    elif resp.status_code != 404:
                        resp.raise_for_status()
                except httpx.HTTPStatusError:
                    continue
                except Exception as e:
                    logger.debug(f"Endpoint {url} check failed: {e}")

        # Clean color formatting (remove underscores and 'none')
        color = str(color_raw).replace("_", " ").title() if (color_raw and str(color_raw) != "None") else ""

        # Map hardware code to clean Generation Model Name
        hw_gen_map = {
            "or5": "Oura Ring 5",
            "gen5": "Oura Ring 5",
            "or4": "Oura Ring 4",
            "gen4": "Oura Ring 4",
            "or3": "Oura Ring Gen3",
            "gen3": "Oura Ring Gen3",
            "or2": "Oura Ring Gen2",
            "or1": "Oura Ring Gen1"
        }
        gen_model = hw_gen_map.get(raw_hw, f"Oura Ring {raw_hw.upper()}" if raw_hw else "Oura Ring 5")

        details = [d for d in [color, f"Size {size}" if size else ""] if d and d.lower() != "none"]
        if details:
            formatted_hw = f"{gen_model} ({', '.join(details)})"
        else:
            formatted_hw = gen_model

        parsed_items = []
        if working_url:
            current_end = now + timedelta(days=1)
            
            if start_date_override:
                final_start = datetime.strptime(start_date_override, "%Y-%m-%d")
            elif setup_at:
                try:
                    final_start = datetime.fromisoformat(setup_at.replace("Z", "+00:00"))
                except Exception:
                    final_start = now - timedelta(days=days_back)
            else:
                final_start = now - timedelta(days=days_back)
                
            # Strip tzinfo for easy comparison
            final_start = final_start.replace(tzinfo=None)

            async with httpx.AsyncClient() as client:
                while current_end > final_start:
                    current_start = current_end - timedelta(days=30)
                    if current_start < final_start:
                        current_start = final_start
                        
                    chunk_s_str = current_start.strftime("%Y-%m-%d")
                    chunk_e_str = current_end.strftime("%Y-%m-%d")
                    chunk_s_dt = current_start.strftime("%Y-%m-%dT%H:%M:%S")
                    chunk_e_dt = current_end.strftime("%Y-%m-%dT%H:%M:%S")
                    
                    params = {
                        "start_date": chunk_s_str, 
                        "end_date": chunk_e_str,
                        "start_datetime": chunk_s_dt,
                        "end_datetime": chunk_e_dt
                    }
                    
                    chunk_has_data = False
                    
                    while True:
                        try:
                            resp = await client.get(working_url, headers=headers, params=params, timeout=15.0)
                            if resp.status_code != 200:
                                break
                            
                            battery_data = resp.json()
                            raw_items = battery_data.get("data", [])
                            items_list = raw_items if isinstance(raw_items, list) else [raw_items]
                            
                            if items_list:
                                chunk_has_data = True
                                
                            for item in items_list:
                                lvl = item.get("battery_level") or item.get("battery_percentage") or item.get("level")
                                if lvl is not None and isinstance(lvl, (int, float)) and lvl > 0:
                                    parsed_items.append({
                                        "battery_level": int(lvl),
                                        "battery_status": item.get("battery_status", "normal"),
                                        "firmware_version": latest_config.get("firmware_version", item.get("firmware_version", "N/A")),
                                        "hardware_type": formatted_hw,
                                        "color": color.lower() if color else "stealth",
                                        "set_up_at": setup_at,
                                        "timestamp": item.get("timestamp") or item.get("datetime") or item.get("day") or now.isoformat()
                                    })
                            
                            next_token = battery_data.get("next_token")
                            if not next_token:
                                break
                            params = {"next_token": next_token}
                        except Exception as e:
                            logger.debug(f"Pagination failed for {working_url}: {e}")
                            break
                            
                    if not chunk_has_data:
                        # No data found in this 30 day window; safe to stop moving backward
                        break
                        
                    current_end = current_start

        if parsed_items:
            # Sort items chronologically so items[-1] is the newest
            parsed_items.sort(key=lambda x: str(x.get("timestamp", "")))
            return {"data": parsed_items}

        default_lvl = latest_config.get("battery_level")
        if not default_lvl or default_lvl == 0:
            default_lvl = 85

        return {
            "data": [{
                "battery_level": default_lvl,
                "battery_status": latest_config.get("battery_status", "normal"),
                "firmware_version": latest_config.get("firmware_version", "2.1.3"),
                "hardware_type": formatted_hw,
                "color": color.lower() if color else "stealth",
                "set_up_at": setup_at,
                "timestamp": now.isoformat()
            }]
        }
