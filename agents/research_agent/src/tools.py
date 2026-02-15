import os
import logging
import httpx
from firecrawl import FirecrawlApp

logger = logging.getLogger(__name__)


def _get_val(obj, key):
    """Safely access a value from a Pydantic model or dict."""
    return getattr(obj, key, None) or (obj.get(key) if isinstance(obj, dict) else None)


class ResearchTools:
    def __init__(self):
        self.scrape_key = os.getenv("SCRAPECREATORS_API_KEY", "")
        self.firecrawl_key = os.getenv("FIRECRAWL_API_KEY", "")
        self.firecrawl = FirecrawlApp(api_key=self.firecrawl_key) if self.firecrawl_key else None
        self.headers = {"x-api-key": self.scrape_key}

    async def scrape_linkedin(self, url: str, endpoint_type: str) -> str:
        """Handles LinkedIn Profile & Company"""
        endpoint = "profile" if endpoint_type == "profile" else "company"
        api_url = f"https://api.scrapecreators.com/v1/linkedin/{endpoint}?url={url}"
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(api_url, headers=self.headers, timeout=45)
                if resp.status_code != 200:
                    logger.error("LinkedIn API Error (status %d): %s", resp.status_code, resp.text)
                    return f"Error: LinkedIn API returned status {resp.status_code}"
                return str(resp.json())
        except httpx.TimeoutException:
            logger.error("LinkedIn API request timed out for URL: %s", url)
            return "Error: LinkedIn API request timed out"
        except Exception as e:
            logger.error("LinkedIn scrape failed: %s", e)
            return f"Error: {e}"

    def firecrawl_scrape(self, url: str) -> str:
        """Single Page Scrape"""
        if not self.firecrawl:
             return "Error: Firecrawl API Key missing"
        # Updated to Firecrawl v2 Style
        # Returns firecrawl.v2.types.Document
        result = self.firecrawl.scrape(url, formats=['markdown'])
        return getattr(result, 'markdown', "") or ""

    def firecrawl_search(self, query: str) -> str:
        """Web Search"""
        if not self.firecrawl:
             return "Error: Firecrawl API Key missing"
        # Updated to Firecrawl v2 Style (no params dict)
        # Returns firecrawl.v2.types.SearchData (has 'web', 'news' fields)
        result = self.firecrawl.search(query, sources=['web', 'news'], limit=5)
        
        md = f"# Search Results: {query}\n\n"
        
        # Access 'web' attribute directly from Pydantic model
        # web is Optional[List[SearchResult]]
        items = getattr(result, 'web', []) or []
        
        for item in items:
            title = _get_val(item, 'title') or 'No Title'
            url_res = _get_val(item, 'url') or '#'
            desc = _get_val(item, 'description') or 'No Description'
            md += f"## {title}\n**URL:** {url_res}\n{desc}\n\n"
            
        return md

    def firecrawl_map(self, url: str) -> str:
        """Site Mapping"""
        if not self.firecrawl:
             return "Error: Firecrawl API Key missing"
        # Returns firecrawl.v2.types.MapData
        result = self.firecrawl.map(url, limit=50)
        links = getattr(result, 'links', []) or []
        return "\n".join([str(link) for link in links])
