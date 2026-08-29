"""Configuration pytest pour DOMAutopsy.

Ajoute le repo root au sys.path pour que les tests puissent importer
les modules Python top-level (schemas, playwright_generator,
clean_steps_builder, replay_reporter) sans package structure.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
