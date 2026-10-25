"""Vet2Employ grounded RAG service."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("vet2employ_rag")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = "1.1.0"
