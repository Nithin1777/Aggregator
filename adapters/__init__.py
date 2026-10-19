"""
Adapters package for vendor APIs.
"""
from .base import BaseAdapter
from .shopify import ShopifyAdapter
from .robu import RobuAdapter

__all__ = ["BaseAdapter", "ShopifyAdapter", "RobuAdapter"]
