"""Scraper module."""

from .base_scraper import BaseScraper, ScraperStats
from .realtor_ca import RealtorCaScraper
from .rentals_ca import RentalsCaScraper
from .apartments_com import ApartmentsComScraper

__all__ = [
    "BaseScraper",
    "ScraperStats",
    "RealtorCaScraper",
    "RentalsCaScraper",
    "ApartmentsComScraper",
]