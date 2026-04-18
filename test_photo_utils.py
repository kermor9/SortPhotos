#!/usr/bin/env python3
"""Simple checks for :class:`FileMetadata` datetime property.

This script isn't a full test suite but exercises the getter/setter so
that developers know the sqlite conversions are working.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from photo_utils import FileMetadata


def main() -> None:
    # datetime -> sqlite string
    dt = datetime(2021, 5, 17, 13, 45, 30)
    fm = FileMetadata(
        path=Path("."),
        datetime=dt,
        file_hash="",
        perceptual_hash=None,
        size=0,
        mtime=0,
        is_video=False,
    )
    print("Raw internal value:", repr(fm._datetime))
    print("Getter returns:", fm.datetime)

    # assign a string coming from a database query
    fm.datetime = "2022-02-02 02:02:02"
    print("After assigning string, getter returns:", fm.datetime)

    # clear it
    fm.datetime = None
    print("After assigning None, getter returns:", fm.datetime)


if __name__ == "__main__":
    main()
