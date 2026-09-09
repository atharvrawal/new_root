"""Entry point for the Thumbnail Assistant.

Run with:  python main.py
Or invoke via the packaged executable produced by PyInstaller (see README).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from thumbnail_assistant.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
