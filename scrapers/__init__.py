"""Scraper module."""

from .base_scraper import BaseScraper, ScraperStats
from .enricher import DetailEnricher
from .realtor_ca import RealtorCaScraper
from .rentals_ca import RentalsCaScraper
from .apartments_com import ApartmentsComScraper

__all__ = [
    "BaseScraper",
    "ScraperStats",
    "DetailEnricher",
    "RealtorCaScraper",
    "RentalsCaScraper",
    "ApartmentsComScraper",
]