import os
import httpx
from firecrawl import FirecrawlApp

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
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(api_url, headers=self.headers, timeout=45)
            if resp.status_code != 200:
                raise Exception(f"LinkedIn API Error: {resp.text}")
            return str(resp.json()) # Return raw JSON string for now

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
            # item is standard dict or Pydantic object? 
            # Usually SearchResult is a dict-like or object. 
            # Let's assume object access first, fallback to get if needed, 
            # but usually Pydantic objects need dot notation.
            # safe access helper:
            def get_val(obj, key):
                return getattr(obj, key, None) or (obj.get(key) if isinstance(obj, dict) else None)

            title = get_val(item, 'title') or 'No Title'
            url_res = get_val(item, 'url') or '#'
            desc = get_val(item, 'description') or 'No Description'
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
