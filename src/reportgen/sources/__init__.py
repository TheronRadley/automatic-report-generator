"""Data source adapters (CSV, Excel, JSON, databases)."""

from reportgen.sources.base import LoadedDataset, inspect_dataframe
from reportgen.sources.factory import load_dataset, load_from_path

__all__ = ["LoadedDataset", "inspect_dataframe", "load_dataset", "load_from_path"]
