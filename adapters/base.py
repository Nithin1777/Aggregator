"""
Base adapter interface for component vendors.
"""
from abc import ABC, abstractmethod
from typing import List, Tuple
from models import UnifiedProduct


class VendorFetchError(RuntimeError):
    """
    Raised when a vendor request failed in a way that must not be mistaken for
    end-of-catalog.

    Adapters signal a genuine end of catalog with an empty result, but a network
    error or unexpected HTTP status is NOT an end: returning empty there makes the
    ingestion loop mark the vendor 'completed' and silently truncate the catalog.
    """


class BaseAdapter(ABC):
    """Abstract base class that all vendor adapters must implement."""

    @property
    @abstractmethod
    def vendor_id(self) -> str:
        """Vendor identifier slug (e.g. 'robu', 'quartzcomponents', 'makerbazar')."""
        pass

    @property
    @abstractmethod
    def vendor_name(self) -> str:
        """Human-readable vendor display name."""
        pass

    @property
    @abstractmethod
    def base_url(self) -> str:
        """Root store domain URL."""
        pass

    @abstractmethod
    async def search_live(self, query: str, limit: int = 20) -> List[UnifiedProduct]:
        """Performs live real-time search query on the vendor."""
        pass

    @abstractmethod
    async def fetch_catalog_page(self, page: int, limit: int = 250) -> Tuple[List[UnifiedProduct], bool]:
        """
        Fetches a paginated slice of the vendor catalog.
        Returns a tuple of (products, has_more).
        """
        pass

    async def close(self):
        """Release any underlying HTTP sessions or client pools."""
        pass
